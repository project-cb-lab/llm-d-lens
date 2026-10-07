"""Turn cumulative llm-d EPP token counters into usage-ledger deltas.

llm-d Gateway Mode has no per-request usage callback: the EPP exposes cumulative
token counters labelled by ``fairness_id`` (the caller identity the Gateway
injects from Lens' ext_authz decision). This module reads those counters from
each cluster's Prometheus, keeps the last snapshot, diffs them, and writes the
delta into ``usage_records`` attributed to the token/user behind the
``fairness_id``. The pure diff/key helpers stay separate from the I/O
orchestration so the attribution rules are unit-testable.

Design reference: ``docs/design/model-service-llmd-routing-design.zh-CN.md``
sections 7-9.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime

from llm_d_bench.db.dao.model_service_usage_snapshot import ModelServiceUsageSnapshotDao
from llm_d_bench.model_service.contracts import (
    EppUsageSnapshot,
    UsageRecordRequest,
    utcnow,
)
from llm_d_bench.model_service.usage_metrics import (
    TOKEN_QUERIES,
    parse_token_query_results,
)

#: Separator inside a snapshot key; cannot appear in model names or token ids.
KEY_SEP = "\x1f"
_COUNTER_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "requests")

#: ``fetch_series(cluster_id, promql) -> instant-query result series``.
SeriesFetcher = Callable[[str, str], Awaitable[list[dict]]]


def snapshot_key(model_name: str, fairness_id: str) -> str:
    return f"{model_name}{KEY_SEP}{fairness_id}"


def parse_snapshot_key(key: str) -> tuple[str, str]:
    model_name, _, fairness_id = key.partition(KEY_SEP)
    return model_name, fairness_id


def to_counters(usage: dict[tuple[str, str], object]) -> dict[str, dict[str, int]]:
    return {
        snapshot_key(item.model_name, item.fairness_id): {field: getattr(item, field) for field in _COUNTER_FIELDS}
        for item in usage.values()
    }


def diff_counters(previous: dict[str, dict[str, int]], current: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Per-key delta from the last snapshot; a counter reset contributes its value.

    EPP counters are cumulative and reset when the EPP restarts, so a field that
    dropped below its previous value is treated as a fresh baseline rather than a
    negative delta.
    """
    deltas: dict[str, dict[str, int]] = {}
    for key, curr in current.items():
        prev = previous.get(key, {})
        deltas[key] = {
            field: (
                curr.get(field, 0) - prev.get(field, 0)
                if curr.get(field, 0) >= prev.get(field, 0)
                else curr.get(field, 0)
            )
            for field in _COUNTER_FIELDS
        }
    return deltas


def _request_id(
    cluster_id: str,
    key: str,
    previous_captured_at: datetime | None,
    counters: dict[str, int],
) -> str:
    raw = f"{cluster_id}|{key}|{previous_captured_at.isoformat() if previous_captured_at else ''}|"
    raw += ",".join(f"{field}={counters.get(field, 0)}" for field in _COUNTER_FIELDS)
    return f"epp-{hashlib.sha256(raw.encode()).hexdigest()[:40]}"


async def _default_fetch_series(cluster_id: str, promql: str) -> list[dict]:
    # Local import breaks the model_service -> monitoring import cycle at module load.
    from llm_d_bench.monitoring.profiling.service import query_prometheus  # noqa: PLC0415

    return await query_prometheus(cluster_id, promql)


