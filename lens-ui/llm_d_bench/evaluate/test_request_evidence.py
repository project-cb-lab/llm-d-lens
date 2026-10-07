import json
from copy import deepcopy

import pytest
import yaml

from llm_d_bench.evaluate.request_evidence import (
    apply_request_evidence,
    enable_request_reports,
    kv_working_set,
    request_goodput,
)


def request(start=0, first=0.1, end=0.2, tokens=3, error=None, stage=0):
    return {
        "start_time": start,
        "end_time": end,
        "error": error,
        "stage_id": stage,
        "info": {"response_metrics": {"output_tokens": tokens, "output_token_times": [first]}},
    }


def metrics(count=4, success=3, duration=10):
    return {
        "benchmark_time_seconds": duration,
        "request_count": count,
        "success_count": success,
        "failure_count": count - success,
    }


def test_goodput_requires_joint_attainment_and_counts_failure_window():
    rows = [request(), request(first=0.3, end=0.4), request(end=1), request(end=10, error={"timeout": True})]
    result = request_goodput(rows, metrics(), {"ttft_ms": 200, "tpot_ms": 100}, 0)
    assert result["value"] == 0.1
    assert result["good_requests"] == 1
    assert result["duration_seconds"] == 10
    assert "info" not in result


def test_no_targets_zero_attainment_and_missing_values_are_distinct():
    row = request()
    assert request_goodput([row], metrics(1, 1), {})["status"] == "unavailable"
    assert request_goodput([row], metrics(1, 1), {"ttft_ms": 1})["value"] == 0
    row["info"]["response_metrics"]["output_tokens"] = 1
    assert request_goodput([row], metrics(1, 1), {"tpot_ms": 100})["status"] == "unavailable"
    assert request_goodput([row], metrics(1, 1), {"ttft_ms": 1000})["value"] == 0.1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows.append(request()),
        lambda rows: rows[0].pop("error"),
        lambda rows: rows[0].update(start_time=float("nan")),
        lambda rows: rows[0]["info"]["response_metrics"].update(output_token_times=[-1]),
        lambda rows: rows[0].update(end_time=20),
    ],
)
def test_reject_incomplete_or_invalid_requests(mutate):
    rows = [request()]
    mutate(rows)
    assert request_goodput(rows, metrics(1, 1), {"ttft_ms": 1000})["status"] == "unavailable"


def test_stage_scope_is_explicit_and_warmup_excluded():
    rows = [request(stage=-1), request(stage=0), request(stage=1, first=1, end=2)]
    assert request_goodput(rows, metrics(1, 1), {"ttft_ms": 500}, 0)["value"] == 0.1
    assert request_goodput(rows, metrics(1, 1), {"ttft_ms": 500}, 1)["value"] == 0
    for row in rows:
        row.pop("stage_id")
    assert request_goodput(rows, metrics(3, 3), {"ttft_ms": 500}, 0)["status"] == "unavailable"


def trace():
    access = {
        "timestamp": "2026-09-10T00:00:01Z",
        "namespace": "model-revision:tokenizer:adapter:cache-group",
        "block_hash": "parent-chain-hash-A",
        "valid_tokens": 16,
    }
    return {
        "schema_version": 1,
        "complete": True,
        "dropped_events": 0,
        "stage_index": 0,
        "identity_semantics": "context-prefix-hash",
        "access_count": 2,
        "window": {"start": "2026-09-10T00:00:00Z", "end": "2026-09-10T00:01:00Z"},
        "accesses": [{**access, "pod": "p1", "tier": "hbm"}, {**access, "pod": "p2", "tier": "cpu"}],
    }


