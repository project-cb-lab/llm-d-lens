"""FastAPI request-context middleware and route-permission dependency.

Node is the primary enforcement point; this is the Python-side defense in
depth (design sections 8.2/8.3). Middleware only resolves a principal (never
raises); the dependency makes the allow/deny decision for the matched route.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from llm_d_bench.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_bench.auth.permissions import ALL_PERMISSIONS
from llm_d_bench.auth.policy import authorize
from llm_d_bench.auth.routes import resolve_route_permission
from llm_d_bench.auth.security import verify_internal
from llm_d_bench.auth.service import default_service
from llm_d_bench.auth.settings import get_settings
from llm_d_bench.core.exceptions import (
    DomainError,
    ForbiddenError,
    PasswordChangeRequiredError,
    UnauthenticatedError,
)

SESSION_COOKIE = "prism_session"
CSRF_COOKIE = "prism_csrf"
CSRF_HEADER = "x-prism-csrf"


class CsrfFailedError(DomainError):
    code = "csrf_failed"
    status_code = 403


def _verify_csrf(request: Request) -> None:
    """Double-submit cookie check (design section 12). Cookie-less clients
    (e.g. Bearer API callers) are exempt; browser sessions always carry it."""
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie:
        return
    if request.headers.get(CSRF_HEADER) != cookie:
        raise CsrfFailedError("CSRF token is missing or does not match")


_PASSWORD_CHANGE_ALLOWED = {
    "/api/v1/auth/password",
    "/api/v1/auth/session",
    "/api/v1/auth/logout",
}


def token_from_request(request: Request) -> str | None:
    """Extract the opaque session token from the cookie or Bearer header."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _disabled_principal() -> Principal:
    return Principal(
        user_id="system",
        username="system",
        bindings=(RoleBinding(role="admin", scope_type=ScopeType.GLOBAL),),
        role_permissions={"admin": frozenset(ALL_PERMISSIONS)},
        role_self_scoped={"admin": frozenset()},
        authz_version=0,
    )


def _internal_principal(request: Request) -> Principal | None:
    principal_id = request.headers.get("x-prism-principal-id")
    timestamp = request.headers.get("x-prism-internal-ts")
    signature = request.headers.get("x-prism-internal-sig")
    if not (principal_id and timestamp and signature):
        return None
    secret = get_settings().internal_auth_secret
    if not secret:
        return None
    try:
        timestamp_value = int(timestamp)
    except ValueError:
        return None
    if not verify_internal(
        secret,
        signature=signature,
        timestamp=timestamp_value,
        method=request.method,
        path=request.url.path,
        principal_id=principal_id,
    ):
        return None
    user = default_service().user_dao.get(principal_id)
    if user is None or user.status != "active":
        return None
    return default_service().principal_for(user)


class AuthContextMiddleware(BaseHTTPMiddleware):
    """Attach the resolved principal (or ``None``) to ``request.state``."""

    async def dispatch(self, request: Request, call_next):
        principal: Principal | None = None
        if get_settings().disabled:
            principal = _disabled_principal()
        else:
            principal = _internal_principal(request)
            if principal is None:
                token = token_from_request(request)
                if token:
                    principal = default_service().validate(token)
        request.state.principal = principal
        request.state.principal_token = token_from_request(request)
        return await call_next(request)


async def require_route_permission(request: Request) -> None:
    """Global dependency: default-deny, with the registry deciding the code."""
    if get_settings().disabled:
        return
    found, permission = resolve_route_permission(request.method, request.url.path)
    if not found:
        raise ForbiddenError("route is not registered for authorization")
    if permission is None:
        return
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        _verify_csrf(request)
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise UnauthenticatedError("authentication required")
    if principal.must_change_password and request.url.path not in _PASSWORD_CHANGE_ALLOWED:
        raise PasswordChangeRequiredError("password change required before continuing")
    if permission == "":
        return
    if not authorize(principal, permission).allowed:
        default_service().audit(
            "access_denied",
            result="deny",
            actor_username=principal.username,
            permission=permission,
            method=request.method,
            path=request.url.path,
            ip=request.client.host if request.client else None,
        )
        raise ForbiddenError("permission denied")
