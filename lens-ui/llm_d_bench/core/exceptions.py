"""Domain exceptions shared by all feature services."""


class DomainError(Exception):
    code = "domain_error"
    status_code = 400


class UnauthenticatedError(DomainError):
    code = "unauthenticated"
    status_code = 401


class SessionExpiredError(UnauthenticatedError):
    code = "session_expired"


class ForbiddenError(DomainError):
    code = "forbidden"
    status_code = 403


class PasswordChangeRequiredError(ForbiddenError):
    code = "password_change_required"


class NotFoundError(DomainError):
    code = "not_found"
    status_code = 404


class ConflictError(DomainError):
    code = "conflict"
    status_code = 409


class DomainValidationError(DomainError):
    code = "validation_failed"
    status_code = 422
