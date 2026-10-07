"""Domain errors for the cluster overview service."""

from __future__ import annotations


class ClusterOverviewError(Exception):
    """An expected cluster-overview failure mapped to an HTTP problem response."""

    def __init__(self, message: str, *, status_code: int = 502, code: str = "cluster_request_failed") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

    def problem(self) -> dict[str, object]:
        return {
            "type": "about:blank",
            "status": self.status_code,
            "title": "Cluster request failed",
            "detail": self.message,
            "code": self.code,
            "error": self.message,
        }
