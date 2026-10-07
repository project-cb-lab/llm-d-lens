"""Tests for the EPP-counter -> usage-ledger sync."""

from types import SimpleNamespace

import pytest

from llm_d_bench.model_service.usage_sync import GatewayUsageSync, diff_counters, snapshot_key


def _series(value):
    return {"metric": {"model_name": "qwen", "fairness_id": "mat-1"}, "value": [0, str(value)]}


class _FakeTokens:
    def __init__(self, tokens):
        self._tokens = tokens

    def get(self, token_id):
        return self._tokens.get(token_id)


class _FakeService:
    def __init__(self, *, members, groups, tokens):
        self._members = members
        self._groups = groups
        self.tokens = _FakeTokens(tokens)
        self.recorded = []

    def list_members(self):
        return self._members

    def list_groups(self):
        return self._groups

    def record_usage(self, request):
        self.recorded.append(request)
        return request


class _FakeSnapshots:
    def __init__(self):
        self.store = {}

    def get(self, cluster_id):
        return self.store.get(cluster_id)

    def put(self, snapshot):
        self.store[snapshot.cluster_id] = snapshot
        return snapshot


def _service():
    member = SimpleNamespace(status="active", cluster_id="cluster-a", epp_ref="qwen-epp", target_namespace="ns-1")
    group = SimpleNamespace(
        id="msg-1",
        cluster_id="cluster-a",
        name="Qwen",
        display_name="Qwen",
        served_name="qwen",
        base_model="Qwen/Qwen3-0.6B",
        model_ref="Qwen/Qwen3-0.6B",
    )
    return _FakeService(
        members=[member],
        groups=[group],
        tokens={"mat-1": SimpleNamespace(id="mat-1", user_id="user-1")},
    )


def test_diff_counters_handles_new_keys_and_resets():
    deltas = diff_counters(
        {
            snapshot_key("qwen", "mat-1"): {
                "input_tokens": 100,
                "output_tokens": 40,
                "cached_input_tokens": 0,
                "requests": 2,
            }
        },
        {
            snapshot_key("qwen", "mat-1"): {
                "input_tokens": 150,
                "output_tokens": 10,
                "cached_input_tokens": 5,
                "requests": 3,
            }
        },
    )
    delta = deltas[snapshot_key("qwen", "mat-1")]
    assert delta["input_tokens"] == 50
    assert delta["output_tokens"] == 10  # reset: current value, not negative
    assert delta["cached_input_tokens"] == 5
    assert delta["requests"] == 1


@pytest.mark.asyncio
async def test_sync_baselines_then_writes_attributed_delta():
    service = _service()
    snapshots = _FakeSnapshots()
    values = {"input_tokens": 100, "output_tokens": 40, "cached": 10, "requests": 2}
    seen_promql = []

    async def fetch(cluster_id, promql):
        seen_promql.append(promql)
        if "output_tokens_sum" in promql:
            return [_series(values["output_tokens"])]
        if "input_tokens_count" in promql:
            return [_series(values["requests"])]
        if "input_tokens_sum" in promql:
            return [_series(values["input_tokens"])]
        return []

    sync = GatewayUsageSync(service=service, snapshots=snapshots, fetch_series=fetch)

    # The first reading only establishes the cumulative baseline.
    assert await sync.sync() == {"cluster-a": 0}
    assert service.recorded == []
    # Queries are scoped to the active member's namespace.
    assert any('namespace=~"ns-1"' in promql for promql in seen_promql)

    values.update(input_tokens=150, output_tokens=90, requests=5)
    assert await sync.sync() == {"cluster-a": 1}
    record = service.recorded[0]
    assert record.token_id == "mat-1"  # noqa: S105 - synthetic test identifier
    assert record.group_id == "msg-1"
    assert record.group_name == "Qwen"
    assert (record.input_tokens, record.output_tokens) == (50, 50)
    assert record.requests == 3
    assert record.cluster_id == "cluster-a"

    # Same counters again: no new delta, snapshot still refreshed.
    assert await sync.sync() == {"cluster-a": 0}
    assert len(service.recorded) == 1


@pytest.mark.asyncio
async def test_sync_records_counter_reset_as_current_value():
    service = _service()
    snapshots = _FakeSnapshots()
    values = {"input_tokens": 100, "output_tokens": 40, "cached": 10, "requests": 2}

    async def fetch(cluster_id, promql):
        if "output_tokens_sum" in promql:
            return [_series(values["output_tokens"])]
        if "input_tokens_count" in promql:
            return [_series(values["requests"])]
        if "input_tokens_sum" in promql:
            return [_series(values["input_tokens"])]
        return []

    sync = GatewayUsageSync(service=service, snapshots=snapshots, fetch_series=fetch)
    await sync.sync()  # baseline
    service.recorded.clear()
    values.update(input_tokens=5, output_tokens=2, cached=0, requests=1)
    await sync.sync()  # smaller reading: EPP restarted -> record the new value
    reset = service.recorded[-1]
    assert (reset.input_tokens, reset.output_tokens, reset.cached_input_tokens) == (5, 2, 0)
