"""Flow Map data assembly for deployment profiling.

Discovers a deployment's serving components (EPP / prefill / decode) from the
cluster and joins them with live Prometheus rates so the UI can render request
and token flow between hops, plus per-component queue depth.
"""

from __future__ import annotations

from llm_d_bench.monitoring.prometheus import query_vector as _query

import asyncio
import logging
import math
import re
import statistics
import time
from datetime import UTC, datetime

import httpx

from llm_d_bench.monitoring.cluster_stack.service import (
    _PROMETHEUS_PORT,
    _find_service,
    _service_name,
    _service_port,
)
from llm_d_bench.monitoring.deployment.service import (
    CENTRAL_NAMESPACE,
    _deployment_target,
    _discover_epp_service,
)
from llm_d_bench.utils.kubernetes import (
    PortForwardError,
    ensure_port_forward,
    list_resources,
)
from llm_d_bench.hardware.telemetry import combined_device_query
from .models import (
    FlowMapComponent,
    FlowMapEdge,
    FlowMapInstance,
    FlowMapResponse,
)
from .xpu_metrics import XPUM_QUERIES, XPUM_SOURCE, aggregate_xpum, device_allocations

logger = logging.getLogger(__name__)

COMPONENT_LABELS = {
    "epp": "Endpoint Picker (EPP)",
    "prefill": "Prefill",
    "decode": "Decode",
}
FLOW_ORDER = ["epp", "prefill", "decode"]
# PromQL lookback range, derived from the deployment monitors' scrape interval
# (monitoring/deployment/service.py SCRAPE_INTERVAL = 5s). ``irate``/``rate`` need at least
# two samples in the range to produce a value — a range equal to the scrape
# interval regularly contains only one sample (the sample at the range's left
# edge is excluded), which yields an empty result and reads as 0. Two scrapes
# (10s) is the minimum that reliably returns a rate; the value still refreshes
# on every 5s scrape, so freshness is unchanged.
RATE_WINDOW = "10s"
# Failure counters are sparse — a finished_reason=abort|error increment may
# only land on every Nth scrape. ``rate()`` averages over the range, so use
# the same 2-scrape range as ``RATE_WINDOW``: it spans two samples reliably
# (no empty result) while holding a failure visible for ~10s so none is
# dropped.
FAIL_RATE_WINDOW = "10s"
# vLLM's ``request_success_total`` counter is labelled by ``finished_reason``:
# stop/length/repetition are normal completions; abort/error are failures.
SUCCESS_REASONS = "stop|length|repetition"
FAIL_REASONS = "abort|error"

# Component discovery shells out to ``kubectl`` several times per request.
# Cache the result briefly so the sub-second realtime polling loop doesn't pay
# that subprocess cost on every refresh.
_COMPONENT_CACHE_TTL = 15.0
_components_cache: dict[tuple[str, str | None], tuple[float, dict[str, list[str]]]] = {}


async def _prometheus_local_port(cluster_id: str | None) -> int | None:
    """Open (or reuse) a tunnel to the central Prometheus and return its port."""
    services = await list_resources("services", namespace=CENTRAL_NAMESPACE, cluster_id=cluster_id)
    prom = _find_service(services, lambda name: name.endswith("-prometheus"))
    if prom is None:
        return None
    service = _service_name(prom)
    port = _service_port(prom, _PROMETHEUS_PORT)
    try:
        forward = await ensure_port_forward(CENTRAL_NAMESPACE, service, port, cluster_id=cluster_id)
        return forward.local_port
    except PortForwardError:
        logger.warning("Unable to open Prometheus tunnel for flow map", exc_info=True)
        return None




