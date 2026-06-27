"""Back-compat re-exports — prefer ``yggdrasil.web.auth_factory``.

Kept so older imports (``AuthStub``) resolve to the real WebAuthFacade.
"""
from yggdrasil.services.auth_service import AuthError
from yggdrasil.web.auth_factory import WebAuthFacade, build_web_auth

# Historical name used before multi-tenant AuthService wiring
AuthStub = WebAuthFacade  # type: ignore[misc, assignment]


def get_auth_service(**kwargs):  # type: ignore[no-untyped-def]
    return build_web_auth(**kwargs)


__all__ = ["AuthError", "AuthStub", "WebAuthFacade", "build_web_auth", "get_auth_service"]
