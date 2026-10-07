"""Public Simulation backend API."""

from typing import Any

from ..models import SimulationResponseCodeIssueSlice, SimulationTask
from . import registry as _registry
from .base import SCENARIOS, Backend, CommandBackend, artifact, command_for_log
from .registry import get_backend, list_backends, list_scenarios, register_backend


def completion_timeline_from_artifacts(
    task: SimulationTask,
    *,
    tolerate_incomplete: bool = False,
) -> list[dict[str, Any]]:
    return _registry._get_registered_backend(task.simulation.backend).completion_timeline_from_artifacts(
        task, tolerate_incomplete=tolerate_incomplete
    )


def live_completion_timeline_from_artifacts(task: SimulationTask) -> list[dict[str, Any]]:
    return completion_timeline_from_artifacts(task, tolerate_incomplete=True)


def latency_timelines_from_artifacts(
    task: SimulationTask,
    *,
    tolerate_incomplete: bool = False,
) -> dict[str, Any]:
    return _registry._get_registered_backend(task.simulation.backend).latency_timelines_from_artifacts(
        task, tolerate_incomplete=tolerate_incomplete
    )


def rate_timelines_from_artifacts(
    task: SimulationTask,
    *,
    tolerate_incomplete: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _registry._get_registered_backend(task.simulation.backend).rate_timelines_from_artifacts(
        task, tolerate_incomplete=tolerate_incomplete
    )


def goodput_timeline_from_artifacts(
    task: SimulationTask,
    *,
    tolerate_incomplete: bool = False,
) -> list[dict[str, Any]]:
    return _registry._get_registered_backend(task.simulation.backend).goodput_timeline_from_artifacts(
        task, tolerate_incomplete=tolerate_incomplete
    )


def status_code_breakdown_from_artifacts(
    task: SimulationTask,
    *,
    tolerate_incomplete: bool = False,
) -> list[dict[str, Any]]:
    return _registry._get_registered_backend(task.simulation.backend).status_code_breakdown_from_artifacts(
        task, tolerate_incomplete=tolerate_incomplete
    )


def response_code_issues_from_artifacts(
    task: SimulationTask,
    status_code: int | None,
    *,
    offset: int = 0,
    limit: int = 100,
    tolerate_incomplete: bool = False,
) -> SimulationResponseCodeIssueSlice:
    return _registry._get_registered_backend(task.simulation.backend).response_code_issues_from_artifacts(
        task,
        status_code,
        offset=offset,
        limit=limit,
        tolerate_incomplete=tolerate_incomplete,
    )


def live_summary_from_artifacts(task: SimulationTask) -> dict[str, Any]:
    return _registry._get_registered_backend(task.simulation.backend).live_summary_from_artifacts(task)


__all__ = [
    "Backend",
    "CommandBackend",
    "SCENARIOS",
    "artifact",
    "command_for_log",
    "completion_timeline_from_artifacts",
    "get_backend",
    "goodput_timeline_from_artifacts",
    "latency_timelines_from_artifacts",
    "list_backends",
    "list_scenarios",
    "live_completion_timeline_from_artifacts",
    "live_summary_from_artifacts",
    "rate_timelines_from_artifacts",
    "register_backend",
    "response_code_issues_from_artifacts",
    "status_code_breakdown_from_artifacts",
]
