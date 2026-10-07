"""Aggregate the usage ledger into the admin dashboard payload.

Pure functions: given the filtered ``UsageRecord`` rows (and a range-only set for
filter facets) build totals, a time series and a per-dimension breakdown. Time
bucketing is done in Python so it is dialect-independent; the DAO bounds the row
count with a limit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from llm_d_bench.model_service.contracts import UsageRecord

GROUP_BY = {"model", "user", "cluster"}
INTERVALS = {"hour", "day"}


def _bucket(record: UsageRecord, interval: str) -> str:
    moment = record.created_at
    return moment.strftime("%Y-%m-%dT%H") if interval == "hour" else moment.strftime("%Y-%m-%d")


def _group_key(record: UsageRecord, group_by: str) -> str:
    if group_by == "user":
        return record.user_id or "unknown"
    if group_by == "cluster":
        return record.cluster_id or "unknown"
    return record.group_name or record.model_ref or record.group_id or "unknown"


def _empty() -> dict[str, int]:
    return {"requests": 0, "inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0, "tokens": 0}


def _accumulate(acc: dict[str, int], record: UsageRecord) -> None:
    input_tokens = record.input_tokens or 0
    cached = record.cached_input_tokens or 0
    output = record.output_tokens or 0
    acc["requests"] += record.requests if record.requests is not None else 1
    acc["inputTokens"] += input_tokens
    acc["cachedInputTokens"] += cached
    acc["outputTokens"] += output
    acc["tokens"] += input_tokens + cached + output


def _with_bucket(bucket: str, acc: dict[str, int]) -> dict[str, Any]:
    return {"bucket": bucket, **acc}


def build_usage_analytics(
    records: Sequence[UsageRecord],
    facets: Sequence[UsageRecord],
    *,
    interval: str = "day",
    group_by: str = "model",
    user_labels: Mapping[str, str] | None = None,
    cluster_labels: Mapping[str, str] | None = None,
    cluster_facets: Sequence[UsageRecord] | None = None,
) -> dict[str, Any]:
    """Aggregate ``records`` and derive dropdown facets from ``facets``.

    ``facets`` drives the model/user options and should already be narrowed to
    the caller's reachable clusters (and, for cascading filters, to any
    currently-selected clusters). ``cluster_facets`` drives the cluster
    dropdown itself; it defaults to ``facets`` but callers pass a
    cluster-selection-independent set so picking a cluster never removes other
    clusters from that dropdown.
    """
    interval = interval if interval in INTERVALS else "day"
    group_by = group_by if group_by in GROUP_BY else "model"

    totals = _empty()
    series: dict[str, dict[str, int]] = {}
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        _accumulate(totals, record)
        bucket = _bucket(record, interval)
        _accumulate(series.setdefault(bucket, _empty()), record)
        key = _group_key(record, group_by)
        entry = grouped.setdefault(key, {"totals": _empty(), "series": {}})
        _accumulate(entry["totals"], record)
        _accumulate(entry["series"].setdefault(bucket, _empty()), record)

    def _label(key: str) -> str:
        if group_by == "user" and user_labels:
            return user_labels.get(key, key)
        if group_by == "cluster" and cluster_labels:
            return cluster_labels.get(key, key)
        return key

    groups = [
        {
            "key": key,
            "label": _label(key),
            "totals": entry["totals"],
            "series": [_with_bucket(bucket, entry["series"][bucket]) for bucket in sorted(entry["series"])],
        }
        for key, entry in sorted(grouped.items())
    ]

    model_facets: dict[str, str] = {}
    user_facets: dict[str, str] = {}
    for record in facets:
        if record.group_id:
            model_facets.setdefault(record.group_id, record.group_name or record.group_id)
        if record.user_id:
            user_facets.setdefault(record.user_id, (user_labels or {}).get(record.user_id, record.user_id))

    cluster_facets_map: dict[str, str] = {}
    for record in cluster_facets if cluster_facets is not None else facets:
        if record.cluster_id:
            cluster_facets_map.setdefault(
                record.cluster_id, (cluster_labels or {}).get(record.cluster_id, record.cluster_id)
            )

    def _options(values: dict[str, str]) -> list[dict[str, str]]:
        return [{"key": key, "label": values[key]} for key in sorted(values)]

    return {
        "interval": interval,
        "groupBy": group_by,
        "totals": totals,
        "series": [_with_bucket(bucket, series[bucket]) for bucket in sorted(series)],
        "groups": groups,
        "facets": {
            "models": _options(model_facets),
            "users": _options(user_facets),
            "clusters": _options(cluster_facets_map),
        },
    }