class GatewayUsageSync:
    """Read per-cluster EPP counters from Prometheus and write ledger deltas."""

    def __init__(
        self,
        *,
        service,
        snapshots: ModelServiceUsageSnapshotDao | None = None,
        fetch_series: SeriesFetcher | None = None,
    ) -> None:
        self._fetch_series = fetch_series or _default_fetch_series
        self._service = service
        self._snapshots = snapshots or ModelServiceUsageSnapshotDao()

    def _active_cluster_ids(self) -> set[str]:
        return {
            member.cluster_id
            for member in self._service.list_members()
            if member.status == "active" and member.cluster_id
        }

    def _group_for_model(self, model_name: str, cluster_id: str):
        # A model service is scoped to one cluster, so match within that cluster
        # (the same public name may exist in another cluster).
        for group in self._service.list_groups():
            if group.cluster_id != cluster_id:
                continue
            if model_name in {group.served_name, group.base_model, group.model_ref, group.name}:
                return group
        return None

    def _build_request(
        self,
        *,
        cluster_id: str,
        key: str,
        counters: dict[str, int],
        previous_captured_at: datetime | None,
    ) -> UsageRecordRequest:
        model_name, fairness_id = parse_snapshot_key(key)
        token = self._service.tokens.get(fairness_id) if fairness_id else None
        group = self._group_for_model(model_name, cluster_id)
        return UsageRecordRequest(
            request_id=_request_id(cluster_id, key, previous_captured_at, counters),
            token_id=token.id if token else None,
            group_id=group.id if group else None,
            group_name=(group.display_name or group.name) if group else None,
            cluster_id=cluster_id,
            model_ref=group.model_ref if group else model_name,
            input_tokens=counters.get("input_tokens", 0),
            cached_input_tokens=counters.get("cached_input_tokens", 0),
            output_tokens=counters.get("output_tokens", 0),
            requests=counters.get("requests", 0),
            usage_source="engine",
        )

    def _namespaces_for_cluster(self, cluster_id: str) -> list[str]:
        """Deployment namespaces of the cluster's active members.

        Scope each query to these namespaces: a cluster can run several
        deployments of the same model, and only the ones actually published as
        providers should count toward the model service.
        """
        return sorted(
            {
                namespace
                for member in self._service.list_members()
                if member.cluster_id == cluster_id
                and member.status == "active"
                and (namespace := getattr(member, "target_namespace", None))
            }
        )

    async def _cluster_usage(self, cluster_id: str) -> dict[tuple[str, str], object]:
        namespaces = self._namespaces_for_cluster(cluster_id)
        # Kubernetes namespace names are DNS labels (no regex metacharacters).
        selector = '{namespace=~"' + "|".join(namespaces) + '"}' if namespaces else ""
        results: dict[str, list[dict]] = {}
        for field, promql in TOKEN_QUERIES.items():
            scoped = promql[:-1] + selector + ")" if selector else promql
            try:
                results[field] = await self._fetch_series(cluster_id, scoped)
            except Exception:  # noqa: BLE001 - one failed query must not abort the rest
                results[field] = []
        return parse_token_query_results(results)

    async def sync(self, cluster_ids: Iterable[str] | None = None) -> dict[str, int]:
        """Read and record; returns ``{cluster_id: records_written}``."""
        wanted = set(cluster_ids) if cluster_ids is not None else None
        written: dict[str, int] = {}
        for cluster_id in sorted(self._active_cluster_ids()):
            if wanted is not None and cluster_id not in wanted:
                continue
            current = to_counters(await self._cluster_usage(cluster_id))
            previous = self._snapshots.get(cluster_id)
            if previous is None:
                # First reading only establishes the baseline: EPP counters are
                # cumulative since the pod started, so recording them now would
                # blame all pre-existing traffic on the current period.
                self._snapshots.put(EppUsageSnapshot(cluster_id=cluster_id, captured_at=utcnow(), counters=current))
                written[cluster_id] = 0
                continue
            deltas = diff_counters(previous.counters, current)
            count = 0
            for key, delta in deltas.items():
                if not any(delta.get(field, 0) for field in _COUNTER_FIELDS):
                    continue
                self._service.record_usage(
                    self._build_request(
                        cluster_id=cluster_id,
                        key=key,
                        counters=delta,
                        previous_captured_at=previous.captured_at if previous else None,
                    )
                )
                count += 1
            self._snapshots.put(EppUsageSnapshot(cluster_id=cluster_id, captured_at=utcnow(), counters=current))
            written[cluster_id] = count
        return written