async def _query_range(
    client: httpx.AsyncClient,
    promql: str,
    start: str,
    end: str,
    step_seconds: int,
) -> list[dict]:
    """Execute a Prometheus range query and return its matrix result."""
    try:
        response = await client.get(
            "/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step_seconds},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    if payload.get("status") != "success":
        return []
    return (payload.get("data") or {}).get("result") or []


async def query_prometheus(cluster_id: str | None, promql: str) -> list[dict]:
    """Run an instant PromQL query against the cluster's central Prometheus.

    Opens (or reuses) the port-forward tunnel to the ``*-prometheus`` Service and
    returns the raw result series; ``[]`` when Prometheus is unreachable or the
    query fails. Shared by the profiling flow map and the model-service usage sync.
    """
    local_port = await _prometheus_local_port(cluster_id)
    if local_port is None:
        return []
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{local_port}", timeout=15.0) as client:
        return await _query(client, promql)


def _iso_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cache_config_snapshot(results: list[dict]) -> dict:
    """Extract numeric vLLM cache capacity labels from its info gauge."""
    numeric_labels = (
        "block_size",
        "num_gpu_blocks",
        "num_cpu_blocks",
        "kv_cache_size_tokens",
        "kv_cache_max_concurrency",
        "gpu_memory_utilization",
        "kv_cache_memory_bytes",
        "cpu_kvcache_space_bytes",
        "cpu_offload_gb",
        "swap_space_bytes",
    )
    engines = []
    seen = set()
    for entry in results:
        labels = entry.get("metric") or {}
        identity = (labels.get("pod"), labels.get("instance"), labels.get("engine"))
        if identity in seen:
            continue
        seen.add(identity)
        item = {key: labels.get(key) for key in ("pod", "instance", "engine") if labels.get(key) is not None}
        for key in numeric_labels:
            raw = labels.get(key)
            if raw in (None, "", "None", "none", "null", "NaN"):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            item[key] = int(value) if value.is_integer() else value
        if any(key in item for key in numeric_labels):
            engines.append(item)

    gpu_tokens = sum(
        item["kv_cache_size_tokens"] for item in engines if isinstance(item.get("kv_cache_size_tokens"), (int, float))
    )
    cpu_tokens = sum(
        item["num_cpu_blocks"] * item["block_size"]
        for item in engines
        if isinstance(item.get("num_cpu_blocks"), (int, float)) and isinstance(item.get("block_size"), (int, float))
    )
    return {
        "source": "vllm:cache_config_info",
        "engines": engines,
        **({"hbm_capacity_tokens": gpu_tokens} if gpu_tokens > 0 else {}),
        **({"cpu_capacity_tokens": cpu_tokens} if cpu_tokens > 0 else {}),
        **({"effective_capacity_tokens": gpu_tokens + cpu_tokens} if gpu_tokens + cpu_tokens > 0 else {}),
    }


def _range_points(results: list[dict]) -> dict[float, float]:
    """Sum finite samples across every series at each timestamp."""
    points: dict[float, float] = {}
    for entry in results:
        for sample in entry.get("values") or []:
            if len(sample) < 2:
                continue
            try:
                timestamp, number = float(sample[0]), float(sample[1])
            except (TypeError, ValueError):
                continue
            if math.isnan(number) or math.isinf(number):
                continue
            points[timestamp] = points.get(timestamp, 0.0) + number
    return points


def _range_series(results: list[dict], identity_labels: tuple[str, ...]) -> dict[str, dict[float, float]]:
    """Preserve labelled range vectors instead of collapsing every pod into one sum."""
    series: dict[str, dict[float, float]] = {}
    for index, entry in enumerate(results):
        labels = entry.get("metric") or {}
        identity = next((str(labels.get(key)) for key in identity_labels if labels.get(key)), f"series-{index + 1}")
        points: dict[float, float] = {}
        for sample in entry.get("values") or []:
            if len(sample) < 2:
                continue
            try:
                timestamp, number = float(sample[0]), float(sample[1])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            points[timestamp] = points.get(timestamp, 0.0) + number
        if points:
            target = series.setdefault(identity, {})
            for timestamp, number in points.items():
                target[timestamp] = target.get(timestamp, 0.0) + number
    return series


def _coefficient_of_variation(values: list[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    if len(finite) < 2:
        return 0.0 if finite else None
    mean = statistics.fmean(finite)
    return round(statistics.pstdev(finite) / mean, 5) if mean else None


def _summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return ordered[index]

    return {
        "mean": statistics.fmean(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": ordered[-1],
    }


def _scalar(results: list[dict]) -> float | None:
    """Sum a Prometheus instant-query result set, ignoring NaN/Inf samples."""
    total = 0.0
    found = False
    for entry in results:
        value = entry.get("value")
        if not value or len(value) < 2:
            continue
        try:
            number = float(value[1])
        except (TypeError, ValueError):
            continue
        if math.isnan(number) or math.isinf(number):
            continue
        total += number
        found = True
    return total if found else None


def _scalar_map(results: list[dict], label: str) -> dict[str, float]:
    """Group a Prometheus result set by a label, summing each group's value."""
    out: dict[str, float] = {}
    for entry in results:
        value = entry.get("value")
        if not value or len(value) < 2:
            continue
        try:
            number = float(value[1])
        except (TypeError, ValueError):
            continue
        if math.isnan(number) or math.isinf(number):
            continue
        metric = entry.get("metric") or {}
        key = str(metric.get(label) or "")
        if not key:
            continue
        out[key] = out.get(key, 0.0) + number
    return out


def _pod_role(pod: dict) -> str | None:
    metadata = pod.get("metadata") or {}
    labels = dict(metadata.get("labels") or {})
    role = str(labels.get("llm-d.ai/role") or "")
    if role in ("prefill", "decode"):
        return role
    name = str(metadata.get("name") or "").lower()
    if "prefill" in name:
        return "prefill"
    if "decode" in name:
        return "decode"
    return None


async def _discover_components(namespace: str, cluster_id: str | None) -> dict[str, list[str]]:
    key = (namespace, cluster_id)
    cached = _components_cache.get(key)
    if cached is not None:
        cached_at, instances = cached
        if time.monotonic() - cached_at < _COMPONENT_CACHE_TTL:
            return instances

    instances = await _discover_components_uncached(namespace, cluster_id)
    _components_cache[key] = (time.monotonic(), instances)
    return instances


async def _discover_components_uncached(namespace: str, cluster_id: str | None) -> dict[str, list[str]]:
    instances: dict[str, list[str]] = {"epp": [], "prefill": [], "decode": []}
    pods = await list_resources("pods", namespace=namespace, cluster_id=cluster_id)
    for pod in pods:
        role = _pod_role(pod)
        name = str((pod.get("metadata") or {}).get("name") or "")
        if role and name:
            instances[role].append(name)
    epp = await _discover_epp_service(namespace, cluster_id)
    if epp and epp.get("name"):
        instances["epp"] = [str(epp["name"])]
    for role in FLOW_ORDER:
        instances[role] = sorted(set(instances[role]))
    return instances


def _sum_metrics(metrics_by_name: dict[str, dict[str, float | None]]) -> dict[str, float | None]:
    """Sum per-instance metrics into component-level totals."""
    totals: dict[str, float | None] = {}
    for metrics in metrics_by_name.values():
        for field, value in metrics.items():
            if value is None:
                continue
            totals[field] = (totals.get(field) or 0.0) + value
    return totals


def _avg_metrics(
    metrics_by_name: dict[str, dict[str, float | None]],
    fields: tuple[str, ...],
) -> dict[str, float | None]:
    """Average per-instance ratio/percentage metrics into component totals.

    Rates (req/s, tok/s) are additive across replicas, but percentages and hit
    ratios are not — averaging keeps a component-level value within 0..100.
    """
    totals: dict[str, float | None] = {}
    for field in fields:
        values = [metrics[field] for metrics in metrics_by_name.values() if metrics.get(field) is not None]
        totals[field] = (sum(values) / len(values)) if values else None
    return totals


def _cache_queries(matcher: str) -> dict[str, str]:
    """PromQL for GPU KV-cache usage% and prefix-cache hit rates (per pod).

    ``vllm:kv_cache_usage_perc`` is a 0..1 ratio (help text: "1 means 100
    percent"), so scale by 100. Prefix-cache hit rates are counter ratios over
    the same lookback window as the request/token rates; when there are no
    queries in the window the pod simply has no sample (reads as None).
    """
    return {
        "kv_cache_usage_perc": f"sum(vllm:kv_cache_usage_perc{matcher}) by (pod) * 100",
        "prefix_cache_hit_rate": (
            f"sum by (pod) (irate(vllm:prefix_cache_hits_total{matcher}[{RATE_WINDOW}]))"
            f" / sum by (pod) (irate(vllm:prefix_cache_queries_total{matcher}[{RATE_WINDOW}]))"
            f" * 100"
        ),
        "external_prefix_cache_hit_rate": (
            f"sum by (pod) (irate(vllm:external_prefix_cache_hits_total{matcher}[{RATE_WINDOW}]))"
            f" / sum by (pod) (irate(vllm:external_prefix_cache_queries_total{matcher}[{RATE_WINDOW}]))"
            f" * 100"
        ),
    }


# Prometheus matches with RE2, which rejects escapes it has no meaning for
# (notably "\-", which re.escape emits). Escape only the true metacharacters;
# "-" is not one outside a character class.
_RE2_META = re.compile(r"([\\.+*?()|\[\]{}^$])")


def _re2_escape(value: str) -> str:
    return _RE2_META.sub(r"\\\1", value)


def _epp_phase_queries(
    namespace: str,
    role_instances: dict[str, list[str]],
) -> dict[str, str]:
    """PromQL splitting the EPP's in-flight requests across pipeline phases.

    The EPP publishes no per-phase in-flight counter: llm_d_epp_request_running
    carries only fairness_id/priority/model_name, and its one endpoint-labelled
    gauge (llm_d_epp_per_endpoint_queue_size) mirrors the servers' *waiting*
    queues, which sit at zero whenever the pipeline keeps up — it says nothing
    about which phase the work is in. Counting each role's own waiting plus
    running gauges does, and in steady state the two sum to request_running
    exactly. They come from the model servers rather than the EPP, so a scrape
    landing mid-ramp can leave the parts briefly out of step with the total;
    the values are reported as observed rather than rescaled to fit.
    """
    promqls: dict[str, str] = {}
    for role, key in (("prefill", "prefill_inflight"), ("decode", "decode_inflight")):
        pods = role_instances.get(role) or []
        if not pods:
            continue
        pod_re = "|".join(_re2_escape(pod) for pod in pods)
        matcher = f'{{namespace="{namespace}", pod=~"{pod_re}"}}'
        promqls[key] = f"sum(vllm:num_requests_waiting{matcher}) + sum(vllm:num_requests_running{matcher})"
    return promqls


async def _role_queries(
    client: httpx.AsyncClient,
    namespace: str,
    role: str,
    instance_names: list[str],
    role_instances: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, float | None]]:
    """Run a role's PromQL queries concurrently; return per-instance metrics.

    Keys are pod names (for prefill/decode) or the single EPP service name.
    Each role is queried against its own pods only, so a node's metrics always
    reflect that pod's own observed activity.
    """
    if role == "epp":
        matcher = f'{{namespace="{namespace}"}}'
        _epp_rate = f"sum(irate(llm_d_epp_request_total{matcher}[{RATE_WINDOW}]))"
        promqls = {
            # The EPP is a pass-through router, not a queueing stage: it holds
            # no admission buffer of its own (its only queue gauges,
            # llm_d_epp_average_queue_size / _per_endpoint_queue_size, are its
            # *view of the model servers'* queues, labelled by
            # model_server_endpoint) and dispatches every request downstream
            # immediately. llm_d_epp_request_total is incremented on receipt,
            # so arrival == throughput == outflow and all three intentionally
            # share one query. That is why the users -> epp and epp -> prefill
            # hops always carry the same rate; it is conservation, not a bug.
            "request_rate": _epp_rate,
            "arrival_request_rate": _epp_rate,
            "success_request_rate": _epp_rate,
            # In-flight (dispatched, still streaming), NOT queued-and-waiting.
            "queue_length": f"sum(llm_d_epp_request_running{matcher})",
            # Which phase that in-flight work is in (see _epp_phase_queries).
            **_epp_phase_queries(namespace, role_instances or {}),
            "input_token_rate": (f"sum(irate(llm_d_epp_request_input_tokens_sum{matcher}[{RATE_WINDOW}]))"),
            "output_token_rate": (f"sum(irate(llm_d_epp_request_output_tokens_sum{matcher}[{RATE_WINDOW}]))"),
        }
        keys = list(promqls)
        results = await asyncio.gather(*(_query(client, promqls[key]) for key in keys))
        name = instance_names[0] if instance_names else "epp"
        values = {key: _scalar(result) for key, result in zip(keys, results, strict=False)}
        # queue_length / prefill_inflight / decode_inflight are current-state
        # gauges (in-flight depth), not traffic counters: an idle EPP with no
        # in-flight work is a real "0", not "no sample". Without this default
        # they would only ever render once some request had passed through,
        # even though the gauge itself carries a value the whole time.
        for field in ("queue_length", "prefill_inflight", "decode_inflight"):
            if field in values and values[field] is None:
                values[field] = 0.0
        return {name: values}

    pod_re = "|".join(_re2_escape(pod) for pod in instance_names)
    matcher = f'{{namespace="{namespace}", pod=~"{pod_re}"}}'
    promqls = {
        "request_rate": (f"sum(irate(vllm:request_success_total{matcher}[{RATE_WINDOW}])) by (pod)"),
        # vLLM only exposes a *completion* counter (request_success_total), not
        # an arrival counter. Reconstruct the true ingress via flow
        # conservation: arrival = outflow (all finished_reason) + the rate of
        # change of in-flight depth (waiting + running). The derivative terms
        # capture a burst the instant it lands in the queue, even before any
        # request completes — this is what makes users -> stage exceed
        # stage -> output while the queue absorbs a burst.
        "arrival_request_rate": (
            f"sum by (pod) (irate(vllm:request_success_total{matcher}[{RATE_WINDOW}]))"
            f" + sum by (pod) (deriv(vllm:num_requests_waiting{matcher}[{RATE_WINDOW}]))"
            f" + sum by (pod) (deriv(vllm:num_requests_running{matcher}[{RATE_WINDOW}]))"
        ),
        "success_request_rate": (
            f'sum(irate(vllm:request_success_total{{namespace="{namespace}", '
            f'pod=~"{pod_re}", finished_reason=~"{SUCCESS_REASONS}"}}[{RATE_WINDOW}])) by (pod)'
        ),
        "failed_request_rate": (
            f'sum(rate(vllm:request_success_total{{namespace="{namespace}", '
            f'pod=~"{pod_re}", finished_reason=~"{FAIL_REASONS}"}}[{FAIL_RATE_WINDOW}])) by (pod)'
        ),
        "client_timeout_rate": (
            f'sum(rate(vllm:request_success_total{{namespace="{namespace}", '
            f'pod=~"{pod_re}", finished_reason="abort"}}[{FAIL_RATE_WINDOW}])) by (pod)'
        ),
        "backend_error_rate": (
            f'sum(rate(vllm:request_success_total{{namespace="{namespace}", '
            f'pod=~"{pod_re}", finished_reason="error"}}[{FAIL_RATE_WINDOW}])) by (pod)'
        ),
        "queue_length": f"sum(vllm:num_requests_waiting{matcher}) by (pod)",
        "input_token_rate": (f"sum(irate(vllm:prompt_tokens_total{matcher}[{RATE_WINDOW}])) by (pod)"),
        "output_token_rate": (f"sum(irate(vllm:generation_tokens_total{matcher}[{RATE_WINDOW}])) by (pod)"),
    }
    promqls.update(_cache_queries(matcher))
    keys = list(promqls)
    results = await asyncio.gather(*(_query(client, promqls[key]) for key in keys))
    # Every discovered pod gets an entry up front (not just pods that show up
    # in at least one PromQL result), so a pod that has processed zero
    # requests still surfaces its gauges as 0 instead of vanishing from the
    # node entirely because it never appeared in any vector.
    by_pod: dict[str, dict[str, float | None]] = {pod: {} for pod in instance_names}
    for key, result in zip(keys, results, strict=False):
        for pod, value in _scalar_map(result, "pod").items():
            by_pod.setdefault(pod, {})[key] = value
    # queue_length / kv_cache_usage_perc are current-state gauges, and cache
    # hit rates are counter ratios; when a pod has taken no traffic in the
    # window these read as "no sample" (0/0 -> NaN, or the gauge series never
    # appearing in the vector), not a genuine outage. Surface 0 so the node
    # still shows the metric instead of silently dropping it — a pod with no
    # requests genuinely has an empty queue, an unused KV cache, and zero
    # cache hits.
    for metrics in by_pod.values():
        for field in (
            "queue_length",
            "kv_cache_usage_perc",
            "prefix_cache_hit_rate",
            "external_prefix_cache_hit_rate",
        ):
            if metrics.get(field) is None:
                metrics[field] = 0.0
    return by_pod


async def _component_latency(client: httpx.AsyncClient, matcher: str) -> float | None:
    """Average e2e request latency (ms) across a role's pods."""
    latency_sum, latency_count = await asyncio.gather(
        _query(
            client,
            f"sum(irate(vllm:e2e_request_latency_seconds_sum{matcher}[{RATE_WINDOW}]))",
        ),
        _query(
            client,
            f"sum(irate(vllm:e2e_request_latency_seconds_count{matcher}[{RATE_WINDOW}]))",
        ),
    )
    total = _scalar(latency_sum)
    count = _scalar(latency_count)
    if total is not None and count:
        return (total / count) * 1000
    return None


async def wait_for_deployment_metrics(
    execution_id: str,
    cluster_id: str | None = None,
    timeout_seconds: float = 20.0,
) -> bool:
    """Wait for a healthy deployment scrape; zero timeout performs one query.

    Discovery/tunnel setup precedes the polling budget. Callers needing a hard
    end-to-end deadline must also bound this coroutine with asyncio.timeout.
    """
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    local_port = await _prometheus_local_port(cluster_id)
    if local_port is None:
        return False
    deadline = time.monotonic() + timeout_seconds
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{local_port}", timeout=5.0) as client:
        while True:
            if _scalar(await _query(client, f'sum(up{{namespace="{namespace}"}} == 1)')):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(2.5, max(0.0, deadline - time.monotonic())))


def _hardware_profiles() -> list:
    """Registered hardware profiles; empty when discovery is unavailable."""
    try:
        from llm_d_bench.hardware.registry import all_profiles

        return all_profiles()
    except Exception:  # pragma: no cover - profiling must not fail on discovery errors
        return []


# Device-metric keys shared with the cluster overview / evaluate normalized
# fields, mapped to the profile's ``device_metric_sources`` entry that provides
# them and the aggregation the flow map uses.
_DEVICE_METRIC_QUERIES = (
    ("gpu_utilization_percent", "utilization", "avg"),
    ("gpu_framebuffer_used_bytes", "framebuffer_used", "sum"),
)


def _direct_profiles(profiles: list) -> list:
    """Profiles whose device metrics are exporter-pod-labelled (not DRA-joined).

    ``allocation_join == "dra"`` hardware (Intel XPUM) is instead resolved by
    ``aggregate_xpum`` against exclusive DRA allocations, so it must not also be
    queried by a bare namespace selector (its series carry no namespace/pod).
    """
    result = []
    for profile in profiles:
        telemetry = getattr(profile, "telemetry", None)
        if telemetry is not None and telemetry.allocation_join != "dra":
            result.append(profile)
    return result


def _device_queries(profiles: list, namespace: str, *, by: str | None = None) -> dict[str, str]:
    """Profile-driven device PromQL for this namespace; empty keys are disabled."""
    queries: dict[str, str] = {}
    for key, source_name, aggregation in _DEVICE_METRIC_QUERIES:
        query = combined_device_query(
            _direct_profiles(profiles), source_name, aggregation=aggregation, match={"namespace": namespace}, by=by
        )
        if query:
            queries[key] = query
    return queries


async def collect_benchmark_observability(
    execution_id: str,
    start: str,
    end: str,
    cluster_id: str | None = None,
) -> dict:
    """Collect deployment runtime metrics over an exact benchmark traffic window."""
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    started = _iso_timestamp(start)
    finished = _iso_timestamp(end)
    duration_seconds = max(1.0, (finished - started).total_seconds())
    step_seconds = max(5, math.ceil(duration_seconds / 240))
    local_port = await _prometheus_local_port(cluster_id)
    if local_port is None:
        return {
            "status": "unavailable",
            "reason": "Prometheus is not reachable",
            "window": {"start": start, "end": end},
            "series": [],
            "summary": {},
        }
    try:
        components = await _discover_components(namespace, cluster_id)
    except Exception:
        logger.warning("Unable to classify benchmark pods by serving role", exc_info=True)
        components = {role: [] for role in FLOW_ORDER}
    role_by_pod = {pod: role for role, pods in components.items() for pod in pods}

    try:
        pods, claims, slices = await asyncio.wait_for(
            asyncio.gather(
                list_resources("pods", namespace=namespace, cluster_id=cluster_id),
                list_resources("resourceclaims.resource.k8s.io", namespace=namespace, cluster_id=cluster_id),
                list_resources("resourceslices.resource.k8s.io", cluster_id=cluster_id),
            ),
            timeout=15.0,
        )
        xpu_allocations = device_allocations(pods, claims, slices, namespace)
    except Exception:
        logger.warning("Unable to map benchmark Intel device allocations", exc_info=True)
        xpu_allocations = {}

    matcher = f'{{namespace="{namespace}"}}'
    queries = {
        "request_rate_rps": f"sum(rate(vllm:request_success_total{matcher}[30s]))",
        "input_token_rate_tps": f"sum(rate(vllm:prompt_tokens_total{matcher}[30s]))",
        "output_token_rate_tps": f"sum(rate(vllm:generation_tokens_total{matcher}[30s]))",
        "queue_depth": f"sum(vllm:num_requests_waiting{matcher})",
        "running_requests": f"sum(vllm:num_requests_running{matcher})",
        "kv_cache_usage_percent": f"avg(vllm:kv_cache_usage_perc{matcher}) * 100",
        "gpu_cache_usage_percent": f"avg(vllm:gpu_cache_usage_perc{matcher}) * 100",
        "cpu_cache_usage_percent": f"avg(vllm:cpu_cache_usage_perc{matcher}) * 100",
        "gpu_memory_usage_bytes": f"sum(vllm:gpu_memory_usage_bytes{matcher})",
        "cpu_memory_usage_bytes": f"sum(vllm:cpu_memory_usage_bytes{matcher})",
        "prefix_cache_hit_percent": (
            f"sum(rate(vllm:prefix_cache_hits_total{matcher}[30s]))"
            f" / sum(rate(vllm:prefix_cache_queries_total{matcher}[30s])) * 100"
        ),
        "external_prefix_cache_hit_percent": (
            f"sum(rate(vllm:external_prefix_cache_hits_total{matcher}[30s]))"
            f" / sum(rate(vllm:external_prefix_cache_queries_total{matcher}[30s])) * 100"
        ),
        "epp_inflight_requests": f"sum(llm_d_epp_request_running{matcher})",
        "failed_request_rate_rps": (
            f'sum(rate(vllm:request_success_total{{namespace="{namespace}", finished_reason=~"{FAIL_REASONS}"}}[30s]))'
        ),
        "nixl_transfer_rate_rps": f"sum(rate(vllm:nixl_xfer_time_seconds_count{matcher}[30s]))",
        "nixl_failed_transfer_rate_rps": f"sum(rate(vllm:nixl_num_failed_transfers{matcher}[30s]))",
        "nixl_failed_notification_rate_rps": f"sum(rate(vllm:nixl_num_failed_notifications{matcher}[30s]))",
        "nixl_expired_request_rate_rps": f"sum(rate(vllm:nixl_num_kv_expired_reqs{matcher}[30s]))",
        "nixl_transfer_bytes_per_second": f"sum(rate(vllm:nixl_bytes_transferred_sum{matcher}[30s]))",
        "nixl_transfer_latency_p50_ms": (
            f"histogram_quantile(0.50, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "nixl_transfer_latency_p95_ms": (
            f"histogram_quantile(0.95, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "nixl_transfer_latency_p99_ms": (
            f"histogram_quantile(0.99, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "kv_offload_bytes_per_second": (
            f'sum(rate(vllm:kv_offload_total_bytes{{namespace="{namespace}", transfer_type=~"GPU_to_.*"}}[30s]))'
        ),
        "kv_restore_bytes_per_second": (
            f'sum(rate(vllm:kv_offload_total_bytes{{namespace="{namespace}", transfer_type=~".*_to_GPU"}}[30s]))'
        ),
        "kv_offload_time_seconds_per_second": (
            f'sum(rate(vllm:kv_offload_total_time{{namespace="{namespace}", transfer_type=~"GPU_to_.*"}}[30s]))'
        ),
        "kv_restore_time_seconds_per_second": (
            f'sum(rate(vllm:kv_offload_total_time{{namespace="{namespace}", transfer_type=~".*_to_GPU"}}[30s]))'
        ),
        "nixl_transfer_rate_rps": f'sum(rate(vllm:nixl_xfer_time_seconds_count{matcher}[30s]))',
        "nixl_failed_transfer_rate_rps": f'sum(rate(vllm:nixl_num_failed_transfers{matcher}[30s]))',
        "nixl_failed_notification_rate_rps": f'sum(rate(vllm:nixl_num_failed_notifications{matcher}[30s]))',
        "nixl_expired_request_rate_rps": f'sum(rate(vllm:nixl_num_kv_expired_reqs{matcher}[30s]))',
        "nixl_transfer_bytes_per_second": f'sum(rate(vllm:nixl_bytes_transferred_sum{matcher}[30s]))',
        "nixl_transfer_latency_p50_ms": f'histogram_quantile(0.50, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000',
        "nixl_transfer_latency_p95_ms": f'histogram_quantile(0.95, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000',
        "nixl_transfer_latency_p99_ms": f'histogram_quantile(0.99, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000',
        "kv_offload_bytes_per_second": f'sum(rate(vllm:kv_offload_total_bytes{{namespace="{namespace}", transfer_type=~"GPU_to_.*"}}[30s]))',
        "kv_restore_bytes_per_second": f'sum(rate(vllm:kv_offload_total_bytes{{namespace="{namespace}", transfer_type=~".*_to_GPU"}}[30s]))',
        "kv_offload_time_seconds_per_second": f'sum(rate(vllm:kv_offload_total_time{{namespace="{namespace}", transfer_type=~"GPU_to_.*"}}[30s]))',
        "kv_restore_time_seconds_per_second": f'sum(rate(vllm:kv_offload_total_time{{namespace="{namespace}", transfer_type=~".*_to_GPU"}}[30s]))',
        "network_receive_bytes_per_second": f'sum(rate(container_network_receive_bytes_total{{namespace="{namespace}", pod!=""}}[30s]))',
        "network_transmit_bytes_per_second": f'sum(rate(container_network_transmit_bytes_total{{namespace="{namespace}", pod!=""}}[30s]))',
    }
    # Engine-side token accounting (vLLM v1). Missing exporter series stay
    # unavailable; router cached-token estimates must never substitute here.
    # https://docs.vllm.ai/en/v0.18.1/usage/metrics/
    engine_token_sources = {
        "engine_prompt_local_compute_tps": "local_compute",
        "engine_prompt_local_cache_tps": "local_cache_hit",
        "engine_prompt_external_tps": "external_kv_transfer",
    }
    queries.update(
        {
            key: (f'sum(rate(vllm:prompt_tokens_by_source_total{{namespace="{namespace}", source="{source}"}}[30s]))')
            for key, source in engine_token_sources.items()
        }
    )
    queries["engine_prompt_recomputed_tps"] = f"sum(rate(vllm:prompt_tokens_recomputed_total{matcher}[30s]))"
    profiles = _hardware_profiles()
    per_pod_queries = {
        "request_rate_rps": f"sum(rate(vllm:request_success_total{matcher}[30s])) by (pod)",
        "input_token_rate_tps": f"sum(rate(vllm:prompt_tokens_total{matcher}[30s])) by (pod)",
        "running_requests": f"sum(vllm:num_requests_running{matcher}) by (pod)",
        "waiting_requests": f"sum(vllm:num_requests_waiting{matcher}) by (pod)",
        "output_token_rate_tps": f"sum(rate(vllm:generation_tokens_total{matcher}[30s])) by (pod)",
        "kv_cache_usage_percent": f"avg(vllm:kv_cache_usage_perc{matcher}) by (pod) * 100",
        "prefix_cache_hit_percent": (
            f"sum(rate(vllm:prefix_cache_hits_total{matcher}[30s])) by (pod)"
            f" / sum(rate(vllm:prefix_cache_queries_total{matcher}[30s])) by (pod) * 100"
        ),
        "gpu_cache_usage_percent": f"avg(vllm:gpu_cache_usage_perc{matcher}) by (pod) * 100",
        "cpu_cache_usage_percent": f"avg(vllm:cpu_cache_usage_perc{matcher}) by (pod) * 100",
        "gpu_memory_usage_bytes": f"sum(vllm:gpu_memory_usage_bytes{matcher}) by (pod)",
        "cpu_memory_usage_bytes": f"sum(vllm:cpu_memory_usage_bytes{matcher}) by (pod)",
        "cpu_usage_cores": (
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}", container!="", '
            f'image!=""}}[30s])) by (pod)'
        ),
        "memory_working_set_bytes": (
            f'sum(container_memory_working_set_bytes{{namespace="{namespace}", container!="", image!=""}}) by (pod)'
        ),
        "filesystem_read_bytes_per_second": (
            f'sum(rate(container_fs_reads_bytes_total{{namespace="{namespace}", container!="", '
            f'image!=""}}[30s])) by (pod)'
        ),
        "filesystem_write_bytes_per_second": (
            f'sum(rate(container_fs_writes_bytes_total{{namespace="{namespace}", container!="", '
            f'image!=""}}[30s])) by (pod)'
        ),
        "network_receive_bytes_per_second": (
            f'sum(rate(container_network_receive_bytes_total{{namespace="{namespace}", pod!=""}}[30s])) by (pod)'
        ),
        "network_transmit_bytes_per_second": (
            f'sum(rate(container_network_transmit_bytes_total{{namespace="{namespace}", pod!=""}}[30s])) by (pod)'
        ),
    }
    router_queries = {
        "request_rate_rps": f"sum(rate(llm_d_epp_request_total{matcher}[30s]))",
        "input_token_rate_tps": f"sum(rate(llm_d_epp_request_input_tokens_sum{matcher}[30s]))",
        "cached_token_rate_tps": f"sum(rate(llm_d_epp_request_cached_tokens_sum{matcher}[30s]))",
        "scheduler_attempt_rate_rps": f"sum(rate(llm_d_epp_scheduler_attempts_total{matcher}[30s]))",
        "ready_endpoints": f"avg(llm_d_epp_ready_endpoints{matcher})",
        "scheduler_latency_p95_ms": (
            f"histogram_quantile(0.95, "
            f"sum(rate(llm_d_epp_scheduler_e2e_duration_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "plugin_latency_p95_ms": (
            f"histogram_quantile(0.95, "
            f"sum(rate(llm_d_epp_plugin_duration_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "pd_decision_rate_rps": (
            f'sum(rate(llm_d_epp_disagg_decision_total{{namespace="{namespace}", '
            f'decision_type=~"prefill-decode|encode-prefill-decode"}}[30s]))'
            f" or sum(rate(llm_d_epp_pd_decision_total{matcher}[30s]))"
            f" or sum(rate(llm_d_inference_scheduler_pd_decision_total{matcher}[30s]))"
        ),
        "index_admissions_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_admissions_total{matcher}[30s])) or "
            f"sum(rate(kvcache_index_admissions_total{matcher}[30s]))"
        ),
        "index_evictions_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_evictions_total{matcher}[30s])) or "
            f"sum(rate(kvcache_index_evictions_total{matcher}[30s]))"
        ),
        "index_lookups_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_lookup_requests_total{matcher}[30s])) or "
            f"sum(rate(kvcache_index_lookup_requests_total{matcher}[30s]))"
        ),
        "index_matched_blocks_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_lookup_hits_total{matcher}[30s])) or "
            f"sum(rate(kvcache_index_lookup_hits_total{matcher}[30s]))"
        ),
        "index_max_pod_hit_blocks_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_max_pod_hit_count_total{matcher}[30s]))"
        ),
        "index_lookup_latency_p95_ms": (
            f"histogram_quantile(0.95, "
            f"sum(rate(llm_d_epp_kv_cache_index_lookup_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "index_lookup_latency_p99_ms": (
            f"histogram_quantile(0.99, "
            f"sum(rate(llm_d_epp_kv_cache_index_lookup_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "index_entry_count": (
            f"sum(kvcache_index_entry_count{matcher}) or sum(inference_extension_prefix_indexer_size{matcher})"
        ),
        "kv_event_rate_rps": f"sum(rate(kvcache_kvevents_messages_total{matcher}[30s]))",
        "kv_event_error_rate_rps": f"sum(rate(kvcache_kvevents_errors_total{matcher}[30s]))",
        "active_subscribers": f"sum(kvcache_kvevents_active_subscribers{matcher})",
        "subscriber_reconnect_rate_rps": f"sum(rate(kvcache_kvevents_reconnections_total{matcher}[30s]))",
    }
    per_endpoint_queries = {
        "inflight_token_load": (
            f"sum(llm_d_epp_inflight_tokens{matcher}) by (endpoint_name, pod, endpoint, target_pod, model_server)"
        ),
        "waiting_requests": (
            f"sum(llm_d_epp_per_endpoint_queue_size{matcher}) "
            "by (model_server_endpoint, endpoint_name, pod, endpoint, target_pod, model_server)"
        ),
    }
    # Device utilization/memory are profile-driven: exporter-labelled profiles
    # (NVIDIA DCGM) are queried directly, and the Intel DRA join replaces them
    # when exclusive allocations exist. A profile that does not configure a
    # metric leaves the key absent, so the panel hides it.
    device_total_queries = _device_queries(profiles, namespace)
    device_pod_queries = _device_queries(profiles, namespace, by="pod")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{local_port}", timeout=10.0) as client:
        (
            results,
            per_pod_results,
            router_results,
            per_endpoint_results,
            cache_config_results,
            xpu_results,
                device_total_results,
                device_pod_results,
        ) = await asyncio.gather(
            asyncio.gather(*(_query_range(client, query, start, end, step_seconds) for query in queries.values())),
            asyncio.gather(
                *(_query_range(client, query, start, end, step_seconds) for query in per_pod_queries.values())
            ),
            asyncio.gather(
                *(_query_range(client, query, start, end, step_seconds) for query in router_queries.values())
            ),
            asyncio.gather(
                *(_query_range(client, query, start, end, step_seconds) for query in per_endpoint_queries.values())
            ),
            _query_range(client, f"vllm:cache_config_info{matcher}", start, end, step_seconds),
            asyncio.gather(*(_query_range(client, query, start, end, step_seconds) for query in XPUM_QUERIES.values()))
            if xpu_allocations
            else asyncio.sleep(0, result=[]),
            asyncio.gather(
                *(_query_range(client, query, start, end, step_seconds) for query in device_total_queries.values())
            ),
            asyncio.gather(
                *(_query_range(client, query, start, end, step_seconds) for query in device_pod_queries.values())
            ),
        )

    device_points = {
        key: _range_points(result) for key, result in zip(device_total_queries, device_total_results, strict=True)
    }
    device_pod_vectors = {
        key: _range_series(result, ("pod", "instance"))
        for key, result in zip(device_pod_queries, device_pod_results)
    }
    device_sources = {key: "DCGM device telemetry" for key in device_points}
    if xpu_allocations:
        for key, samples in zip(XPUM_QUERIES, xpu_results, strict=False):
            total, per_pod_result = aggregate_xpum(samples, xpu_allocations, key)
            device_points[key] = _range_points(total)
            device_pod_vectors[key] = _range_series(per_pod_result, ("pod",))
            device_sources[key] = XPUM_SOURCE

    values_by_metric = {key: _range_points(result) for key, result in zip(queries, results, strict=False)}
    values_by_metric.update(device_points)
    timestamps = sorted({timestamp for points in values_by_metric.values() for timestamp in points})
    series = [
        {
            "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z"),
            **{key: points[timestamp] for key, points in values_by_metric.items() if timestamp in points},
        }
        for timestamp in timestamps
    ]
    summary = {
        key: stats for key, points in values_by_metric.items() if (stats := _summary(list(points.values()))) is not None
    }
    per_pod_vectors = {
        key: _range_series(result, ("pod", "instance"))
        for key, result in zip(per_pod_queries, per_pod_results, strict=False)
    }
    per_pod_vectors.update(device_pod_vectors)
    pod_names = sorted({name for vector in per_pod_vectors.values() for name in vector})
    per_pod = []
    for pod_name in pod_names:
        pod_metrics = {"pod": pod_name, "role": role_by_pod.get(pod_name, "model-server")}
        for metric_name, vector in per_pod_vectors.items():
            stats = _summary(list((vector.get(pod_name) or {}).values()))
            if stats is not None:
                pod_metrics[metric_name] = stats
        per_pod.append(pod_metrics)

    router_vectors = {key: _range_points(result) for key, result in zip(router_queries, router_results, strict=False)}
    router_summary = {
        key: stats for key, points in router_vectors.items() if (stats := _summary(list(points.values()))) is not None
    }
    cache_config = _cache_config_snapshot(cache_config_results)
    # Keep Router signals aligned with the common timeline so the result UI can
    # render index/event behavior without issuing live queries after cleanup.
    for point in series:
        timestamp = _iso_timestamp(point["timestamp"]).timestamp()
        for key, values in router_vectors.items():
            if timestamp in values:
                point[f"router_{key}"] = values[timestamp]
    metric_availability = {
        **{key: ("available" if result else "unavailable") for key, result in zip(queries, results, strict=False)},
        **{
            f"per_pod.{key}": ("available" if result else "unavailable")
            for key, result in zip(per_pod_queries, per_pod_results, strict=False)
        },
        **{key: ("available" if points else "unavailable") for key, points in device_points.items()},
        **{
            f"per_pod.{key}": ("available" if vector else "unavailable")
            for key, vector in device_pod_vectors.items()
        },
        **{
            f"router.{key}": ("available" if result else "unavailable")
            for key, result in zip(router_queries, router_results, strict=False)
        },
        **{
            f"per_endpoint.{key}": ("available" if result else "unavailable")
            for key, result in zip(per_endpoint_queries, per_endpoint_results, strict=False)
        },
        "cache_config_info": "available" if cache_config.get("engines") else "unavailable",
    }
    per_endpoint_vectors = {
        key: _range_series(
            result, ("endpoint_name", "model_server_endpoint", "endpoint", "target_pod", "model_server", "pod")
        )
        for key, result in zip(per_endpoint_queries, per_endpoint_results, strict=False)
    }
    endpoint_names = sorted({name for vector in per_endpoint_vectors.values() for name in vector})
    per_endpoint = []
    for endpoint_name in endpoint_names:
        endpoint_metrics = {"endpoint": endpoint_name}
        for metric_name, vector in per_endpoint_vectors.items():
            stats = _summary(list((vector.get(endpoint_name) or {}).values()))
            if stats is not None:
                endpoint_metrics[metric_name] = stats
        per_endpoint.append(endpoint_metrics)

    request_means = {
        item["pod"]: item.get("request_rate_rps", {}).get("mean")
        for item in per_pod
        if isinstance(item.get("request_rate_rps", {}).get("mean"), (int, float))
    }
    request_total = sum(request_means.values())
    destination_distribution = [
        {
            "pod": pod,
            "request_rate_rps": rate,
            "percent": round(rate / request_total * 100, 3) if request_total else None,
        }
        for pod, rate in request_means.items()
    ]
    role_destination_distribution = {
        role: [item for item in destination_distribution if role_by_pod.get(item["pod"]) == role]
        for role in ("prefill", "decode")
    }
    token_load_means = [
        item["inflight_token_load"]["mean"]
        for item in per_endpoint
        if isinstance(item.get("inflight_token_load", {}).get("mean"), (int, float))
    ]
    input_rate = router_summary.get("input_token_rate_tps", {}).get("mean")
    cached_rate = router_summary.get("cached_token_rate_tps", {}).get("mean")
    avoided_prefill_ratio = (
        round(min(100.0, max(0.0, cached_rate / input_rate * 100)), 3)
        if isinstance(input_rate, (int, float)) and input_rate > 0 and isinstance(cached_rate, (int, float))
        else None
    )
    recomputed_prefill_tokens = (
        round(max(0.0, input_rate - cached_rate) * duration_seconds)
        if isinstance(input_rate, (int, float)) and isinstance(cached_rate, (int, float))
        else None
    )
    recomputed_token_rate = (
        max(0.0, input_rate - cached_rate)
        if isinstance(input_rate, (int, float)) and isinstance(cached_rate, (int, float))
        else None
    )
    router_request_rate = router_summary.get("request_rate_rps", {}).get("mean")
    pd_decision_rate = router_summary.get("pd_decision_rate_rps", {}).get("mean")
    pd_decision_ratio = (
        round(min(100.0, max(0.0, pd_decision_rate / router_request_rate * 100)), 3)
        if isinstance(router_request_rate, (int, float))
        and router_request_rate > 0
        and isinstance(pd_decision_rate, (int, float))
        else None
    )
    successful_transfers = (summary.get("nixl_transfer_rate_rps") or {}).get("mean")
    failed_transfers = (summary.get("nixl_failed_transfer_rate_rps") or {}).get("mean")
    kv_transfer_success_rate = (
        round(successful_transfers / (successful_transfers + failed_transfers) * 100, 3)
        if isinstance(successful_transfers, (int, float))
        and isinstance(failed_transfers, (int, float))
        and successful_transfers + failed_transfers > 0
        else None
    )
    index_lookups = (router_summary.get("index_lookups_per_second") or {}).get("mean")
    matched_blocks = (router_summary.get("index_matched_blocks_per_second") or {}).get("mean")
    matched_blocks_per_lookup = (
        round(matched_blocks / index_lookups, 4)
        if isinstance(matched_blocks, (int, float)) and isinstance(index_lookups, (int, float)) and index_lookups > 0
        else None
    )
    active_subscribers = (router_summary.get("active_subscribers") or {}).get("mean")
    expected_subscribers = len(components.get("prefill") or []) + len(components.get("decode") or [])
    subscriber_coverage = (
        round(min(100.0, active_subscribers / expected_subscribers * 100), 3)
        if isinstance(active_subscribers, (int, float)) and expected_subscribers > 0
        else None
    )
    role_summary: dict[str, dict] = {}
    additive = {
        "request_rate_rps",
        "input_token_rate_tps",
        "running_requests",
        "waiting_requests",
        "output_token_rate_tps",
        "memory_working_set_bytes",
        "cpu_memory_usage_bytes",
        "gpu_memory_usage_bytes",
        "gpu_framebuffer_used_bytes",
        "filesystem_read_bytes_per_second",
        "filesystem_write_bytes_per_second",
        "network_receive_bytes_per_second",
        "network_transmit_bytes_per_second",
    }
    for role in ("prefill", "decode"):
        pods = [item for item in per_pod if item.get("role") == role]
        role_metrics: dict[str, float] = {}
        metric_names = {key for item in pods for key in item if key not in {"pod", "role"}}
        for metric_name in metric_names:
            values = [(item.get(metric_name) or {}).get("mean") for item in pods]
            finite = [value for value in values if isinstance(value, (int, float)) and math.isfinite(value)]
            if finite:
                role_metrics[metric_name] = sum(finite) if metric_name in additive else statistics.fmean(finite)
        if role_metrics:
            role_summary[role] = role_metrics

    role_timestamps = sorted(
        {timestamp for vector in per_pod_vectors.values() for points in vector.values() for timestamp in points}
    )
    role_series = []
    for timestamp in role_timestamps:
        point = {"timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")}
        for role in ("prefill", "decode"):
            role_pods = set(components.get(role) or [])
            for metric_name, vector in per_pod_vectors.items():
                values = [
                    points[timestamp]
                    for pod, points in vector.items()
                    if pod in role_pods and timestamp in points and math.isfinite(points[timestamp])
                ]
                if values:
                    point[f"{role}_{metric_name}"] = (
                        sum(values) if metric_name in additive else statistics.fmean(values)
                    )
        if len(point) > 1:
            role_series.append(point)

    derived = {
        "token_load_cv": _coefficient_of_variation(token_load_means),
        "request_destination_distribution": destination_distribution,
        "prefill_endpoint_distribution": role_destination_distribution["prefill"],
        "decode_endpoint_distribution": role_destination_distribution["decode"],
        "avoided_prefill_ratio": avoided_prefill_ratio,
        "recomputed_prefill_tokens": recomputed_prefill_tokens,
        "recomputed_token_rate_tps": recomputed_token_rate,
        "pd_decision_ratio": pd_decision_ratio,
        "kv_transfer_success_rate": kv_transfer_success_rate,
        "matched_blocks_per_lookup": matched_blocks_per_lookup,
        "kv_event_subscriber_coverage": subscriber_coverage,
        "offloaded_bytes": round((summary.get("kv_offload_bytes_per_second") or {}).get("mean", 0) * duration_seconds)
        if summary.get("kv_offload_bytes_per_second")
        else None,
        "restored_bytes": round((summary.get("kv_restore_bytes_per_second") or {}).get("mean", 0) * duration_seconds)
        if summary.get("kv_restore_bytes_per_second")
        else None,
        "role_summary": role_summary,
        "role_series": role_series,
        "hbm_capacity_tokens": cache_config.get("hbm_capacity_tokens"),
        "cpu_capacity_tokens": cache_config.get("cpu_capacity_tokens"),
        "effective_capacity_tokens": cache_config.get("effective_capacity_tokens"),
    }
    evidence_specs = {
        "prefix_cache_hit_rate": (
            (summary.get("prefix_cache_hit_percent") or {}).get("mean"),
            "%",
            "vLLM prefix cache counter ratio",
        ),
        "effective_prefix_hit_rate": (
            next(
                (
                    value
                    for value in (
                        avoided_prefill_ratio,
                        (summary.get("external_prefix_cache_hit_percent") or {}).get("mean"),
                        (summary.get("prefix_cache_hit_percent") or {}).get("mean"),
                    )
                    if isinstance(value, (int, float))
                ),
                None,
            ),
            "%",
            "EPP cached/input token ratio, with vLLM cache counters as fallback",
        ),
        "token_load_cv": (derived["token_load_cv"], "CV", "derived from EPP in-flight tokens by endpoint"),
        "effective_cached_prompt_fraction": (
            avoided_prefill_ratio,
            "%",
            "derived from EPP cached tokens / input tokens",
        ),
        "prefill_recomputation_avoided": (avoided_prefill_ratio, "%", "derived from EPP cached tokens / input tokens"),
        "recomputed_prompt_tokens": (
            recomputed_prefill_tokens,
            "tokens",
            "derived over the benchmark window from EPP token rates",
        ),
        "kv_transfer_success_rate": (
            kv_transfer_success_rate,
            "%",
            "derived from vLLM NIXL success and failure counters",
        ),
        "transfer_latency_p50": (
            (summary.get("nixl_transfer_latency_p50_ms") or {}).get("p50"),
            "ms",
            "vLLM NIXL transfer histogram",
        ),
        "transfer_latency_p95": (
            (summary.get("nixl_transfer_latency_p95_ms") or {}).get("p95"),
            "ms",
            "vLLM NIXL transfer histogram",
        ),
        "transfer_latency_p99": (
            (summary.get("nixl_transfer_latency_p99_ms") or {}).get("p99"),
            "ms",
            "vLLM NIXL transfer histogram",
        ),
        "transfer_bandwidth": (
            (summary.get("nixl_transfer_bytes_per_second") or {}).get("mean"),
            "bytes/s",
            "vLLM NIXL bytes-transferred histogram",
        ),
        "index_admissions": (
            (router_summary.get("index_admissions_per_second") or {}).get("mean"),
            "blocks/s",
            "llm-d KV index counter",
        ),
        "index_evictions": (
            (router_summary.get("index_evictions_per_second") or {}).get("mean"),
            "blocks/s",
            "llm-d KV index counter",
        ),
        "index_lookups": (index_lookups, "lookups/s", "llm-d KV index counter"),
        "matched_blocks_lookup": (
            matched_blocks_per_lookup,
            "blocks/lookup",
            "derived from KV index hit and lookup counters",
        ),
        "index_lookup_latency": (
            (router_summary.get("index_lookup_latency_p95_ms") or {}).get("p95"),
            "ms p95",
            "llm-d KV index histogram",
        ),
        "kv_event_rate": (
            (router_summary.get("kv_event_rate_rps") or {}).get("mean"),
            "events/s",
            "llm-d KV event counter",
        ),
        "active_subscribers": (
            (router_summary.get("active_subscribers") or {}).get("mean"),
            "subscribers",
            "llm-d KV event subscriber gauge",
        ),
        "offloaded_bytes": (
            derived["offloaded_bytes"],
            "bytes",
            "vLLM offloading counter integrated over benchmark window",
        ),
        "restored_bytes": (
            derived["restored_bytes"],
            "bytes",
            "vLLM offloading counter integrated over benchmark window",
        ),
        "offload_bandwidth": (
            (summary.get("kv_offload_bytes_per_second") or {}).get("mean"),
            "bytes/s",
            "vLLM offloading counter",
        ),
        "restore_bandwidth": (
            (summary.get("kv_restore_bytes_per_second") or {}).get("mean"),
            "bytes/s",
            "vLLM offloading counter",
        ),
        "hbm_cache_utilization": (
            (summary.get("gpu_cache_usage_percent") or summary.get("kv_cache_usage_percent") or {}).get("mean"),
            "%",
            "vLLM GPU KV cache gauge",
        ),
        "cpu_cache_utilization": (
            (summary.get("cpu_cache_usage_percent") or {}).get("mean"),
            "%",
            "vLLM CPU KV cache gauge",
        ),
        "cpu_memory_usage": (
            (summary.get("cpu_memory_usage_bytes") or {}).get("mean"),
            "bytes",
            "vLLM CPU memory gauge",
        ),
        "gpu_xpu_utilization": (
            (summary.get("gpu_utilization_percent") or {}).get("mean"),
            "%",
            device_sources.get("gpu_utilization_percent", "device telemetry not configured"),
        ),
        "hbm_capacity": (cache_config.get("hbm_capacity_tokens"), "tokens", "vLLM cache_config_info runtime capacity"),
        "effective_cache_capacity": (
            cache_config.get("effective_capacity_tokens"),
            "tokens",
            "vLLM GPU plus CPU cache_config_info capacities",
        ),
    }
    evidence = {
        key: {
            "status": "measured" if isinstance(value, (int, float)) else "unavailable",
            **({"value": value, "unit": unit} if isinstance(value, (int, float)) else {}),
            "source": source,
            "window": {"start": start, "end": end},
            **(
                {"reason": f"{source} returned no finite sample during the benchmark window"}
                if not isinstance(value, (int, float))
                else {}
            ),
        }
        for key, (value, unit, source) in evidence_specs.items()
    }
    return {
        "status": "available" if series else "empty",
        "reason": None if series else "Prometheus returned no samples for the benchmark window",
        "source": "prometheus",
        "guide_type": getattr(_context, "guide", None),
        "namespace": namespace,
        "window": {"start": start, "end": end},
        "step_seconds": step_seconds,
        "series": series,
        "summary": summary,
        "per_pod": per_pod,
        "per_endpoint": per_endpoint,
        "role_series": role_series,
        "router": router_summary,
        "cache_config": cache_config,
        "derived": {key: value for key, value in derived.items() if value is not None},
        "evidence": evidence,
        "availability": {
            "metrics": metric_availability,
            "prefix_group_stickiness": "not_exposed_by_current_router_metrics",
            "prefix_group_pod_heatmap": "requires_request_prefix_group_destination_correlation",
            "affinity_routing_evidence": "not_exposed_by_current_router_metrics",
            "spillover_routing_evidence": "not_exposed_by_current_router_metrics",
            "kv_event_subscriber_health": "upstream_metric_required",
            "kv_event_publisher_coverage": "upstream_vLLM_does_not_export_per_publisher_KV_event_health",
            "warm_route_share": "requires_request_level_route_and_resident-prefix correlation",
            "cache_warm_route_share": "requires_request_level_route_and_resident-prefix correlation",
            "kv_peer_connectivity": "upstream_connector_does_not_export_peer_connectivity_gauge",
            "affinity_load_gate_behavior": "requires_per_request_router_decision_outcome_metrics",
            "hbm_capacity": "available"
            if cache_config.get("hbm_capacity_tokens") is not None
            else "vllm_cache_config_info_did_not_expose_kv_cache_size_tokens",
            "effective_cache_capacity": "available"
            if cache_config.get("effective_capacity_tokens") is not None
            else "vllm_cache_config_info_did_not_expose_GPU_or_CPU_token_capacity",
            "cpu_hit_rate": "external_hits_are_not_attributed_to_CPU_vs_filesystem_by_vLLM",
            "fs_hit_rate": "external_hits_are_not_attributed_to_CPU_vs_filesystem_by_vLLM",
            "fs_cache_utilization": "filesystem_backend_does_not_export_capacity_utilization_gauge",
        },
        "device_telemetry": {
            "scope": "allocated-device" if xpu_allocations else "exporter-pod-labels",
            "devices": [
                {"node": node, "pci_bdf": pci, **allocation}
                for (node, pci), allocation in sorted(xpu_allocations.items())
            ],
            "missing_metrics": [key for key in XPUM_QUERIES if key not in summary],
            **(
                {
                    "reason": (
                        "Missing XPUM samples or node/PCI labels"
                        if xpu_allocations
                        else (
                            "Intel XPUM requires exclusive DRA allocations with node/PCI mapping; "
                            "NVIDIA uses DCGM Pod labels"
                        )
                    )
                }
                if any(key not in summary for key in XPUM_QUERIES)
                else {}
            ),
        },
        "metric_sources": {
            **device_sources,
            **{
                key: f"vLLM prompt_tokens_by_source_total · source={source}"
                for key, source in engine_token_sources.items()
            },
            "engine_prompt_recomputed_tps": (
                "vLLM prompt_tokens_recomputed_total · cached tokens recomputed for forward pass"
            ),
            "request_destination_distribution": "vLLM completed-request rate grouped by destination pod",
            "token_load_cv": "llm_d_epp_inflight_tokens grouped by endpoint",
            "avoided_prefill_ratio": "llm_d_epp_request_cached_tokens / llm_d_epp_request_input_tokens",
        },
    }


