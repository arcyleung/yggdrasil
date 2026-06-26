"""Yggdrasil control-plane UI — FastAPI + Jinja2.

Run: uvicorn yggdrasil.web.app:app --host 127.0.0.1 --port 8080
     python -m yggdrasil.web
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Union

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from yggdrasil.services.auth_service import AuthError
from yggdrasil.web.auth_factory import WebAuthFacade, build_web_auth

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_UI_SECRET = "dev-secret"
SESSION_COOKIE_KEY = "ygg_session"

# Avoid FastAPI 0.116+ issues with PEP604 unions in some annotations by aliasing
HtmlOrRedirect = Union[HTMLResponse, RedirectResponse]
PlainOrRedirect = Union[PlainTextResponse, RedirectResponse]
JsonOrRedirect = Union[JSONResponse, RedirectResponse, PlainTextResponse]


def _public_base_url(env: dict[str, str] | None = None) -> str:
    e = env if env is not None else os.environ
    return (e.get("YGG_PUBLIC_BASE_URL") or DEFAULT_PUBLIC_BASE_URL).rstrip("/")


def _url_prefix(env: dict[str, str] | None = None, *, public_base_url: str | None = None) -> str:
    """Path prefix when behind Tailscale Funnel --set-path=/ygg (proxy strips prefix).

    Links/redirects must use this prefix so browsers stay on /ygg/* not host root.
    """
    e = env if env is not None else os.environ
    explicit = (e.get("YGG_URL_PREFIX") or "").strip().rstrip("/")
    if explicit:
        return explicit if explicit.startswith("/") else f"/{explicit}"
    base = public_base_url or _public_base_url(e)
    try:
        from urllib.parse import urlparse

        path = (urlparse(base).path or "").rstrip("/")
        if path and path != "/":
            return path
    except Exception:
        pass
    return ""


def _session_secret(env: dict[str, str] | None = None) -> str:
    e = env if env is not None else os.environ
    return (e.get("YGG_UI_SECRET") or DEFAULT_UI_SECRET).strip() or DEFAULT_UI_SECRET


def create_app(
    *,
    auth: WebAuthFacade | None = None,
    key_name_map: dict[str, str] | None = None,
    sqlite_path: str | Path | None = None,
    env: dict[str, str] | None = None,
    public_base_url: str | None = None,
) -> FastAPI:
    """Application factory (testable). Uses real AuthService + SQLite token store."""
    env_map = dict(env) if env is not None else dict(os.environ)
    base_url = (public_base_url or _public_base_url(env_map)).rstrip("/")
    prefix = _url_prefix(env_map, public_base_url=base_url)
    secret = _session_secret(env_map)

    def _p(path: str) -> str:
        """App-absolute path with optional public prefix (e.g. /ygg/lab/home)."""
        if not path.startswith("/"):
            path = "/" + path
        return f"{prefix}{path}" if prefix else path

    if auth is not None:
        auth_svc = auth
    else:
        auth_svc = build_web_auth(
            sqlite_path=sqlite_path,
            key_name_map=key_name_map,
            env=env_map,
        )

    app = FastAPI(title="Yggdrasil Control Plane", version="0.1.0")
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie=SESSION_COOKIE_KEY,
        same_site="lax",
        https_only=False,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.auth = auth_svc
    app.state.public_base_url = base_url
    app.state.url_prefix = prefix
    app.state.templates = templates

    def _tpl(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            name,
            {
                "request": request,
                "public_base_url": base_url,
                "url_prefix": prefix,
                "p": _p,
                "mcp_url": f"{base_url}/mcp",
                **ctx,
            },
        )

    def _session_user(request: Request) -> dict[str, Any] | None:
        sess = request.session
        token_id = sess.get("token_id")
        owner = sess.get("owner")
        if not token_id or not owner:
            return None
        return {
            "token_id": token_id,
            "owner": owner,
            "tenant_id": sess.get("tenant_id", "lab"),
            # flash_token only present until consumed (one-time reveal)
            "flash_token": sess.get("flash_token"),
        }

    def _set_session(
        request: Request,
        result: dict[str, Any],
        *,
        flash_raw_token: bool = True,
    ) -> None:
        """Persist token_id + owner; optionally flash raw bearer once for copy."""
        request.session["token_id"] = result["token_id"]
        request.session["owner"] = result["owner"]
        request.session["tenant_id"] = result["tenant_id"]
        if flash_raw_token and result.get("token"):
            request.session["flash_token"] = result["token"]
        else:
            request.session.pop("flash_token", None)

    def _pop_flash_token(request: Request) -> str | None:
        tok = request.session.pop("flash_token", None)
        return tok if isinstance(tok, str) else None

    def _clear_session(request: Request) -> None:
        for k in ("token_id", "owner", "tenant_id", "flash_token", "token"):
            request.session.pop(k, None)

    def _bearer_from_request(request: Request) -> str | None:
        """Bearer for skill/mcp: query, Authorization, or flash (not long-lived session secret)."""
        q = request.query_params.get("token")
        if q:
            return q
        auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        # Allow one-time flash still in session for same-session skill download after login
        flash = request.session.get("flash_token")
        if isinstance(flash, str) and flash:
            return flash
        return None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return _tpl(request, "home.html")

    @app.get("/lab/login", response_class=HTMLResponse)
    def lab_login_get(request: Request) -> Any:
        user = _session_user(request)
        if user and user.get("tenant_id") == "lab":
            return RedirectResponse(url=_p("/lab/home"), status_code=303)
        return _tpl(request, "lab_login.html", error=None)

    @app.post("/lab/login", response_class=HTMLResponse)
    async def lab_login_post(request: Request, api_key: str = Form(...)) -> Any:
        try:
            result = auth_svc.exchange_api_key(api_key)
        except AuthError as exc:
            return _tpl(request, "lab_login.html", error=exc.message)
        _set_session(request, result, flash_raw_token=True)
        return RedirectResponse(url=_p("/lab/home"), status_code=303)

    @app.get("/lab/home", response_class=HTMLResponse)
    def lab_home(request: Request) -> Any:
        user = _session_user(request)
        if not user or user.get("tenant_id") != "lab":
            return RedirectResponse(url=_p("/lab/login"), status_code=303)
        # Reveal bearer once (consume flash) — session keeps token_id only afterward
        bearer = _pop_flash_token(request)
        return _tpl(
            request,
            "lab_home.html",
            owner=user["owner"],
            tenant_id=user["tenant_id"],
            token_id=user.get("token_id", ""),
            bearer_token=bearer,
        )

    @app.post("/lab/logout")
    def lab_logout(request: Request) -> RedirectResponse:
        _clear_session(request)
        return RedirectResponse(url=_p("/"), status_code=303)

    def _skill_context(request: Request, *, require_tenant: str | None = None) -> dict[str, Any] | None:
        token = _bearer_from_request(request)
        if not token:
            return None
        try:
            principal = auth_svc.resolve_token(token)
        except AuthError:
            return None
        if require_tenant and principal.tenant_id != require_tenant:
            return None
        return {
            "public_base_url": base_url,
            "mcp_url": f"{base_url}/mcp",
            "bearer_token": token,
            "owner": principal.owner,
            "tenant_id": principal.tenant_id,
        }

    @app.get("/lab/skill.md")
    def lab_skill_md(request: Request) -> Any:
        ctx = _skill_context(request, require_tenant="lab")
        if ctx is None:
            user = _session_user(request)
            if not user:
                return RedirectResponse(url=_p("/lab/login"), status_code=303)
            return PlainTextResponse(
                "unauthorized: pass ?token=ygg_... or Authorization Bearer (session stores token_id only)",
                status_code=401,
            )
        text = templates.get_template("skill_lab.md.j2").render(**ctx)
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.get("/lab/mcp.json")
    def lab_mcp_json(request: Request) -> Any:
        ctx = _skill_context(request, require_tenant="lab")
        if ctx is None:
            user = _session_user(request)
            if not user:
                return RedirectResponse(url=_p("/lab/login"), status_code=303)
            return PlainTextResponse(
                "unauthorized: pass ?token=ygg_... or Authorization Bearer",
                status_code=401,
            )
        body = templates.get_template("mcp_config.json.j2").render(**ctx)
        return PlainTextResponse(body, media_type="application/json; charset=utf-8")

    @app.get("/demo", response_class=HTMLResponse)
    def demo_get(request: Request) -> HTMLResponse:
        user = _session_user(request)
        demo_token = None
        demo_owner = None
        if user and user.get("tenant_id") == "demo":
            demo_owner = user["owner"]
            demo_token = request.session.get("flash_token")
        return _tpl(
            request,
            "demo.html",
            demo_token=demo_token,
            demo_owner=demo_owner,
            error=None,
        )

    @app.post("/demo", response_class=HTMLResponse)
    async def demo_post(request: Request) -> HTMLResponse:
        try:
            result = auth_svc.issue_demo_token()
        except AuthError as exc:
            return _tpl(request, "demo.html", demo_token=None, demo_owner=None, error=exc.message)
        _set_session(request, result, flash_raw_token=True)
        return _tpl(
            request,
            "demo.html",
            demo_token=result["token"],
            demo_owner=result["owner"],
            error=None,
        )

    @app.get("/demo/skill.md")
    def demo_skill_md(request: Request) -> Any:
        ctx = _skill_context(request, require_tenant="demo")
        if ctx is None:
            try:
                result = auth_svc.issue_demo_token()
                _set_session(request, result, flash_raw_token=True)
                ctx = {
                    "public_base_url": base_url,
                    "mcp_url": f"{base_url}/mcp",
                    "bearer_token": result["token"],
                    "owner": result["owner"],
                    "tenant_id": result["tenant_id"],
                }
            except AuthError:
                return RedirectResponse(url=_p("/demo"), status_code=303)
        text = templates.get_template("skill_demo.md.j2").render(**ctx)
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.post("/api/v1/tokens/exchange")
    async def tokens_exchange(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "invalid json"}, status_code=400)
        api_key = body.get("api_key") if isinstance(body, dict) else None
        if not api_key:
            return JSONResponse({"detail": "api_key required"}, status_code=400)
        try:
            result = auth_svc.exchange_api_key(str(api_key))
        except AuthError as exc:
            return JSONResponse({"detail": exc.message}, status_code=401)
        return JSONResponse(
            {
                "token": result["token"],
                "owner": result["owner"],
                "tenant_id": result["tenant_id"],
                "token_id": result["token_id"],
            }
        )

    @app.post("/api/v1/tokens/revoke")
    async def tokens_revoke(request: Request) -> JSONResponse:
        token = _bearer_from_request(request)
        token_id = request.session.get("token_id")
        revoked = False
        if token:
            revoked = auth_svc.revoke_bearer(token)
        elif token_id:
            revoked = auth_svc.revoke_token_id(str(token_id))
        if not revoked and not token and not token_id:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        if not revoked:
            return JSONResponse({"detail": "token not found or already revoked"}, status_code=404)
        _clear_session(request)
        return JSONResponse({"status": "revoked"})

    @app.get("/mcp")
    def mcp_stub() -> JSONResponse:
        return JSONResponse(
            {"detail": "MCP HTTP gateway not implemented; use stdio with YGG_MCP_TOKEN"},
            status_code=501,
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    bind = os.environ.get("YGG_UI_BIND", "127.0.0.1:8080")
    if ":" in bind:
        host, _, port_s = bind.rpartition(":")
        port = int(port_s)
    else:
        host, port = "127.0.0.1", int(bind)
    uvicorn.run("yggdrasil.web.app:app", host=host or "127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()
