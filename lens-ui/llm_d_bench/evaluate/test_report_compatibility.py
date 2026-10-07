"""Regression coverage for standardized reports collected without raw lifecycle JSON."""

import importlib
import json

import pytest
import yaml

router = importlib.import_module("llm_d_bench.evaluate.router")


def write_report(root, version="0.2.1", stage=0):
    root.mkdir(parents=True, exist_ok=True)
    metrics = {
        "requests": {"total": 8, "failures": 0, "input_length": {"mean": 512}, "output_length": {"mean": 64}},
        "latency": {
            "time_to_first_token": {"p50": 0.071, "p95": 0.196534, "units": "s"},
            "inter_token_latency": {"p95": 0.088187, "units": "s/token"},
            "time_per_output_token": {"p95": 0.015253, "units": "s/token"},
        },
        "throughput": {
            "output_token_rate": {"mean": 46.587, "units": "tokens/s"},
            "request_rate": {"mean": 0.728, "units": "queries/s"},
        },
    }
    payload = {
        "version": version,
        "results": {"request_performance": {"aggregate": metrics}},
        "run": {"time": {"duration": "PT72S"}},
    }
    if version == "0.1":
        metrics["throughput"] = {"output_tokens_per_sec": 46.587, "requests_per_sec": 0.728}
        metrics["time"] = {"duration": 9.8}
        payload = {"version": version, "metrics": metrics}
    path = root / f"benchmark_report_v{version},_stage_{stage}_lifecycle_metrics.json.yaml"
    path.write_text(yaml.safe_dump(payload))
    return path


@pytest.mark.parametrize("version", ["0.1", "0.2", "0.2.1"])
def test_standardized_stage_reports_supply_client_metrics(tmp_path, version):
    write_report(tmp_path, version)
    metrics = router._stage_metric_summary(tmp_path, 0)
    assert metrics["success_rate"] == 100
    assert metrics["request_count"] == 8
    assert metrics["throughput_tps"] == 46.587
    assert metrics["throughput_rps"] == 0.728
    assert metrics["latency_distributions"]["ttft"]["p95_ms"] == 196.534
    assert metrics["latency_distributions"]["itl"]["p95_ms"] == 88.187
    assert metrics["latency_distributions"]["tpot"]["p95_ms"] == 15.253
    assert metrics["ttft_ms"] == 71
    # Standardized v0.2 run duration includes harness setup, not stage measurement.
    assert metrics["benchmark_time_seconds"] == (9.8 if version == "0.1" else None)
    assert router._stage_metric_summary(tmp_path, 1) == {}
    assert router._metric_summary(tmp_path) == {}


def test_raw_lifecycle_json_remains_preferred(tmp_path):
    write_report(tmp_path)
    (tmp_path / "stage_0_lifecycle_metrics.json").write_text(
        json.dumps({"successes": {"count": 3}, "failures": {"count": 1}})
    )
    assert router._stage_metric_summary(tmp_path, 0)["success_rate"] == 75


@pytest.mark.parametrize("output_key", ["output", "result_output"])
def test_backfill_recovers_matrix_stages_without_summary_path(tmp_path, output_key):
    write_report(tmp_path / "matrix-0")
    record = {
        output_key: str(tmp_path),
        "metrics": {"observability": {"status": "available"}},
        "matrix_results": [
            {"metrics": {}, "stage_metrics": [{"concurrency": 1, "metrics": {}}, {"concurrency": 8, "metrics": {}}]},
        ],
    }
    assert router._backfill_metric_summary(record)
    assert record["matrix_results"][0]["stage_metrics"][0]["metrics"]["success_rate"] == 100
    assert record["matrix_results"][0]["stage_metrics"][1]["metrics"] == {}
    assert record["matrix_results"][0]["metrics"] == {}
    assert record["metrics"] == {"observability": {"status": "available"}}
    assert not router._backfill_metric_summary(record)


def test_backfill_keeps_existing_measurements_and_recovers_rate_stages(tmp_path):
    write_report(tmp_path)
    record = {
        "output": str(tmp_path),
        "metrics": {},
        "rate_stage_results": [
            {"rate": 1, "metrics": {"throughput_tps": 123, "observability": {"status": "available"}}},
        ],
    }
    assert router._backfill_metric_summary(record)
    metrics = record["rate_stage_results"][0]["metrics"]
    assert metrics["throughput_tps"] == 123
    assert metrics["success_rate"] == 100
    assert metrics["observability"]["status"] == "available"