def test_working_set_deduplicates_placement_but_not_context():
    payload = trace()
    assert kv_working_set(payload, 0)["value"] == 16
    payload["accesses"][1]["namespace"] = "another-model"
    assert kv_working_set(payload, 0)["value"] == 32


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(complete=False),
        lambda p: p.update(dropped_events=1),
        lambda p: p.update(stage_index=1),
        lambda p: p.update(access_count=3),
        lambda p: p["accesses"][1].update(valid_tokens=32),
        lambda p: p["accesses"][1].update(timestamp="2026-09-10T00:01:00Z"),
        lambda p: p.update(identity_semantics="physical-block-id"),
    ],
)
def test_invalid_working_set_is_not_promoted(mutate):
    payload = trace()
    mutate(payload)
    assert kv_working_set(payload, 0)["status"] == "unavailable"


def test_native_stage_counter_uses_full_duration_and_matching_saved_constraints(tmp_path):
    path = tmp_path / "stage_0_lifecycle_metrics.json"
    path.write_text(json.dumps({"successes": {"goodput_metrics": {"good_requests": 2, "total_requests": 3}}}))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(enable_request_reports({}, {"ttft_ms": 200})))
    record = {
        "sla_targets": {"ttft_ms": 200},
        "rate_stage_results": [{"metrics": {**metrics(), "summary_path": str(path)}}],
    }
    (tmp_path / "stage_0_kv_access.json").write_text(json.dumps(trace()))
    assert apply_request_evidence(record)
    result = record["rate_stage_results"][0]["metrics"]
    assert result["request_slo_goodput"]["value"] == 0.2
    assert result["distinct_kv_working_set"]["value"] == 16
    assert not apply_request_evidence(record)
    record["sla_targets"]["ttft_ms"] = 300
    assert apply_request_evidence(record)
    assert result["request_slo_goodput"]["status"] == "unavailable"


def test_raw_report_ingestion_and_artifact_cleanup(tmp_path):
    path = tmp_path / "summary_lifecycle_metrics.json"
    path.write_text("{}")
    (tmp_path / "per_request_lifecycle_metrics.json").write_text(json.dumps([request()]))
    record = {"sla_targets": {"ttft_ms": 500}, "metrics": {**metrics(1, 1), "summary_path": str(path)}}
    assert apply_request_evidence(record)
    assert record["metrics"]["request_slo_goodput"]["value"] == 0.1
    path.unlink()
    saved = deepcopy(record)
    assert not apply_request_evidence(record)
    assert record == saved


def test_generated_workloads_enable_request_collection_and_slo_constraints():
    from llm_d_bench.evaluate.models import ConcurrencyStage, SharedPrefixWorkloadSpec, WorkloadMatrixPoint
    from llm_d_bench.evaluate.router import _matrix_workload_yaml, _shared_prefix_workload_yaml

    limits = {"ttft_ms": 200, "tpot_ms": 20}
    matrix = yaml.safe_load(
        _matrix_workload_yaml(
            WorkloadMatrixPoint(isl=128, osl=64),
            [ConcurrencyStage(concurrency=1, num_requests=10)],
            "model",
            "http://test",
            limits,
        )
    )
    shared = yaml.safe_load(
        _shared_prefix_workload_yaml(
            SharedPrefixWorkloadSpec(
                num_groups=2,
                num_prompts_per_group=2,
                system_prompt_len=128,
                question_len=32,
                output_len=64,
                stages=[{"rate": 1, "duration": 10}],
            ),
            "model",
            "http://test",
            limits,
        )
    )
    for workload in [matrix, shared]:
        assert workload["report"]["request_lifecycle"]["per_request"] is True
        assert workload["report"]["goodput"]["constraints"] == {"ttft": 0.2, "tpot": 0.02}


def test_recorded_empty_output_is_not_slo_good_even_if_transport_succeeded():
    row = request(tokens=0)
    row["info"]["response_metrics"]["output_token_times"] = []
    result = request_goodput([row], metrics(1, 1), {"ttft_ms": 1000})
    assert result["value"] == 0
    assert result["empty_output_count"] == 1
    row["info"]["response_metrics"]["output_tokens"] = 128
    assert request_goodput([row], metrics(1, 1), {"ttft_ms": 1000})["status"] == "unavailable"