async def build_flow_map(execution_id: str, cluster_id: str | None = None) -> FlowMapResponse:
    """Assemble the request-pipeline Flow Map for a deployment execution."""
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    local_port = await _prometheus_local_port(cluster_id)
    instances = await _discover_components(namespace, cluster_id)

    components: list[FlowMapComponent] = []
    by_role: dict[str, FlowMapComponent] = {}

    present_roles = [role for role in FLOW_ORDER if instances[role]]
    metrics_by_role: dict[str, dict[str, dict[str, float | None]]] = {}
    process_time_by_role: dict[str, float | None] = {}
    if local_port is not None and present_roles:
        # One shared HTTP client (kept-alive connection) issues every PromQL
        # query concurrently, so the whole role set costs a single round-trip
        # rather than ~30 sequential requests.
        latency_roles = [role for role in present_roles if role != "epp" and instances[role]]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{local_port}", timeout=5.0) as client:
            metrics_task = asyncio.gather(
                *(
                    _role_queries(
                        client,
                        namespace,
                        role,
                        instances[role],
                        instances,
                    )
                    for role in present_roles
                )
            )
            latency_task = asyncio.gather(
                *(
                    _component_latency(
                        client,
                        f'{{namespace="{namespace}", pod=~"{pod_re}"}}',
                    )
                    for pod_re in ("|".join(instances[role]) for role in latency_roles)
                )
            )
            metrics_results, latency_results = await asyncio.gather(metrics_task, latency_task)
            metrics_by_role = dict(zip(present_roles, metrics_results, strict=False))
            process_time_by_role = dict(zip(latency_roles, latency_results, strict=False))

    for role in FLOW_ORDER:
        present = bool(instances[role])
        instance_objects: list[FlowMapInstance] = []
        metrics_by_name = metrics_by_role.get(role) or {}
        for name in instances[role]:
            metrics = metrics_by_name.get(name) or {}
            instance_objects.append(
                FlowMapInstance(
                    name=name,
                    request_rate=metrics.get("request_rate"),
                    arrival_request_rate=metrics.get("arrival_request_rate"),
                    success_request_rate=metrics.get("success_request_rate"),
                    failed_request_rate=metrics.get("failed_request_rate"),
                    client_timeout_rate=metrics.get("client_timeout_rate"),
                    backend_error_rate=metrics.get("backend_error_rate"),
                    input_token_rate=metrics.get("input_token_rate"),
                    output_token_rate=metrics.get("output_token_rate"),
                    queue_length=metrics.get("queue_length"),
                    prefill_inflight=metrics.get("prefill_inflight"),
                    decode_inflight=metrics.get("decode_inflight"),
                    kv_cache_usage_perc=metrics.get("kv_cache_usage_perc"),
                    prefix_cache_hit_rate=metrics.get("prefix_cache_hit_rate"),
                    external_prefix_cache_hit_rate=metrics.get("external_prefix_cache_hit_rate"),
                )
            )
        component = FlowMapComponent(
            role=role,
            label=COMPONENT_LABELS[role],
            present=present,
            instances=instance_objects,
        )
        if present and metrics_by_name:
            totals = _sum_metrics(metrics_by_name)
            component.request_rate = totals.get("request_rate")
            component.arrival_request_rate = totals.get("arrival_request_rate")
            component.success_request_rate = totals.get("success_request_rate")
            component.failed_request_rate = totals.get("failed_request_rate")
            component.client_timeout_rate = totals.get("client_timeout_rate")
            component.backend_error_rate = totals.get("backend_error_rate")
            component.input_token_rate = totals.get("input_token_rate")
            component.output_token_rate = totals.get("output_token_rate")
            component.queue_length = totals.get("queue_length")
            component.process_time_ms = process_time_by_role.get(role)
            cache_totals = _avg_metrics(
                metrics_by_name,
                (
                    "kv_cache_usage_perc",
                    "prefix_cache_hit_rate",
                    "external_prefix_cache_hit_rate",
                ),
            )
            component.kv_cache_usage_perc = cache_totals.get("kv_cache_usage_perc")
            component.prefix_cache_hit_rate = cache_totals.get("prefix_cache_hit_rate")
            component.external_prefix_cache_hit_rate = cache_totals.get("external_prefix_cache_hit_rate")
        components.append(component)
        by_role[role] = component

    # Each stage reports its OWN measured rate. Requests are conserved down the
    # pipeline, but a request that fails at a stage must NOT be carried into
    # the next stage (it diverges out of the flow), so cloning the deepest
    # stage's rate onto upstream stages would mask both idle stages and real
    # per-stage failure deltas. The frontend draws success flow stage-to-stage
    # and branches each stage's failed_request_rate to a failure sink.

    chain = ["users"] + present_roles
    edges: list[FlowMapEdge] = []
    for source, target in zip(chain, chain[1:], strict=False):
        component = by_role[target]
        edges.append(
            FlowMapEdge(
                id=f"{source}->{target}",
                source=source,
                target=target,
                request_rate=component.request_rate,
                success_request_rate=component.success_request_rate,
                failed_request_rate=component.failed_request_rate,
                input_token_rate=component.input_token_rate,
                output_token_rate=component.output_token_rate,
            )
        )

    message: str | None = None
    if local_port is None:
        message = "Prometheus is not reachable; rates are unavailable."
    elif not present_roles:
        message = "No EPP, prefill, or decode components found in this namespace."

    return FlowMapResponse(
        execution_id=execution_id,
        namespace=namespace,
        prometheus_reachable=local_port is not None,
        message=message,
        components=components,
        edges=edges,
    )
