"""Evaluate workflow persistence: case ids are unique per workflow, not globally."""

from __future__ import annotations

from uuid import uuid4

from llm_d_bench.db.dao.evaluate_workflow import EvaluateWorkflowDao
from llm_d_bench.db.evaluate_persistence_models import (
    EvaluateWorkflowCaseRecord,
    EvaluateWorkflowRecord,
)


def _workflow() -> EvaluateWorkflowRecord:
    workflow_id = str(uuid4())
    return EvaluateWorkflowRecord(
        id=workflow_id,
        status="queued",
        cluster_id="c1",
        cases=[
            EvaluateWorkflowCaseRecord(id="guide-1-1", status="queued"),
            EvaluateWorkflowCaseRecord(id="baseline-1", status="queued"),
        ],
    )


def test_two_workflows_may_share_case_ids():
    dao = EvaluateWorkflowDao()
    first = dao.create(_workflow())
    second = dao.create(_workflow())

    assert first.id != second.id
    assert {case.id for case in dao.get(first.id).cases} == {"guide-1-1", "baseline-1"}
    assert {case.id for case in dao.get(second.id).cases} == {"guide-1-1", "baseline-1"}
    # Deleting one workflow cascades only its own cases.
    dao.delete(first.id)
    assert dao.get(first.id) is None
    assert {case.id for case in dao.get(second.id).cases} == {"guide-1-1", "baseline-1"}
