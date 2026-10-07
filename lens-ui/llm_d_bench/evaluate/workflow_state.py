"""Pure lifecycle rules for Evaluation workflows, cases, and benchmark runs."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class LifecycleError(ValueError):
    """Raised when a persisted Evaluation object attempts an invalid transition."""


class WorkflowStatus(StrEnum):
    QUEUED = "queued"
    DEPLOYING = "deploying"
    RUNNING = "running"
    BENCHMARKING = "benchmarking"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CaseStatus(StrEnum):
    QUEUED = "queued"
    DEPLOYING = "deploying"
    BENCHMARKING = "benchmarking"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BenchmarkStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


WORKFLOW_TRANSITIONS: Mapping[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.QUEUED: frozenset(
        {WorkflowStatus.DEPLOYING, WorkflowStatus.RUNNING, WorkflowStatus.CANCELLING, WorkflowStatus.FAILED}
    ),
    WorkflowStatus.DEPLOYING: frozenset(
        {WorkflowStatus.BENCHMARKING, WorkflowStatus.CANCELLING, WorkflowStatus.FAILED}
    ),
    WorkflowStatus.RUNNING: frozenset(
        {WorkflowStatus.SUCCEEDED, WorkflowStatus.CANCELLING, WorkflowStatus.CANCELLED, WorkflowStatus.FAILED}
    ),
    WorkflowStatus.BENCHMARKING: frozenset(
        {WorkflowStatus.SUCCEEDED, WorkflowStatus.CANCELLING, WorkflowStatus.CANCELLED, WorkflowStatus.FAILED}
    ),
    WorkflowStatus.CANCELLING: frozenset({WorkflowStatus.CANCELLED, WorkflowStatus.FAILED}),
    WorkflowStatus.SUCCEEDED: frozenset(),
    WorkflowStatus.FAILED: frozenset({WorkflowStatus.QUEUED}),
    WorkflowStatus.CANCELLED: frozenset({WorkflowStatus.QUEUED}),
}

CASE_TRANSITIONS: Mapping[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.QUEUED: frozenset(
        {CaseStatus.DEPLOYING, CaseStatus.BENCHMARKING, CaseStatus.FAILED, CaseStatus.CANCELLED}
    ),
    CaseStatus.DEPLOYING: frozenset({CaseStatus.BENCHMARKING, CaseStatus.FAILED, CaseStatus.CANCELLED}),
    CaseStatus.BENCHMARKING: frozenset({CaseStatus.SUCCEEDED, CaseStatus.FAILED, CaseStatus.CANCELLED}),
    CaseStatus.SUCCEEDED: frozenset({CaseStatus.QUEUED}),
    CaseStatus.FAILED: frozenset({CaseStatus.QUEUED, CaseStatus.BENCHMARKING}),
    CaseStatus.CANCELLED: frozenset({CaseStatus.QUEUED}),
}

BENCHMARK_TRANSITIONS: Mapping[BenchmarkStatus, frozenset[BenchmarkStatus]] = {
    BenchmarkStatus.QUEUED: frozenset({BenchmarkStatus.RUNNING, BenchmarkStatus.CANCELLING, BenchmarkStatus.FAILED}),
    BenchmarkStatus.RUNNING: frozenset(
        {BenchmarkStatus.CANCELLING, BenchmarkStatus.SUCCEEDED, BenchmarkStatus.FAILED, BenchmarkStatus.CANCELLED}
    ),
    BenchmarkStatus.CANCELLING: frozenset({BenchmarkStatus.CANCELLED, BenchmarkStatus.FAILED}),
    BenchmarkStatus.SUCCEEDED: frozenset(),
    BenchmarkStatus.FAILED: frozenset(),
    BenchmarkStatus.CANCELLED: frozenset(),
}


def can_transition(current: StrEnum, target: StrEnum, rules: Mapping[StrEnum, frozenset[StrEnum]]) -> bool:
    return current == target or target in rules.get(current, frozenset())


def require_transition(current: StrEnum, target: StrEnum, rules: Mapping[StrEnum, frozenset[StrEnum]]) -> None:
    if not can_transition(current, target, rules):
        raise LifecycleError(f"invalid lifecycle transition: {current.value} -> {target.value}")


def is_terminal(status: StrEnum, rules: Mapping[StrEnum, frozenset[StrEnum]]) -> bool:
    return not rules.get(status, frozenset())


# A retryable failure is still terminal for polling, deletion and resource usage.
TERMINAL_EVALUATION_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
