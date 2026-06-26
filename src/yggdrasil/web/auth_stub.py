"""Deprecated: use yggdrasil.web.auth_factory. Kept for import compatibility."""

from yggdrasil.services.auth_service import AuthError
from yggdrasil.web.auth_factory import WebAuthFacade, build_web_auth

# Back-compat names used by older tests — prefer WebAuthFacade + real tokens
get_auth_service = build_web_auth  # type: ignore[assignment]
AuthStub = WebAuthFacade  # type: ignore[misc, assignment]

__all__ = ["AuthError", "AuthStub", "WebAuthFacade", "build_web_auth", "get_auth_service"]
