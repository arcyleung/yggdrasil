"""API key exchange and demo token issuance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from yggdrasil.adapters.importers.api_key_owners import (
    fingerprint_auth_value,
    load_key_name_map,
    owner_map_from_key_name_map,
)
from yggdrasil.adapters.token_store import SqliteTokenStore
from yggdrasil.domain.principal import Principal
from yggdrasil.ports.token_store import TokenRecord, TokenStore


class AuthError(Exception):
    """Authentication / authorization failure (maps to 401 for HTTP)."""

    def __init__(self, message: str = "authentication failed") -> None:
        self.message = message
        super().__init__(message)


DEFAULT_LAB_SCOPES: frozenset[str] = frozenset({"read", "write"})
DEFAULT_DEMO_SCOPES: frozenset[str] = frozenset({"read", "write"})


class AuthService:
    """Exchange lab API keys for opaque tokens; issue demo tokens."""

    def __init__(
        self,
        token_store: TokenStore,
        *,
        key_name_map: Mapping[str, str] | None = None,
        user_mapping_path: str | Path | None = None,
        default_tenant: str = "lab",
        demo_owner: str = "demo",
        demo_enabled: bool = True,
        token_ttl_days: int | None = 90,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._tokens = token_store
        self._default_tenant = default_tenant
        self._demo_owner = demo_owner
        self._demo_enabled = demo_enabled
        self._token_ttl_days = token_ttl_days

        if key_name_map is not None:
            self._key_name_map = dict(key_name_map)
        else:
            self._key_name_map = load_key_name_map(user_mapping_path, env=env)
        self._fp_to_owner = owner_map_from_key_name_map(self._key_name_map)
        # Also allow direct full-key lookup for exchange
        self._key_to_owner: dict[str, str] = {
            str(k).strip(): str(v).strip()
            for k, v in self._key_name_map.items()
            if str(k).strip() and str(v).strip()
        }

    def _expires_at(self) -> datetime | None:
        if self._token_ttl_days is None:
            return None
        return datetime.now(timezone.utc) + timedelta(days=self._token_ttl_days)

    def resolve_owner_for_api_key(self, api_key: str) -> str | None:
        """Match API key to owner via full key or auth fingerprint."""
        key = str(api_key).strip()
        if not key:
            return None
        # Strip Bearer if present
        import re

        bare = re.sub(r"(?i)^bearer\s+", "", key).strip()
        if bare in self._key_to_owner:
            return self._key_to_owner[bare]
        if key in self._key_to_owner:
            return self._key_to_owner[key]
        for variant in (bare, key, f"Bearer {bare}"):
            fp = fingerprint_auth_value(variant)
            if fp and fp in self._fp_to_owner:
                return self._fp_to_owner[fp]
        return None

    def exchange_api_key(
        self,
        api_key: str,
        *,
        scopes: frozenset[str] | set[str] | list[str] | None = None,
        label: str | None = None,
        tenant_id: str | None = None,
    ) -> tuple[str, Principal, TokenRecord]:
        """Prove lab API key once; issue opaque bearer token.

        Returns (raw_token, principal, record). Raises AuthError if unknown key.
        """
        owner = self.resolve_owner_for_api_key(api_key)
        if owner is None:
            raise AuthError("unknown or unmapped API key")
        tid = tenant_id or self._default_tenant
        scope_set = frozenset(scopes) if scopes is not None else DEFAULT_LAB_SCOPES
        raw, record = self._tokens.create_token(
            tenant_id=tid,
            owner=owner,
            scopes=scope_set,
            label=label or f"lab:{owner}",
            expires_at=self._expires_at(),
        )
        return raw, record.to_principal(), record

    def issue_demo_token(
        self,
        *,
        owner: str | None = None,
        scopes: frozenset[str] | set[str] | list[str] | None = None,
        label: str | None = None,
        raw_token: str | None = None,
    ) -> tuple[str, Principal, TokenRecord]:
        """Issue a demo-tenant token (tenant_id=demo)."""
        if not self._demo_enabled:
            raise AuthError("demo tenant is disabled")
        demo_owner = owner or self._demo_owner
        scope_set = frozenset(scopes) if scopes is not None else DEFAULT_DEMO_SCOPES
        raw, record = self._tokens.create_token(
            tenant_id="demo",
            owner=demo_owner,
            scopes=scope_set,
            label=label or "demo",
            expires_at=self._expires_at(),
            raw_token=raw_token,
        )
        return raw, record.to_principal(), record

    def resolve_bearer(self, raw_token: str) -> Principal:
        """Resolve opaque token to Principal or raise AuthError."""
        principal = self._tokens.resolve_token(raw_token)
        if principal is None:
            raise AuthError("invalid or revoked token")
        return principal

    def revoke_token(self, token_id: str) -> bool:
        return self._tokens.revoke(token_id)


def build_auth_service_from_store_path(
    sqlite_path: str | Path,
    *,
    user_mapping_path: str | Path | None = None,
    key_name_map: Mapping[str, str] | None = None,
    default_tenant: str = "lab",
    demo_owner: str = "demo",
    demo_enabled: bool = True,
    token_ttl_days: int | None = 90,
    env: Mapping[str, str] | None = None,
    shared_conn: object | None = None,
) -> tuple[AuthService, SqliteTokenStore]:
    """Convenience: token store on same SQLite path (or shared connection)."""
    if shared_conn is not None:
        token_store = SqliteTokenStore(shared_conn)  # type: ignore[arg-type]
    else:
        token_store = SqliteTokenStore(sqlite_path)
    auth = AuthService(
        token_store,
        key_name_map=key_name_map,
        user_mapping_path=user_mapping_path,
        default_tenant=default_tenant,
        demo_owner=demo_owner,
        demo_enabled=demo_enabled,
        token_ttl_days=token_ttl_days,
        env=env,
    )
    return auth, token_store
