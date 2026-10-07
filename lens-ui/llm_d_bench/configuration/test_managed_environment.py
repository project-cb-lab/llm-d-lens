"""Tests for the administrator-managed Hugging Face environment policy."""

from llm_d_bench.configuration.managed_environment import (
    admin_managed_environment_names,
    is_admin_managed_environment_variable,
)
from llm_d_bench.configuration.models import CandidateConfig, CandidateSource
from llm_d_bench.configuration.validators import validate_candidate


def test_admin_managed_variable_matching():
    for name in (
        "HF_HOME",
        "hf_token",
        "HF_HUB_OFFLINE",
        "HUGGINGFACE_HUB_CACHE",
        "HUGGING_FACE_HUB_TOKEN",
        "TRANSFORMERS_OFFLINE",
    ):
        assert is_admin_managed_environment_variable(name), name
    assert not is_admin_managed_environment_variable("VLLM_LOGGING_LEVEL")
    assert not is_admin_managed_environment_variable("")
    assert not is_admin_managed_environment_variable(None)


def test_admin_managed_names_only_inspects_environment_overrides():
    overrides = [
        {"target": "both", "kind": "argument", "name": "HF_HOME", "value": "x"},
        {"target": "both", "kind": "environment", "name": "VLLM_LOGGING_LEVEL", "value": "DEBUG"},
        {"target": "both", "kind": "environment", "name": "HF_HUB_OFFLINE", "value": "0"},
    ]
    assert admin_managed_environment_names(overrides) == ["HF_HUB_OFFLINE"]
    assert admin_managed_environment_names("not-a-list") == []


def _candidate(custom_parameters):
    return CandidateConfig(
        type="baseline",
        candidate_source=CandidateSource(name="manual"),
        target={"model": "Qwen/Qwen3-8B", "hardware": {"accelerator_count": 1}},
        serving={"replicas": 1, "tensor_parallel_size": 1},
        custom_parameters=custom_parameters,
    )


def test_validate_candidate_rejects_admin_managed_environment():
    result = validate_candidate(
        _candidate([{"target": "both", "kind": "environment", "name": "HF_HOME", "value": "/models"}])
    )
    assert result.status == "invalid"
    assert any("platform administrator" in error for error in result.errors)


def test_validate_candidate_allows_other_environment():
    result = validate_candidate(
        _candidate([{"target": "both", "kind": "environment", "name": "VLLM_LOGGING_LEVEL", "value": "DEBUG"}])
    )
    assert result.status != "invalid"
