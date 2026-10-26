"""Error types with stable exit codes shared by the CLI and the HTTP API."""

from __future__ import annotations


class LlmdError(Exception):
    """An actionable failure. ``hint`` tells the user what to do next."""

    exit_code = 1
    http_status = 500
    code = "error"

    def __init__(self, message: str, *, hint: str | None = None, details: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details

    def as_problem(self) -> dict:
        problem = {"code": self.code, "message": self.message}
        if self.hint:
            problem["hint"] = self.hint
        if self.details is not None:
            problem["details"] = self.details
        return problem


class UsageError(LlmdError):
    exit_code = 2
    http_status = 400
    code = "invalid_request"


class PolicyError(LlmdError):
    """The plan has blocking findings or failed a cluster preflight."""

    exit_code = 3
    http_status = 422
    code = "plan_blocked"


class ConflictError(LlmdError):
    exit_code = 4
    http_status = 409
    code = "conflict"


class CommandError(LlmdError):
    """kubectl, helm, kustomize, or git failed."""

    exit_code = 5
    http_status = 502
    code = "command_failed"


class NotFoundError(LlmdError):
    exit_code = 6
    http_status = 404
    code = "not_found"


class NotReadyError(LlmdError):
    exit_code = 7
    http_status = 504
    code = "not_ready"


def require(condition: object, message: str, *, hint: str | None = None, error=UsageError) -> None:
    if not condition:
        raise error(message, hint=hint)
