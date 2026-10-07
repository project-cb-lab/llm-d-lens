"""Evaluation runs can target a published Model Service instead of a raw execution.

Covers model_service_group_id/deployment_execution_id mutual exclusivity on the
request contract (resolution of a group to a concrete member is covered by
``llm_d_bench/model_service/test_resolution.py``, the shared resolver both
Evaluation and Simulation use).
"""

from __future__ import annotations

import pytest

from llm_d_bench.evaluate.models import EvaluateRunRequest


def test_request_requires_exactly_one_target():
    with pytest.raises(ValueError, match="exactly one"):
        EvaluateRunRequest()
    with pytest.raises(ValueError, match="exactly one"):
        EvaluateRunRequest(deployment_execution_id="execution", model_service_group_id="msg-1", api_key="lens-mk-x")


def test_model_service_target_requires_an_api_key():
    with pytest.raises(ValueError, match="api_key"):
        EvaluateRunRequest(model_service_group_id="msg-1")


def test_deployment_execution_request_does_not_require_an_api_key():
    request = EvaluateRunRequest(deployment_execution_id="execution")
    assert request.api_key is None
