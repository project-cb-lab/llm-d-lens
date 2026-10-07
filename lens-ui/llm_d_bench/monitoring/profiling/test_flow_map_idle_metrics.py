"""Tests that idle (zero-traffic) pods still surface gauge-style Profiling
metrics (queue depth, KV cache usage, cache hit rates, EPP phase in-flight)
instead of falling back to "no data" just because nothing has completed yet.
"""

import httpx
import pytest

from llm_d_bench.monitoring.profiling import service


@pytest.mark.asyncio
async def test_role_queries_decode_defaults_idle_gauges_to_zero(monkeypatch):
    async def fake_query(_client, _promql):
        # Every PromQL in the role's query set comes back empty, as it would
        # for a pod that has taken zero requests (no counter samples yet).
        return []

    monkeypatch.setattr(service, "_query", fake_query)

    async with httpx.AsyncClient() as client:
        by_pod = await service._role_queries(client, "ns", "decode", ["decode-0"], {"decode": ["decode-0"]})

    assert by_pod["decode-0"]["queue_length"] == 0.0
    assert by_pod["decode-0"]["kv_cache_usage_perc"] == 0.0
    assert by_pod["decode-0"]["prefix_cache_hit_rate"] == 0.0
    assert by_pod["decode-0"]["external_prefix_cache_hit_rate"] == 0.0
    # Traffic counters (request rate, etc.) are not gauges and are left
    # unset — there is a real difference between "no requests yet" and "zero
    # rate", and this change only concerns the always-on state gauges.
    assert by_pod["decode-0"].get("request_rate") is None


@pytest.mark.asyncio
async def test_role_queries_decode_pod_absent_from_every_vector_still_reports_zero(monkeypatch):
    # A pod that never shows up in ANY PromQL result vector (e.g. brand new,
    # not yet scraped) previously vanished from `by_pod` entirely, dropping
    # every metric to None once merged in build_flow_map. It must still get
    # an entry with the idle-gauge defaults.
    async def fake_query(_client, _promql):
        return []

    monkeypatch.setattr(service, "_query", fake_query)

    async with httpx.AsyncClient() as client:
        by_pod = await service._role_queries(client, "ns", "prefill", ["prefill-0"], {"prefill": ["prefill-0"]})

    assert "prefill-0" in by_pod
    assert by_pod["prefill-0"]["queue_length"] == 0.0
    assert by_pod["prefill-0"]["kv_cache_usage_perc"] == 0.0


@pytest.mark.asyncio
async def test_role_queries_epp_defaults_inflight_gauges_to_zero(monkeypatch):
    async def fake_query(_client, _promql):
        return []

    monkeypatch.setattr(service, "_query", fake_query)

    async with httpx.AsyncClient() as client:
        by_name = await service._role_queries(
            client,
            "ns",
            "epp",
            ["optimized-baseline-epp"],
            {"prefill": ["prefill-0"], "decode": ["decode-0"]},
        )

    metrics = by_name["optimized-baseline-epp"]
    assert metrics["queue_length"] == 0.0
    assert metrics["prefill_inflight"] == 0.0
    assert metrics["decode_inflight"] == 0.0


@pytest.mark.asyncio
async def test_role_queries_epp_omits_phase_when_role_not_present(monkeypatch):
    async def fake_query(_client, _promql):
        return []

    monkeypatch.setattr(service, "_query", fake_query)

    async with httpx.AsyncClient() as client:
        by_name = await service._role_queries(
            client,
            "ns",
            "epp",
            ["optimized-baseline-epp"],
            {"decode": ["decode-0"]},
        )

    metrics = by_name["optimized-baseline-epp"]
    # No prefill pods discovered, so there is no "at prefill" phase to report
    # at all — it should stay absent rather than being coerced to 0, which
    # would wrongly claim a prefill stage exists.
    assert "prefill_inflight" not in metrics
    assert metrics["decode_inflight"] == 0.0
