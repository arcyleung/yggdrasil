"""Authenticated principal for multi-tenant access control."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    """Server-side identity bound to every authorized MCP / API call.

    Lab principals share org retrieval scope (tenant_id=lab, any owner).
    Demo principals are isolated to tenant_id=demo only.
    """

    tenant_id: str
    owner: str
    scopes: frozenset[str]
    token_id: str

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def can_read(self) -> bool:
        return self.has_scope("read") or self.has_scope("admin")

    def can_write(self) -> bool:
        return self.has_scope("write") or self.has_scope("admin")
