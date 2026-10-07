"""Backend-neutral Simulation backend contracts and command execution."""

from __future__ import annotations

import asyncio
import math
import shlex
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from ..errors import SimulationConfigurationError, SimulationError
from ..incluster import execute_command_in_pod, resolve_incluster_target
from ..models import (
    ArtifactRecord,
    BackendAvailability,
    BackendCommand,
    BackendDescriptor,
    ScenarioDescriptor,
    SimulationArtifact,
    SimulationResponseCodeIssue,
    SimulationResponseCodeIssueSlice,
    SimulationResult,
    SimulationTask,
)
from ..process import CommandResult, RunContext, executable_availability, execute_command
from ..traces import trace_registry
from .analytics import (
    completion_timeline,
    goodput_timeline,
    latency_heatmap,
    latency_timeline,
    rate_timelines,
    status_code_breakdown,
)

SCENARIOS = [
    ScenarioDescriptor(
        name="chat",
        display_name="Chat",
        description="Conversational and general assistant traffic.",
    ),
    ScenarioDescriptor(
        name="api-calling",
        display_name="Tool & API Use",
        description="Tool-use, agent, and business API traffic.",
    ),
    ScenarioDescriptor(
        name="coding",
        display_name="Code Generation",
        description="Code generation and software engineering traffic.",
    ),
]


def command_for_log(executable: str, args: Sequence[str]) -> str:
    sanitized = list(args)
    for index, value in enumerate(sanitized[:-1]):
        if value == "--api-key":
            sanitized[index + 1] = "<redacted>"
            continue
        if value not in {"--endpoint", "--url"}:
            continue
        parsed = urlparse(sanitized[index + 1])
        hostname = parsed.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
        sanitized[index + 1] = parsed._replace(netloc=netloc, params="", query="", fragment="").geturl()
    return shlex.join([executable, *sanitized])


def artifact(kind: str, path: Path, media_type: str) -> SimulationArtifact:
    return SimulationArtifact(kind=kind, path=str(path), media_type=media_type)


