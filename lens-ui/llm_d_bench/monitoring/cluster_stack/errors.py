"""Domain errors for cluster monitoring stack management."""

from __future__ import annotations


class ClusterStackError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code

    def detail(self) -> dict[str, str | bool]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}
