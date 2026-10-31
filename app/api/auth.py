"""
Bearer-token authentication and scope enforcement.

Two principals, configured via environment variables:
  AGENT_API_KEY → principal "agent"  (scopes: context:read)
  OPS_API_KEY   → principal "ops"    (scopes: context:read, findings:read, sync:run, sync:read)

Invalid or missing token → 401 unauthenticated
Authenticated but missing scope → 403 forbidden
X-Correlation-ID is attached to every 401/403 via the APIError handler.
"""
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.api.errors import APIError
from app.core.settings import get_settings

_bearer = HTTPBearer(auto_error=False)

# Scope sets defined here; driven by data_policy.yaml in the PolicyEngine but
# replicated here as a fast lookup for auth middleware (no YAML parse on hot path).
_PRINCIPAL_SCOPES: dict[str, set[str]] = {
    "agent": {"context:read"},
    "ops": {"context:read", "findings:read", "sync:run", "sync:read"},
}


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    settings = get_settings()
    if not credentials:
        raise APIError(401, "unauthenticated", "Missing or invalid Authorization header.")
    token = credentials.credentials
    if token == settings.agent_api_key:
        return Principal("agent", frozenset(_PRINCIPAL_SCOPES["agent"]))
    if token == settings.ops_api_key:
        return Principal("ops", frozenset(_PRINCIPAL_SCOPES["ops"]))
    raise APIError(401, "unauthenticated", "Invalid API key.")


def require_scope(scope: str):
    """FastAPI dependency factory. Raises 403 if the principal lacks the required scope."""
    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.has_scope(scope):
            raise APIError(
                403,
                "forbidden",
                f"This operation requires the '{scope}' scope.",
            )
        return principal
    return _dep
