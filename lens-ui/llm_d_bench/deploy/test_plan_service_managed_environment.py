"""Deployment-run creation rejects user-configured Hugging Face environment."""

import pytest

from llm_d_bench.deploy.contracts import (
    DeployableConfiguration,
    DeploymentRunCreateRequest,
    RuntimeBinding,
)
from llm_d_bench.deploy.plan_service import DeploymentPlanService


class _Store:
    def create_run(self, run):
        return run


class _Planner:
    def plan(self, configurations, *, run_id, provenance):
        return []


def _configuration(content):
    return DeployableConfiguration(
        type="baseline",
        format="helm",
        provider_ref="baseline-vllm",
        checksum="checksum",
        content=content,
    )


def _service():
    return DeploymentPlanService(_Store(), _Planner())


def test_create_run_rejects_admin_managed_custom_environment():
    request = DeploymentRunCreateRequest(
        configurations=[
            _configuration(
                {"customParameters": [{"target": "both", "kind": "environment", "name": "HF_HOME", "value": "/models"}]}
            )
        ]
    )
    with pytest.raises(ValueError, match="platform administrator"):
        _service().create_run(request)


def test_create_run_rejects_admin_managed_runtime_binding_environment():
    request = DeploymentRunCreateRequest(
        configurations=[_configuration({"runtime": {"image": "img:tag"}})],
        runtime_binding=RuntimeBinding(environment={"HF_TOKEN": "secret"}),
    )
    with pytest.raises(ValueError, match="platform administrator"):
        _service().create_run(request)


def test_create_run_allows_other_custom_environment():
    request = DeploymentRunCreateRequest(
        configurations=[
            _configuration(
                {
                    "customParameters": [
                        {"target": "both", "kind": "environment", "name": "VLLM_LOGGING_LEVEL", "value": "DEBUG"}
                    ]
                }
            )
        ]
    )
    assert _service().create_run(request) is not None
