"""Intel GPU DRA driver / Kubernetes device plugin installer API.

Distinct from ``llm_d_bench.monitoring.accelerator``, which installs Intel's
xpumd *telemetry* chart on top of an already-present driver: this module
installs the driver/plugin itself (the hardware-access prerequisite), used by
the cluster-creation wizard's Accelerator step.
"""

from .router import router

__all__ = ["router"]
