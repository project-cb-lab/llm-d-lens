"""Tests for the AIC estimate adapter."""

from types import SimpleNamespace

import pytest
import yaml

from llm_d_bench.aic import service


def test_estimate_rejects_a_different_aggregated_topology(monkeypatch):
    monkeypatch.setattr(
        service,
        "experiments_sync",
        lambda request: SimpleNamespace(
            experiments=[
                {
                    "mode": "agg",
                    "tp": 2,
                    "replicas": 4,
                    "ttft_ms": 120,
                    "tpot_ms": 20,
                }
            ]
        ),
    )

    with pytest.raises(service.AICError, match="topology"):
        service.estimate_sync(
            service.AICEstimateRequest(
                scenario="inference_scheduling",
                model_name="Qwen/Qwen3-8B",
                gpu_count=4,
                tp=2,
                replicas=2,
                ttft_target_ms=200,
            )
        )


def test_estimate_passes_exact_aggregated_topology_and_workload(monkeypatch):
    captured = {}

    def experiments(request):
        captured.update(yaml.safe_load(request.yaml_text)["manual"])
        return SimpleNamespace(
            experiments=[
                {
                    "mode": "agg",
                    "tp": 2,
                    "replicas": 2,
                    "ttft_ms": 120,
                    "tpot_ms": 20,
                }
            ]
        )

    monkeypatch.setattr(service, "experiments_sync", experiments)
    result = service.estimate_sync(
        service.AICEstimateRequest(
            scenario="inference_scheduling",
            model_name="Qwen/Qwen3-8B",
            gpu_count=4,
            tp=2,
            replicas=2,
            mean_input_tokens=4096,
            mean_output_tokens=256,
            ttft_target_ms=200,
        )
    )

    assert result["ttft_ms"] == 120
    assert captured["total_gpus"] == 4
    assert captured["agg_tp_candidates"] == [2]
    assert captured["agg_num_gpu_candidates"] == [2]
    assert captured["isl"] == 4096


def test_estimate_rejects_inconsistent_gpu_count(monkeypatch):
    monkeypatch.setattr(service, "experiments_sync", lambda request: pytest.fail("SDK must not be called"))

    with pytest.raises(service.AICError, match="requires 4 GPUs"):
        service.estimate_sync(
            service.AICEstimateRequest(
                scenario="inference_scheduling",
                model_name="Qwen/Qwen3-8B",
                gpu_count=8,
                tp=2,
                replicas=2,
            )
        )


def test_estimate_rejects_unverifiable_pipeline_parallelism(monkeypatch):
    monkeypatch.setattr(service, "experiments_sync", lambda request: pytest.fail("SDK must not be called"))
    with pytest.raises(service.AICError, match="PP greater than 1"):
        service.estimate_sync(
            service.AICEstimateRequest(
                scenario="inference_scheduling",
                model_name="Qwen/Qwen3-8B",
                gpu_count=4,
                tp=2,
                pp=2,
            )
        )


def test_estimate_rejects_a_different_disaggregated_worker_allocation(monkeypatch):
    monkeypatch.setattr(
        service,
        "experiments_sync",
        lambda request: SimpleNamespace(
            experiments=[
                {
                    "mode": "disagg",
                    "prefill_tp": 2,
                    "prefill_replicas": 1,
                    "decode_tp": 1,
                    "decode_replicas": 2,
                    "ttft_ms": 120,
                    "tpot_ms": 20,
                }
            ]
        ),
    )

    with pytest.raises(service.AICError, match="topology"):
        service.estimate_sync(
            service.AICEstimateRequest(
                scenario="pd_disaggregation",
                model_name="Qwen/Qwen3-8B",
                gpu_count=3,
                prefill_tp=2,
                prefill_replicas=1,
                decode_tp=1,
                decode_replicas=1,
                ttft_target_ms=200,
            )
        )


