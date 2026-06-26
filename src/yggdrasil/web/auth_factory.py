"""Build AuthService for the control-plane UI (real multi-tenant tokens)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from yggdrasil.adapters.token_store import SqliteTokenStore
from yggdrasil.config import load_config
from yggdrasil.domain.principal import Principal
from yggdrasil.services.auth_service import AuthError, AuthService, build_auth_service_from_store_path

# Re-export for app imports
__all__ = [
    "AuthError",
    "AuthService",
    "Principal",
    "WebAuthFacade",
    "build_web_auth",
    "token_result_dict",
]


def token_result_dict(raw: str, principal: Principal, record: Any) -> dict[str, Any]:
    """UI/JSON shape: includes raw token once; session should store token_id."""
    return {
        "token": raw,
        "owner": principal.owner,
        "tenant_id": principal.tenant_id,
        "token_id": principal.token_id,
        "scopes": sorted(principal.scopes),
    }


class WebAuthFacade:
    """Thin adapter so routes can use dict results + revoke by bearer."""

    def __init__(self, auth: AuthService, token_store: SqliteTokenStore) -> None:
        self._auth = auth
        self._tokens = token_store

    @property
    def auth_service(self) -> AuthService:
        return self._auth

    @property
    def token_store(self) -> SqliteTokenStore:
        return self._tokens

    def exchange_api_key(self, api_key: str) -> dict[str, Any]:
        raw, principal, record = self._auth.exchange_api_key(api_key)
        return token_result_dict(raw, principal, record)

    def issue_demo_token(self) -> dict[str, Any]:
        raw, principal, record = self._auth.issue_demo_token()
        return token_result_dict(raw, principal, record)

    def resolve_token(self, raw_token: str) -> Principal:
        return self._auth.resolve_bearer(raw_token)

    def revoke_bearer(self, raw_token: str) -> bool:
        principal = self._tokens.resolve_token(raw_token)
        if principal is None:
            return False
        return self._auth.revoke_token(principal.token_id)

    def revoke_token_id(self, token_id: str) -> bool:
        return self._auth.revoke_token(token_id)

    def principal_for_token_id(self, token_id: str) -> Principal | None:
        """Return Principal if token_id exists, not revoked, and not expired."""
        from datetime import datetime, timezone

        rec = self._tokens.get_record(str(token_id))
        if rec is None or rec.revoked_at is not None:
            return None
        if rec.expires_at is not None:
            exp = rec.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                return None
        return rec.to_principal()


def build_web_auth(
    *,
    sqlite_path: str | Path | None = None,
    key_name_map: Mapping[str, str] | None = None,
    user_mapping_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    demo_enabled: bool = True,
    token_ttl_days: int | None = 90,
) -> WebAuthFacade:
    """Construct AuthService + SqliteTokenStore (same DB as trajectories by default)."""
    env_map = dict(env) if env is not None else dict(os.environ)
    path = sqlite_path
    mapping = user_mapping_path
    ttl = token_ttl_days
    demo = demo_enabled
    default_tenant = "lab"
    demo_owner = "demo"
    if path is None or mapping is None:
        try:
            cfg = load_config(environ=env_map)
            path = path or cfg.sqlite_path
            mapping = mapping if mapping is not None else cfg.user_mapping_path
            ttl = cfg.token_ttl_days if token_ttl_days == 90 else token_ttl_days
            demo = cfg.demo_enabled if demo_enabled is True else demo_enabled
            default_tenant = cfg.default_tenant
            demo_owner = cfg.demo_owner
        except Exception:
            path = path or Path(env_map.get("YGG_SQLITE_PATH") or "./data/yggdrasil.db")
            mapping = mapping or env_map.get("YGG_USER_MAPPING_PATH") or env_map.get("KEY_NAME_MAP")

    auth, store = build_auth_service_from_store_path(
        path,
        user_mapping_path=mapping,
        key_name_map=key_name_map,
        default_tenant=default_tenant,
        demo_owner=demo_owner,
        demo_enabled=demo,
        token_ttl_days=ttl,
        env=env_map,
    )
    # Seed env demo token if set
    demo_tok = env_map.get("YGG_DEMO_TOKEN")
    if demo_tok and demo:
        if store.resolve_token(demo_tok) is None:
            try:
                auth.issue_demo_token(label="env:YGG_DEMO_TOKEN", raw_token=demo_tok)
            except Exception:
                pass
    return WebAuthFacade(auth, store)
