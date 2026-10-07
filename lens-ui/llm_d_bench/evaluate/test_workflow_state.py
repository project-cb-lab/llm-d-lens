import pytest

from llm_d_bench.evaluate.workflow_state import (
    BENCHMARK_TRANSITIONS,
    CASE_TRANSITIONS,
    WORKFLOW_TRANSITIONS,
    BenchmarkStatus,
    CaseStatus,
    LifecycleError,
    WorkflowStatus,
    can_transition,
    is_terminal,
    require_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkflowStatus.QUEUED, WorkflowStatus.RUNNING),
        (WorkflowStatus.RUNNING, WorkflowStatus.SUCCEEDED),
        (WorkflowStatus.CANCELLING, WorkflowStatus.CANCELLED),
        (WorkflowStatus.FAILED, WorkflowStatus.QUEUED),
    ],
)
def test_workflow_transitions_allow_execution_and_retry(current, target) -> None:
    assert can_transition(current, target, WORKFLOW_TRANSITIONS)


def test_workflow_transition_rejects_terminal_regression() -> None:
    with pytest.raises(LifecycleError, match="succeeded -> running"):
        require_transition(WorkflowStatus.SUCCEEDED, WorkflowStatus.RUNNING, WORKFLOW_TRANSITIONS)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (CaseStatus.QUEUED, CaseStatus.DEPLOYING),
        (CaseStatus.QUEUED, CaseStatus.BENCHMARKING),
        (CaseStatus.DEPLOYING, CaseStatus.BENCHMARKING),
        (CaseStatus.BENCHMARKING, CaseStatus.SUCCEEDED),
        (CaseStatus.FAILED, CaseStatus.QUEUED),
        (CaseStatus.FAILED, CaseStatus.BENCHMARKING),
    ],
)
def test_case_transitions_cover_deployment_reuse_and_retry(current, target) -> None:
    assert can_transition(current, target, CASE_TRANSITIONS)


def test_benchmark_terminal_states_are_explicit() -> None:
    assert is_terminal(BenchmarkStatus.SUCCEEDED, BENCHMARK_TRANSITIONS)
    assert is_terminal(BenchmarkStatus.FAILED, BENCHMARK_TRANSITIONS)
    assert not is_terminal(BenchmarkStatus.RUNNING, BENCHMARK_TRANSITIONS)
