"""Kubernetes version gating for hardware driver access modes.

A profile may declare a per-mode ``min_kubernetes_version`` under ``driver.modes``
(e.g. Intel DRA and NVIDIA DRA both need modern Kubernetes). Resolving the
cluster's server version and comparing it lets the gpu-driver API fail fast with
an actionable message instead of surfacing a raw Helm/kubectl error.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from llm_d_bench.utils import kubernetes as k8s

if TYPE_CHECKING:
    from .models import HardwareProfile


def parse_version(value: str) -> tuple[int, int, int]:
    parts = [int(re.sub(r"\D", "", part) or 0) for part in str(value).split(".")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


async def server_kubernetes_version(cluster_id: str | None) -> tuple[int, int, int] | None:
    """Cluster server version from ``kubectl version``, or None when unreadable."""
    result = await k8s.scoped_runner(cluster_id).run(["kubectl", "version", "-o", "json"], timeout=15)
    if result.returncode != 0:
        return None
    try:
        server = json.loads(result.stdout).get("serverVersion") or {}
        return parse_version(f"{server.get('major', '0')}.{server.get('minor', '0')}")
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def access_mode_version_message(
    profile: HardwareProfile | None,
    access_mode: str,
    server: tuple[int, int, int] | None,
) -> str | None:
    """Actionable message when the access mode needs a newer Kubernetes, else None."""
    if profile is None or profile.driver is None or server is None:
        return None
    mode = profile.driver.mode(access_mode)
    minimum = mode.min_kubernetes_version if mode is not None else None
    if not minimum or server >= parse_version(minimum):
        return None
    version = f"v{server[0]}.{server[1]}.{server[2]}"
    alternative = "plugin" if access_mode == "dra" else "dra"
    return (
        f"The {access_mode} {profile.display_name} driver requires Kubernetes >= {minimum} "
        f"(cluster is {version}); use the {alternative} access mode instead."
    )
