"""Consume a hardware profile's telemetry config for scoped metric queries.

``device_metrics`` (full PromQL) drives the cluster overview. Consumers that
must inject a namespace/pod selector (profiling) or parse scraped text
(evaluate) instead read ``device_metric_sources``: a bare metric name plus a
unit, scale and any required label matchers. A missing/empty entry means the
hardware does not report that metric, so callers skip it (empty == disabled).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import DeviceMetricSource, HardwareProfile


def device_metric_source(profile: HardwareProfile | None, name: str) -> DeviceMetricSource | None:
    """The profile's configured source for ``name``, or None when disabled."""
    if profile is None or profile.telemetry is None:
        return None
    source = profile.telemetry.device_metric_sources.get(name)
    if source is None or not source.metric:
        return None
    return source


def _selector(*matcher_maps: Mapping[str, str] | None) -> str:
    items: dict[str, str] = {}
    for matchers in matcher_maps:
        for key, value in (matchers or {}).items():
            if value:
                items[str(key)] = str(value)
    if not items:
        return ""
    return "{" + ",".join(f'{key}="{value}"' for key, value in items.items()) + "}"


def scoped_device_query(
    source: DeviceMetricSource,
    *,
    aggregation: str,
    match: Mapping[str, str] | None = None,
    by: str | None = None,
) -> str:
    """``<aggregation>(<metric>{<match>}) [by (<by>)] [* <scale>]``."""
    expression = f"{aggregation}({source.metric}{_selector(source.match, match)})"
    if by:
        expression += f" by ({by})"
    if source.scale != 1.0:
        expression += f" * {source.scale:g}"
    return expression


def combined_device_query(
    profiles: Iterable[HardwareProfile],
    name: str,
    *,
    aggregation: str,
    match: Mapping[str, str] | None = None,
    by: str | None = None,
) -> str:
    """OR-combine one metric across every profile that configures it.

    A single cluster runs one vendor, so only one branch returns samples; ``or``
    keeps it a single PromQL expression and ``""`` means no profile declares it.
    """
    expressions = [
        scoped_device_query(source, aggregation=aggregation, match=match, by=by)
        for profile in profiles
        if (source := device_metric_source(profile, name)) is not None
    ]
    if not expressions:
        return ""
    return " or ".join(f"({expression})" for expression in expressions)