class Backend(ABC):
    name: str
    display_name: str
    trace_formats: ClassVar[list[str]]
    capabilities: ClassVar[dict[str, Any]]
    scenarios: ClassVar[list[ScenarioDescriptor]] = SCENARIOS
    tolerate_timeout: ClassVar[bool] = False

    async def validate(self, task: SimulationTask) -> None:
        options = task.simulation.backend_options
        if "error_rate_slo" in options:
            value = options["error_rate_slo"]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= 100
            ):
                raise SimulationConfigurationError("error_rate_slo must be a percentage from 0 to 100")
        for name in ("ttft_slo", "tpot_slo"):
            if name not in options:
                continue
            value = options[name]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise SimulationConfigurationError(f"{name} must be a non-negative number")
        await self.validate_backend(task)

    @abstractmethod
    async def validate_backend(self, task: SimulationTask) -> None: ...

    @abstractmethod
    async def run(self, task: SimulationTask, context: RunContext) -> SimulationResult: ...

    def availability(self) -> BackendAvailability:
        return BackendAvailability(
            available=True,
            version=self.backend_version(None),
            unavailable_reason=None,
        )

    def descriptor(self) -> BackendDescriptor:
        availability = self.availability()
        return BackendDescriptor(
            name=self.name,
            display_name=self.display_name,
            api_version=1,
            available=availability.available,
            version=availability.version,
            unavailable_reason=availability.unavailable_reason,
            scenarios=self.scenarios,
            capabilities={**self.capabilities, "trace_formats": self.trace_formats},
        )

    def backend_version(self, detected_version: str | None) -> str | None:
        return detected_version

    def artifact_records(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> list[ArtifactRecord]:
        return []

    def artifact_time_origin(self, records: list[ArtifactRecord]) -> float:
        return 0

    def expected_request_count(self, task: SimulationTask) -> int | None:
        trace = task.prompt.trace
        count = trace_registry.resolve(trace.path, trace.format).expected_request_count(
            trace,
            task.simulation.duration_seconds,
            include_end=self.name == "trace-replayer",
        )
        if count is not None and self.name == "aiperf":
            count *= int(task.simulation.backend_options.get("num_profile_runs", 1))
        return count

    def arrival_schedule_seconds(self, task: SimulationTask) -> list[float]:
        """Simulation-relative arrival times for every request the trace schedules
        within the replay window, independent of whether each request completed.

        Returns an empty list when the trace is unavailable so callers can fall
        back to per-request records.
        """
        trace = task.prompt.trace
        try:
            resolved = trace_registry.resolve(trace.path, trace.format)
            end_seconds = (
                trace.end_seconds
                if trace.end_seconds is not None
                else trace.start_seconds + task.simulation.duration_seconds * trace.synthesis_speedup_ratio
            )
            return resolved.arrival_schedule(
                start_seconds=trace.start_seconds,
                end_seconds=end_seconds,
                speedup_ratio=trace.synthesis_speedup_ratio,
                include_end=self.name == "trace-replayer",
            )
        except (OSError, ValueError, TypeError, SimulationError):
            return []

    def completed_request_count(self, task: SimulationTask) -> int:
        return len(self.artifact_records(task, tolerate_incomplete=True))

    def completion_timeline_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        records = self.artifact_records(task, tolerate_incomplete=tolerate_incomplete)
        origin = self.artifact_time_origin(records)
        schedule = self.arrival_schedule_seconds(task)
        arrival_seconds = (
            [origin + value for value in schedule]
            if schedule
            else [record.arrived for record in records if record.arrived is not None]
        )
        return completion_timeline(
            [record.completed for record in records if record.completed is not None],
            arrival_seconds=arrival_seconds,
            successful_completion_seconds=[
                record.completed for record in records if record.completed is not None and not record.failed_issue
            ],
            failed_completion_seconds=[
                record.completed for record in records if record.completed is not None and record.failed_issue
            ],
            client_timeout_completion_seconds=[
                record.completed
                for record in records
                if record.completed is not None and record.failed_issue and record.status_code is None
            ],
            backend_error_completion_seconds=[
                record.completed
                for record in records
                if record.completed is not None and record.failed_issue and record.status_code is not None
            ],
            origin_seconds=origin,
        )

    def latency_timelines_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> dict[str, Any]:
        records = self.artifact_records(task, tolerate_incomplete=tolerate_incomplete)
        origin = self.artifact_time_origin(records)
        arrivals = [record.latency_arrival for record in records if record.latency_arrival is not None]
        completions = [record.latency_completion for record in records if record.latency_completion is not None]

        def metric(name: str) -> list[tuple[float, float]]:
            values: list[tuple[float, float]] = []
            for record in records:
                arrived = record.latency_arrival
                value = getattr(record, name)
                if arrived is not None and value is not None:
                    values.append((arrived, value))
            return values

        end = max(completions) if completions else None
        ttft_heatmap = latency_heatmap(
            [
                (record.latency_arrival, record.input_tokens, record.ttft_ms)
                for record in records
                if record.latency_arrival is not None and record.input_tokens is not None and record.ttft_ms is not None
            ],
            metric="ttft",
            sequence_length="input",
            sequence_length_source="observed",
            origin_seconds=origin,
        )
        tpot_heatmap = latency_heatmap(
            [
                (
                    record.latency_arrival,
                    (
                        record.requested_output_tokens
                        if record.requested_output_tokens is not None
                        else record.output_tokens
                    ),
                    record.tpot_ms,
                )
                for record in records
                if record.latency_arrival is not None
                and (record.requested_output_tokens is not None or record.output_tokens is not None)
                and record.tpot_ms is not None
            ],
            metric="tpot",
            sequence_length="output",
            sequence_length_source="requested",
            origin_seconds=origin,
        )
        return {
            "latency_timeline": latency_timeline(
                metric("latency_ms"),
                arrival_seconds=arrivals,
                origin_seconds=origin,
                end_seconds=end,
            ),
            "ttft_timeline": latency_timeline(
                metric("ttft_ms"),
                arrival_seconds=arrivals,
                origin_seconds=origin,
                end_seconds=end,
            ),
            "tpot_timeline": latency_timeline(
                metric("tpot_ms"),
                arrival_seconds=arrivals,
                origin_seconds=origin,
                end_seconds=end,
            ),
            "ttft_heatmap": ttft_heatmap.model_dump(mode="json") if ttft_heatmap is not None else None,
            "tpot_heatmap": tpot_heatmap.model_dump(mode="json") if tpot_heatmap is not None else None,
        }

    def rate_timelines_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        records = self.artifact_records(task, tolerate_incomplete=tolerate_incomplete)
        return rate_timelines(
            [
                (
                    record.arrived,
                    record.rate_completion,
                    record.rate_tokens,
                    record.failed_issue,
                )
                for record in records
                if record.arrived is not None and record.rate_completion is not None
            ],
            origin_seconds=self.artifact_time_origin(records),
        )

    def goodput_timeline_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        records = self.artifact_records(task, tolerate_incomplete=tolerate_incomplete)
        options = task.simulation.backend_options
        ttft_slo_ms = float(options["ttft_slo"]) * 1000 if "ttft_slo" in options else None
        tpot_slo_ms = float(options["tpot_slo"]) * 1000 if "tpot_slo" in options else None

        def qualifies(record: ArtifactRecord) -> bool:
            return (
                record.successful
                and not record.failed_issue
                and (ttft_slo_ms is None or (record.ttft_ms is not None and record.ttft_ms <= ttft_slo_ms))
                and (tpot_slo_ms is None or (record.tpot_ms is not None and record.tpot_ms <= tpot_slo_ms))
            )

        return [
            point.model_dump(mode="json")
            for point in goodput_timeline(
                [
                    (record.rate_completion, qualifies(record))
                    for record in records
                    if record.rate_completion is not None
                ],
                origin_seconds=self.artifact_time_origin(records),
            )
        ]

    def status_code_breakdown_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        records = self.artifact_records(task, tolerate_incomplete=tolerate_incomplete)
        return status_code_breakdown(Counter(record.status_code for record in records if record.failed_issue))

    def response_code_issues_from_artifacts(
        self,
        task: SimulationTask,
        status_code: int | None,
        *,
        offset: int = 0,
        limit: int = 100,
        tolerate_incomplete: bool = False,
    ) -> SimulationResponseCodeIssueSlice:
        records = self.artifact_records(task, tolerate_incomplete=tolerate_incomplete)
        matching = [
            (index, record)
            for index, record in enumerate(records, 1)
            if record.failed_issue and record.status_code == status_code
        ]
        issues = []
        for request_number, record in matching[offset : offset + limit]:
            latency_ms = (
                max(0, record.completed - record.arrived) * 1000
                if record.arrived is not None and record.completed is not None
                else None
            )
            issues.append(
                SimulationResponseCodeIssue(
                    request_number=request_number,
                    request_id=record.request_id,
                    status_code=record.status_code,
                    error=record.error,
                    started_at_seconds=record.arrived,
                    latency_ms=latency_ms,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                )
            )
        return SimulationResponseCodeIssueSlice(total=len(matching), items=issues)

    def live_summary_from_artifacts(self, task: SimulationTask) -> dict[str, Any]:
        completion = self.completion_timeline_from_artifacts(task, tolerate_incomplete=True)
        latency = self.latency_timelines_from_artifacts(task, tolerate_incomplete=True)
        throughput, errors = self.rate_timelines_from_artifacts(task, tolerate_incomplete=True)
        goodput = self.goodput_timeline_from_artifacts(task, tolerate_incomplete=True)
        duration = throughput[-1]["end_seconds"] if throughput else 0
        records = self.artifact_records(task, tolerate_incomplete=True)
        origin = self.artifact_time_origin(records)
        completed = sum(point["completed_requests"] for point in throughput)
        output_tokens = sum(point["output_tokens"] for point in throughput)
        successful = sum(point["successful_requests"] for point in completion)
        failed = sum(point["failed_requests"] for point in completion)
        total = successful + failed

        def mean_metric(timeline: list[dict[str, Any]]) -> dict[str, float | None]:
            samples = [
                (point["request_count"], point["average_latency_ms"])
                for point in timeline
                if point["request_count"] and point["average_latency_ms"] is not None
            ]
            count = sum(item_count for item_count, _ in samples)
            return {"mean_ms": sum(item_count * value for item_count, value in samples) / count if count else None}

        return {
            "completion_timeline": completion,
            "throughput_timeline": throughput,
            "goodput_timeline": goodput,
            "error_timeline": errors,
            "timeline_origin_seconds": origin,
            "status_code_breakdown": self.status_code_breakdown_from_artifacts(task, tolerate_incomplete=True),
            "total_requests": total,
            "successful_requests": successful,
            "failed_requests": failed,
            "success_rate": successful / total * 100 if total else None,
            "throughput_rps": completed / duration if duration else None,
            "throughput_tps": output_tokens / duration if duration else None,
            "ttft": mean_metric(latency["ttft_timeline"]),
            "tpot": mean_metric(latency["tpot_timeline"]),
        }


class CommandBackend(Backend):
    #: Bare command name (e.g. ``"aiperf"``) if this backend can be installed
    #: on demand *inside* an in-cluster simulation pod via ``pip install``.
    #: When set, in-cluster runs skip host-side installation/availability
    #: checks entirely (the host never needs the tool) and the pod-side
    #: bootstrap in :mod:`llm_d_bench.simulation.incluster` installs it by
    #: name. Backends that require a host toolchain to build (e.g.
    #: trace-replayer via cargo) leave this ``None`` and keep relying on the
    #: host-built artifact being staged into the pod.
    pip_installable_in_pod: ClassVar[str | None] = None

    @property
    @abstractmethod
    def executable(self) -> str: ...

    @abstractmethod
    async def command(self, task: SimulationTask) -> BackendCommand: ...

    @abstractmethod
    async def parse(
        self,
        task: SimulationTask,
        command: CommandResult,
        version: str | None,
    ) -> SimulationResult: ...

    def availability(self) -> BackendAvailability:
        return executable_availability(self.executable)

    async def prepare_executable(self, context: RunContext) -> str:
        return self.executable

    async def run(self, task: SimulationTask, context: RunContext) -> SimulationResult:
        context.progress(0, "Preparing simulation")
        await self.validate(task)
        incluster_target = resolve_incluster_target(task)
        if task.endpoint_mode == "deployment" and incluster_target is None:
            # Deployment-bound tasks must always run from inside an in-cluster
            # test pod so traffic is load-balanced across every replica the
            # same way real clients see it; there is no host-side fallback
            # ("local endpoint") execution path anymore. Reaching this means
            # the deployment's namespace/cluster could not be resolved.
            raise SimulationConfigurationError(
                "Deployment-bound simulation tasks require an in-cluster test pod, but the "
                "target deployment's namespace/cluster could not be resolved."
            )
        detected_version: str | None = None
        if incluster_target is not None and self.pip_installable_in_pod:
            # The tool is installed on demand inside the pod; the host
            # never needs it, so skip the host-side install/probe and just
            # use the bare command name for logging and pod execution.
            executable = self.pip_installable_in_pod
        else:
            executable = await self.prepare_executable(context)
            availability = executable_availability(executable)
            if not availability.available:
                raise SimulationConfigurationError(availability.unavailable_reason or "Backend unavailable")
            detected_version = availability.version
        command = await self.command(task)
        trace = task.prompt.trace
        if trace.end_seconds is not None:
            input_path = None
            for flag in ("--input-file", "--dataset-path"):
                if flag in command.args:
                    input_path = command.args[command.args.index(flag) + 1]
                    break
            context.log(
                "Replay range: "
                f"{trace.start_seconds:.3f}s-{trace.end_seconds:.3f}s source time; "
                f"scale={trace.synthesis_speedup_ratio:g}x; "
                f"duration={task.simulation.duration_seconds}s" + (f"; input={input_path}" if input_path else "")
            )
        context.log(f"Command: {command_for_log(executable, command.args)}")
        expected_requests = await asyncio.to_thread(self.expected_request_count, task)
        context.progress(0, "Starting simulation backend")
        artifact_dir = Path(task.task_dir) / "artifacts" / self.name
        if incluster_target is not None:
            namespace, cluster_id = incluster_target
            result = await execute_command_in_pod(
                namespace=namespace,
                cluster_id=cluster_id,
                task_dir=Path(task.task_dir),
                executable=executable,
                args=command.args,
                artifact_dir=artifact_dir,
                timeout_seconds=command.timeout_seconds,
                context=context,
                env=dict(command.env) or None,
                progress_request_counts=lambda: (self.completed_request_count(task), expected_requests),
                tolerate_timeout=self.tolerate_timeout,
            )
        else:
            result = await execute_command(
                executable,
                command.args,
                artifact_dir,
                command.timeout_seconds,
                context,
                progress_request_counts=lambda: (self.completed_request_count(task), expected_requests),
                tolerate_timeout=self.tolerate_timeout,
                env=dict(command.env) or None,
            )
        context.progress(99, "Processing simulation results")
        parsed = await self.parse(task, result, self.backend_version(detected_version))
        context.progress(99, "Finalizing simulation results")
        return parsed

    @staticmethod
    def command_artifacts(result: CommandResult) -> list[SimulationArtifact]:
        return [
            artifact("stdout", result.stdout_path, "text/plain"),
            artifact("stderr", result.stderr_path, "text/plain"),
        ]
