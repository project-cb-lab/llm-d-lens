"""Built-in Intel XPU hardware provider.

Phase 0 establishes the profile and the entry-point plumbing; driver install,
presence probing and telemetry wiring move here in later phases (see
``docs/design/hardware-plugin-architecture.md``).
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from llm_d_bench.hardware.models import HardwareProfile
from llm_d_bench.hardware.registry import get_profile


def load_profiles() -> list[dict[str, Any]]:
    """Return the bundled Intel profile payload(s)."""
    text = files("llm_d_bench.hardware").joinpath("profiles", "intel_xpu.json").read_text(encoding="utf-8")
    return [json.loads(text)]


class IntelXpuProvider:
    """Intel XPU provider: profile link plus driver install and presence.

    The driver implementation stays in ``monitoring/gpu_driver/service.py``;
    these methods expose it through the provider contract without changing the
    gpu-driver route (which has no vendor parameter).
    """

    def profile(self) -> HardwareProfile:
        return get_profile("intel-xpu")

    async def ensure_presence(self, *, cluster_id: str | None) -> None:
        from llm_d_bench.monitoring.gpu_driver.service import _ensure_intel_gpu_hardware_detected

        await _ensure_intel_gpu_hardware_detected(cluster_id)

    async def driver_status(self, access_mode: str, *, cluster_id: str | None):
        from llm_d_bench.monitoring.gpu_driver.service import _intel_driver_status

        return await _intel_driver_status(access_mode, cluster_id=cluster_id)

    async def install_driver(self, access_mode: str, *, cluster_id: str | None):
        from llm_d_bench.monitoring.gpu_driver.service import _intel_install_driver

        return await _intel_install_driver(access_mode, cluster_id=cluster_id)
