"""Local auth stub when multi-tenant AuthService is not available.

Loads KEY_NAME_MAP / user_mapping.yaml via existing importers and issues a
simple signed bearer token (itsdangerous URLSafeSerializer).
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from itsdangerous import BadSignature, URLSafeSerializer

from yggdrasil.adapters.importers.api_key_owners import (
    fingerprint_auth_value,
    load_key_name_map,
    owner_map_from_key_name_map,
)

DEFAULT_UI_SECRET = "dev-secret"
TOKEN_SALT = "ygg-ui-token-v1"
LAB_TENANT = "lab"
DEMO_TENANT = "demo"
DEMO_OWNER = "demo"


@dataclass(frozen=True)
class Principal:
    """Minimal principal matching control-plane / multi-tenant contract."""

    tenant_id: str
    owner: str
    scopes: frozenset[str]
    token_id: str


class AuthError(Exception):
    """Raised when API key exchange or token validation fails."""

    def __init__(self, message: str = "unauthorized", *, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AuthStub:
    """Prove-once key exchange (spec option B) backed by user_mapping / KEY_NAME_MAP."""

    def __init__(
        self,
        *,
        secret: str | None = None,
        mapping_path: str | Path | None = None,
        key_name_map: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        base_dir: Path | str | None = None,
        demo_enabled: bool = True,
        demo_owner: str = DEMO_OWNER,
    ) -> None:
        env_map = env if env is not None else os.environ
        self._secret = (secret or env_map.get("YGG_UI_SECRET") or DEFAULT_UI_SECRET).strip()
        self._serializer = URLSafeSerializer(self._secret, salt=TOKEN_SALT)
        self._demo_enabled = demo_enabled
        self._demo_owner = demo_owner or DEMO_OWNER

        if key_name_map is not None:
            self._key_name_map = {str(k): str(v) for k, v in key_name_map.items()}
        else:
            path = mapping_path or env_map.get("YGG_USER_MAPPING_PATH") or env_map.get("KEY_NAME_MAP")
            self._key_name_map = load_key_name_map(path, base_dir=base_dir, env=env_map)

        # Normalize keys (strip Bearer) for O(1) lookup by full key
        self._key_to_owner: dict[str, str] = {}
        for raw_key, owner in self._key_name_map.items():
            key = str(raw_key).strip()
            key = key[7:].strip() if key.lower().startswith("bearer ") else key
            name = str(owner).strip()
            if key and name:
                self._key_to_owner[key] = name

        self._fp_to_owner = owner_map_from_key_name_map(self._key_name_map)

    @property
    def known_owners(self) -> set[str]:
        return set(self._key_to_owner.values())

    def exchange_api_key(self, api_key: str) -> dict[str, Any]:
        """Exchange a lab API key for an opaque bearer token.

        Returns ``{token, owner, tenant_id, token_id, scopes}`` or raises AuthError.
        """
        if not api_key or not str(api_key).strip():
            raise AuthError("missing api_key")
        key = str(api_key).strip()
        key = key[7:].strip() if key.lower().startswith("bearer ") else key

        owner = self._key_to_owner.get(key)
        if owner is None:
            # Try fingerprint match (handles slight formatting differences)
            for variant in (key, f"Bearer {key}"):
                fp = fingerprint_auth_value(variant)
                if fp and fp in self._fp_to_owner:
                    owner = self._fp_to_owner[fp]
                    break
        if owner is None:
            raise AuthError("unknown api key")

        token_id = secrets.token_urlsafe(12)
        payload = {
            "tid": token_id,
            "owner": owner,
            "tenant": LAB_TENANT,
            "scopes": ["read", "write"],
            "iat": int(time.time()),
        }
        token = self._serializer.dumps(payload)
        return {
            "token": token,
            "owner": owner,
            "tenant_id": LAB_TENANT,
            "token_id": token_id,
            "scopes": ["read", "write"],
        }

    def issue_demo_token(self) -> dict[str, Any]:
        """Issue a demo-tenant bearer token (owner=demo, tenant=demo)."""
        if not self._demo_enabled:
            raise AuthError("demo disabled", status_code=403)
        token_id = secrets.token_urlsafe(12)
        payload = {
            "tid": token_id,
            "owner": self._demo_owner,
            "tenant": DEMO_TENANT,
            "scopes": ["read", "write"],
            "iat": int(time.time()),
        }
        token = self._serializer.dumps(payload)
        return {
            "token": token,
            "owner": self._demo_owner,
            "tenant_id": DEMO_TENANT,
            "token_id": token_id,
            "scopes": ["read", "write"],
        }

    def resolve_token(self, token: str) -> Principal:
        """Validate bearer token and return Principal."""
        if not token or not str(token).strip():
            raise AuthError("missing token")
        try:
            data = self._serializer.loads(str(token).strip())
        except BadSignature as exc:
            raise AuthError("invalid token") from exc
        if not isinstance(data, dict):
            raise AuthError("invalid token")
        owner = str(data.get("owner") or "").strip()
        tenant = str(data.get("tenant") or "").strip()
        token_id = str(data.get("tid") or "").strip()
        scopes_raw = data.get("scopes") or ["read"]
        if not owner or not tenant or not token_id:
            raise AuthError("invalid token payload")
        scopes = frozenset(str(s) for s in scopes_raw)
        return Principal(tenant_id=tenant, owner=owner, scopes=scopes, token_id=token_id)


def get_auth_service(
    *,
    secret: str | None = None,
    mapping_path: str | Path | None = None,
    key_name_map: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    base_dir: Path | str | None = None,
) -> Any:
    """Prefer multi-tenant AuthService; fall back to AuthStub."""
    try:
        from yggdrasil.services.auth_service import AuthService  # type: ignore[attr-defined]

        return AuthService(
            secret=secret,
            mapping_path=mapping_path,
            key_name_map=key_name_map,
            env=env,
            base_dir=base_dir,
        )
    except Exception:
        return AuthStub(
            secret=secret,
            mapping_path=mapping_path,
            key_name_map=key_name_map,
            env=env,
            base_dir=base_dir,
        )
