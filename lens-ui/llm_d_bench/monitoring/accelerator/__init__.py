"""Accelerator observability API."""

from . import intel_gpu  # noqa: F401  (registers the Intel GPU provider)
from . import nvidia_gpu  # noqa: F401  (registers the NVIDIA GPU provider)
from .router import router

__all__ = ["router"]
