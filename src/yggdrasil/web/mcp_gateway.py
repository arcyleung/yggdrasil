"""Streamable HTTP MCP gateway for the control plane (Tailscale /ygg/mcp).

Mounts FastMCP's Streamable HTTP transport under ``/mcp`` with Bearer
``ygg_…`` auth (same tokens as lab login). Per-request principal is bound via
``principal_context`` so multi-user tenants work on one process.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from yggdrasil.mcp.app_context import AppContext
from yggdrasil.mcp.server import build_server
from yggdrasil.services.auth_service import AuthError
from yggdrasil.services.principal_context import reset_principal, set_principal

logger = logging.getLogger(__name__)


def _bearer_from_scope(scope: Scope) -> str | None:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Some clients send token only
    if auth.startswith("ygg_"):
        return auth.strip()
    return None


class BearerPrincipalMiddleware:
    """ASGI middleware: require Bearer ygg_ token, bind principal for the request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        resolve_token: Callable[[str], Any],
        optional: bool = False,
    ) -> None:
        self.app = app
        self.resolve_token = resolve_token
        self.optional = optional

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Health-style GET without body on /mcp root may be probes — still require auth for MCP
        token = _bearer_from_scope(scope)
        if not token:
            if self.optional:
                await self.app(scope, receive, send)
                return
            resp = JSONResponse(
                {"detail": "Authorization Bearer ygg_… token required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="yggdrasil-mcp"'},
            )
            await resp(scope, receive, send)
            return

        try:
            principal = self.resolve_token(token)
        except AuthError as exc:
            resp = JSONResponse({"detail": exc.message}, status_code=401)
            await resp(scope, receive, send)
            return
        if principal is None:
            resp = JSONResponse({"detail": "invalid or expired token"}, status_code=401)
            await resp(scope, receive, send)
            return

        tok = set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(tok)


def build_mcp_http_app(ctx: AppContext | None = None) -> tuple[Any, Any]:
    """Return (starlette_app, session_manager) for lifespan ``async with manager.run()``.

    FastMCP route is at ``/`` so FastAPI can ``mount("/mcp", app)`` and clients use
    ``https://host/ygg/mcp`` (Funnel strips ``/ygg`` → POST ``/mcp``).
    """
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    context = ctx or AppContext.from_config()
    # Multi-user HTTP: do not pin a single process principal from env for tool overrides.
    # Tools fall back to get_principal() contextvar set by BearerPrincipalMiddleware.
    context.principal = None

    hosts_raw = os.environ.get(
        "YGG_MCP_ALLOWED_HOSTS",
        "arcyleung-ubuntu.tailb940e6.ts.net,localhost,127.0.0.1,localhost:6001,127.0.0.1:6001",
    )
    origins_raw = os.environ.get("YGG_MCP_ALLOWED_ORIGINS", "*")
    allowed_hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
    # TransportSecurity supports suffix wildcards like host:*
    allowed_hosts_expanded: list[str] = []
    for h in allowed_hosts:
        allowed_hosts_expanded.append(h)
        if ":" not in h and "*" not in h:
            allowed_hosts_expanded.append(f"{h}:*")
    if origins_raw.strip() == "*":
        allowed_origins = ["*"]
        enable_dns = False  # wildcard origins + rebinding checks are awkward
    else:
        allowed_origins = [o.strip() for o in origins_raw.split(",") if o.strip()]
        enable_dns = True

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=enable_dns,
        allowed_hosts=allowed_hosts_expanded or ["*"],
        allowed_origins=allowed_origins,
    )

    mcp = FastMCP(
        "yggdrasil",
        streamable_http_path="/",
        # Stateless avoids sticky session issues behind Funnel; Codex still gets full tool calls.
        stateless_http=os.environ.get("YGG_MCP_STATELESS", "1") not in ("0", "false", "no"),
        transport_security=security,
        host="0.0.0.0",
    )
    from yggdrasil.mcp.tools import register_tools

    register_tools(mcp, context)
    # Force build of streamable app + session manager
    starlette_app = mcp.streamable_http_app()
    manager = mcp.session_manager
    return starlette_app, manager, context


def attach_mcp_gateway(app: Any, *, auth_resolve: Callable[[str], Any] | None = None) -> AppContext:
    """Mount Streamable HTTP MCP on FastAPI ``app`` at ``/mcp`` with bearer auth.

    Call once at startup; registers lifespan for the session manager.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    mcp_starlette, manager, ctx = build_mcp_http_app()

    def _resolve(token: str):
        if auth_resolve is not None:
            return auth_resolve(token)
        if ctx.auth_service is not None:
            return ctx.auth_service.resolve_bearer(token)
        if ctx.token_store is not None:
            return ctx.token_store.resolve_token(token)
        raise AuthError("auth not configured")

    gated = BearerPrincipalMiddleware(mcp_starlette, resolve_token=_resolve)

    # Preserve existing lifespan if any
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        async with manager.run():
            logger.info("MCP Streamable HTTP session manager running (mount /mcp)")
            if previous is not None:
                async with previous(app_instance):
                    yield
            else:
                yield

    app.router.lifespan_context = lifespan

    class _McpRootASGI:
        """ASGI app: FastMCP streamable handler at path ``/`` (mounted under /mcp)."""

        def __init__(self, inner: ASGIApp) -> None:
            self.inner = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                # Under Mount("/mcp"), path is remainder ("" or "/") — normalize to "/"
                path = scope.get("path") or ""
                if path in ("", "/"):
                    scope = dict(scope)
                    scope["path"] = "/"
                    scope["raw_path"] = b"/"
            await self.inner(scope, receive, send)

    # Mount at /mcp — clients should use …/mcp or …/mcp/ (see path rewrite middleware on app)
    app.mount("/mcp", _McpRootASGI(gated))

    app.state.mcp_context = ctx
    app.state.mcp_session_manager = manager
    return ctx
