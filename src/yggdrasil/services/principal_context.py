"""Contextvar-backed principal for request / MCP process scope."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

from yggdrasil.domain.principal import Principal

_current_principal: ContextVar[Principal | None] = ContextVar(
    "yggdrasil_current_principal", default=None
)


def get_principal() -> Principal | None:
    """Return the principal bound to the current context, if any."""
    return _current_principal.get()


def set_principal(principal: Principal | None) -> Token:
    """Set principal; returns a reset token for use with reset_principal."""
    return _current_principal.set(principal)


def reset_principal(token: Token) -> None:
    _current_principal.reset(token)


@contextmanager
def principal_scope(principal: Principal | None) -> Iterator[Principal | None]:
    """Temporarily bind a principal for the duration of the block."""
    tok = set_principal(principal)
    try:
        yield principal
    finally:
        reset_principal(tok)


def require_principal(*, tenancy_enforced: bool) -> Principal | None:
    """When tenancy is enforced, require a non-None principal; else return current or None."""
    principal = get_principal()
    if tenancy_enforced and principal is None:
        from yggdrasil.services.auth_service import AuthError

        raise AuthError("principal required when YGG_TENANCY_MODE=enforced")
    return principal
