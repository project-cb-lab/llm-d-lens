"""Errors raised by the GPU driver installer."""

from __future__ import annotations


class GpuDriverError(Exception):
    """Raised when detecting or installing the GPU driver/plugin fails."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
