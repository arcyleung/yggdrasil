"""Token store port for opaque API tokens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from yggdrasil.domain.principal import Principal


@dataclass(frozen=True, slots=True)
class TokenRecord:
    token_id: str
    token_hash: str
    tenant_id: str
    owner: str
    scopes: frozenset[str]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    label: str | None = None

    def to_principal(self) -> Principal:
        return Principal(
            tenant_id=self.tenant_id,
            owner=self.owner,
            scopes=self.scopes,
            token_id=self.token_id,
        )


@runtime_checkable
class TokenStore(Protocol):
    def create_token(
        self,
        *,
        tenant_id: str,
        owner: str,
        scopes: frozenset[str] | set[str] | list[str],
        label: str | None = None,
        expires_at: datetime | None = None,
        raw_token: str | None = None,
    ) -> tuple[str, TokenRecord]:
        """Create a token; returns (raw_token, record). Raw is shown once."""
        ...

    def resolve_token(self, raw_token: str) -> Principal | None:
        """Look up principal by raw token (sha256 hash). None if invalid/revoked/expired."""
        ...

    def revoke(self, token_id: str) -> bool:
        """Revoke by token_id. Returns True if a row was updated."""
        ...

    def get_record(self, token_id: str) -> TokenRecord | None: ...
