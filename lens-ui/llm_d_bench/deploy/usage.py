"""Cross-module usage guards for Deploy-owned deployments.

Deleting a deployment must not break work that is actively using it. Consumer
modules (Evaluate today, others later) register a probe here instead of Deploy
importing them, which keeps the dependency direction one-way: consumers depend
on Deploy, never the reverse.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

UsageProbe = Callable[[str], str | None]

_probes: dict[str, UsageProbe] = {}


def register_usage_probe(name: str, probe: UsageProbe) -> None:
    """Register a probe returning a human-readable reason when a deployment is busy."""
    _probes[name] = probe


def active_usage_reason(execution_id: str) -> str | None:
    """Return the first reason a deployment is still in use, if any.

    A failing probe never silently unblocks a delete: it is reported as a
    conflict so a deployment in unknown use is not destroyed.
    """
    for name, probe in _probes.items():
        try:
            reason = probe(execution_id)
        except Exception:
            logger.exception("Deployment usage probe %s failed", name)
            return f"usage of this deployment could not be verified by {name}"
        if reason:
            return reason
    return None
