"""Optional pinning of DRA GPU claims to specific physical devices.

Some shared clusters must reserve a subset of GPUs for other workloads. When
``PRISM_GPU_PCI_ALLOWLIST`` is set (comma-separated PCI addresses, e.g.
``0000:40:00.0,0000:ac:00.0``), every rendered ``gpu.intel.com`` DRA
ResourceClaim is restricted to those devices via a CEL selector instead of
letting the driver pick any of the node's available GPUs.
"""

from __future__ import annotations

import os

from .hardware_profile import device_class

_ALLOWLIST_ENV_VAR = "PRISM_GPU_PCI_ALLOWLIST"


def gpu_pci_allowlist() -> list[str]:
    """PCI addresses this deployment is restricted to, if any."""
    raw = os.environ.get(_ALLOWLIST_ENV_VAR, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def gpu_device_selectors() -> list[dict[str, dict[str, str]]] | None:
    """CEL selector list restricting gpu.intel.com claims to the allowlist, if configured."""
    addresses = gpu_pci_allowlist()
    if not addresses:
        return None
    values = ", ".join(f'"{address}"' for address in addresses)
    expression = f'device.attributes["{device_class()}"].pciAddress in [{values}]'
    return [{"cel": {"expression": expression}}]
