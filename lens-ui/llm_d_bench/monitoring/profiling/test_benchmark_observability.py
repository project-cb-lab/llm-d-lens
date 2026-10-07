"""Tests for benchmark-window Prometheus observability."""

from datetime import UTC, datetime

import pytest

from llm_d_bench.monitoring.profiling import service


@pytest.fixture(autouse=True)
def no_intel_allocations(monkeypatch):
    async def resources(*_args, **_kwargs):
        return []

    monkeypatch.setattr(service, "list_resources", resources)


def test_range_points_sum_series_and_summary_percentiles():
    points = service._range_points(
        [
            {"values": [[10, "1.5"], [20, "2.5"], [30, "NaN"]]},
            {"values": [[10, "3.5"], [20, "4.5"]]},
        ]
    )

    assert points == {10.0: 5.0, 20.0: 7.0}
    assert service._summary(list(points.values())) == {
        "mean": 6.0,
        "p50": 5.0,
        "p95": 7.0,
        "p99": 7.0,
        "max": 7.0,
    }


def test_cache_config_snapshot_extracts_runtime_token_capacity():
    snapshot = service._cache_config_snapshot(
        [
            {
                "metric": {
                    "pod": "vllm-0",
                    "engine": "0",
                    "block_size": "16",
                    "num_gpu_blocks": "100",
                    "num_cpu_blocks": "25",
                    "kv_cache_size_tokens": "1600",
                },
                "values": [[1, "1"]],
            },
            {
                "metric": {
                    "pod": "vllm-1",
                    "engine": "0",
                    "block_size": "16",
                    "num_gpu_blocks": "100",
                    "num_cpu_blocks": "None",
                    "kv_cache_size_tokens": "1600",
                },
                "values": [[1, "1"]],
            },
        ]
    )

    assert snapshot["hbm_capacity_tokens"] == 3200
    assert snapshot["cpu_capacity_tokens"] == 400
    assert snapshot["effective_capacity_tokens"] == 3600


@pytest.mark.asyncio
async def test_collect_benchmark_observability_builds_aligned_series(monkeypatch):
    monkeypatch.setattr(
        service,
        "_deployment_target",
        lambda execution_id, cluster_id: (object(), "benchmark-ns", "cluster-1"),
    )

    async def fake_port(_cluster_id):
        return 19090

    async def fake_query_range(_client, promql, _start, _end, _step):
        if "external_prefix_cache_hits_total" in promql:
            value = "25"
        elif "prefix_cache_hits_total" in promql:
            value = "80"
        else:
            value = "75" if "kv_cache_usage" in promql else "4"
        metric = {"endpoint_name": "prefill-0"} if "by (endpoint_name" in promql else {"pod": "prefill-0"}
        return [{"metric": metric, "values": [[1000, value], [1005, value]]}]

    async def fake_components(_namespace, _cluster_id):
        return {"epp": ["epp-0"], "prefill": ["prefill-0"], "decode": [], "model-server": []}

    monkeypatch.setattr(service, "_prometheus_local_port", fake_port)
    monkeypatch.setattr(service, "_query_range", fake_query_range)
    monkeypatch.setattr(service, "_discover_components", fake_components)

    result = await service.collect_benchmark_observability(
        "execution-1",
        "2026-09-02T10:00:00Z",
        "2026-09-02T10:01:00Z",
    )

    assert result["status"] == "available"
    assert result["namespace"] == "benchmark-ns"
    assert len(result["series"]) == 2
    assert result["series"][0]["kv_cache_usage_percent"] == 75.0
    assert result["series"][0]["prefix_cache_hit_percent"] == 80.0
    assert result["series"][0]["external_prefix_cache_hit_percent"] == 25.0
    assert result["summary"]["external_prefix_cache_hit_percent"]["mean"] == 25.0
    assert result["summary"]["queue_depth"]["p99"] == 4.0
    assert result["per_pod"][0]["request_rate_rps"]["mean"] == 4.0
    assert result["per_endpoint"][0]["inflight_token_load"]["mean"] == 4.0
    assert result["derived"]["token_load_cv"] == 0.0
    assert result["derived"]["request_destination_distribution"][0]["percent"] == 100.0
    assert result["derived"]["avoided_prefill_ratio"] == 100.0
    assert result["derived"]["kv_transfer_success_rate"] == 50.0
    assert result["derived"]["matched_blocks_per_lookup"] == 1.0
    assert result["role_series"][0]["prefill_request_rate_rps"] == 4.0
    assert result["evidence"]["kv_transfer_success_rate"]["status"] == "measured"
    assert result["evidence"]["transfer_bandwidth"]["source"] == "vLLM NIXL bytes-transferred histogram"
    assert result["evidence"]["hbm_capacity"]["status"] == "unavailable"
    assert "no finite sample" in result["evidence"]["hbm_capacity"]["reason"]
    assert result["router"]["scheduler_latency_p95_ms"]["mean"] == 4.0
    assert datetime.fromisoformat(result["series"][0]["timestamp"].replace("Z", "+00:00")).tzinfo == UTC


@pytest.mark.asyncio
async def test_collect_benchmark_observability_reports_missing_prometheus(monkeypatch):
    monkeypatch.setattr(
        service,
        "_deployment_target",
        lambda execution_id, cluster_id: (object(), "benchmark-ns", "cluster-1"),
    )

    async def fake_port(_cluster_id):
        return None

    monkeypatch.setattr(service, "_prometheus_local_port", fake_port)

    result = await service.collect_benchmark_observability(
        "execution-1",
        "2026-09-02T10:00:00Z",
        "2026-09-02T10:01:00Z",
    )

    assert result["status"] == "unavailable"
    assert result["series"] == []


@pytest.mark.asyncio
async def test_engine_token_sources_are_collected_separately_from_router_estimates(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "benchmark-ns", "cluster-1"))

    async def port(*_):
        return 19090

    async def components(*_):
        return {"prefill": [], "decode": []}

    observed_queries = []

    async def query(_client, promql, *_):
        observed_queries.append(promql)
        rates = {
            'source="local_compute"': 20,
            'source="local_cache_hit"': 60,
            'source="external_kv_transfer"': 10,
            "vllm:prompt_tokens_recomputed_total": 2,
        }
        for needle, value in rates.items():
            if needle in promql:
                return [{"values": [[1788998400, str(value)], [1788998405, str(value)]]}]
        return []

    monkeypatch.setattr(service, "_prometheus_local_port", port)
    monkeypatch.setattr(service, "_discover_components", components)
    monkeypatch.setattr(service, "_query_range", query)
    result = await service.collect_benchmark_observability(
        "execution-1", "2026-09-10T00:00:00Z", "2026-09-10T00:01:00Z"
    )
    for field, value in {
        "engine_prompt_local_compute_tps": 20,
        "engine_prompt_local_cache_tps": 60,
        "engine_prompt_external_tps": 10,
        "engine_prompt_recomputed_tps": 2,
    }.items():
        assert result["summary"][field]["mean"] == value
        assert result["series"][0][field] == value
        assert result["availability"]["metrics"][field] == "available"
        assert "vLLM" in result["metric_sources"][field]
    assert any(
        'vllm:prompt_tokens_by_source_total{namespace="benchmark-ns", source="local_compute"}' in q
        for q in observed_queries
    )