def test_estimate_accepts_matching_disaggregated_topology(monkeypatch):
    captured = {}

    def experiments(request):
        captured.update(yaml.safe_load(request.yaml_text)["manual"])
        return SimpleNamespace(
            experiments=[
                {
                    "mode": "disagg",
                    "prefill_tp": 2,
                    "prefill_replicas": 1,
                    "decode_tp": 1,
                    "decode_replicas": 2,
                    "num_total_gpus": 4,
                    "ttft_ms": 180,
                    "tpot_ms": 25,
                }
            ]
        )

    monkeypatch.setattr(service, "experiments_sync", experiments)
    result = service.estimate_sync(
        service.AICEstimateRequest(
            scenario="pd_disaggregation",
            model_name="Qwen/Qwen3-8B",
            gpu_count=4,
            prefill_tp=2,
            prefill_replicas=1,
            decode_tp=1,
            decode_replicas=2,
            ttft_target_ms=200,
        )
    )

    assert result["tpot_ms"] == 25
    assert captured["prefill_tp_candidates"] == [2]
    assert captured["decode_tp_candidates"] == [1]
    assert captured["total_gpus"] == 4


def test_estimate_without_targets_uses_direct_aggregated_prediction(monkeypatch):
    from aiconfigurator.cli import api

    captured = {}

    def direct(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            ttft=45,
            tpot=7,
            raw={
                "tp": 1,
                "pp": 1,
                "num_total_gpus": 1,
                "tokens/s": 150,
            },
        )

    monkeypatch.setattr(api, "cli_estimate", direct)
    result = service.estimate_sync(
        service.AICEstimateRequest(
            scenario="inference_scheduling",
            model_name="Qwen/Qwen3-0.6B",
            gpu_count=1,
            tp=1,
            replicas=1,
            mean_input_tokens=1024,
            mean_output_tokens=256,
        )
    )

    assert result["ttft_ms"] == 45
    assert result["throughput_tokens_per_sec"] == 150
    assert captured["isl"] == 1024
    assert captured["osl"] == 256
    assert captured["batch_size"] == 1
    assert "ttft" not in captured and "tpot" not in captured


def test_estimate_without_targets_rejects_mismatched_direct_topology(monkeypatch):
    from aiconfigurator.cli import api

    monkeypatch.setattr(
        api,
        "cli_estimate",
        lambda **kwargs: SimpleNamespace(
            ttft=45,
            tpot=7,
            raw={"(p)tp": 1, "(p)workers": 1, "(d)tp": 1, "(d)workers": 2, "num_total_gpus": 3},
        ),
    )
    with pytest.raises(service.AICError, match="topology"):
        service.estimate_sync(
            service.AICEstimateRequest(
                scenario="pd_disaggregation",
                model_name="Qwen/Qwen3-0.6B",
                gpu_count=2,
                prefill_tp=1,
                prefill_replicas=1,
                decode_tp=1,
                decode_replicas=1,
            )
        )


def test_estimate_without_targets_accepts_matching_direct_disaggregation(monkeypatch):
    from aiconfigurator.cli import api

    captured = {}

    def direct(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            ttft=44,
            tpot=7,
            raw={
                "(p)tp": 1,
                "(p)workers": 2,
                "(d)tp": 1,
                "(d)workers": 3,
                "num_total_gpus": 5,
                "tokens/s": 300,
            },
        )

    monkeypatch.setattr(api, "cli_estimate", direct)
    result = service.estimate_sync(
        service.AICEstimateRequest(
            scenario="pd_disaggregation",
            model_name="Qwen/Qwen3-0.6B",
            gpu_count=5,
            prefill_tp=1,
            prefill_replicas=2,
            decode_tp=1,
            decode_replicas=3,
            mean_input_tokens=1024,
            mean_output_tokens=256,
        )
    )

    assert result["throughput_tokens_per_sec"] == 300
    assert result["prefill_replicas"] == 2
    assert result["decode_replicas"] == 3
    assert captured["prefill_num_workers"] == 2
    assert captured["decode_num_workers"] == 3
    assert captured["isl"] == 1024 and captured["osl"] == 256
    assert "ttft" not in captured and "tpot" not in captured


def test_estimate_without_targets_does_not_infer_multi_replica_throughput(monkeypatch):
    from aiconfigurator.cli import api

    monkeypatch.setattr(
        api,
        "cli_estimate",
        lambda **kwargs: SimpleNamespace(
            ttft=45,
            tpot=7,
            raw={"tp": 1, "pp": 1, "num_total_gpus": 1, "tokens/s": 150},
        ),
    )
    result = service.estimate_sync(
        service.AICEstimateRequest(
            scenario="inference_scheduling",
            model_name="Qwen/Qwen3-0.6B",
            gpu_count=2,
            tp=1,
            replicas=2,
        )
    )

    assert result["replicas"] == 2
    assert result["throughput_tokens_per_sec"] is None
