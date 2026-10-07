"""AIPerf simulation backend."""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import sys
from functools import singledispatchmethod
from pathlib import Path
from typing import ClassVar

from llm_d_bench.utils.paths import storage_path
from llm_d_bench.utils.shell import run_sync, which

from ..errors import SimulationConfigurationError, SimulationError, SimulationResultParseError
from ..models import (
    ArtifactRecord,
    BackendCommand,
    BackendDescriptor,
    SimulationResult,
    SimulationTask,
)
from ..process import CommandResult, RunContext
from ..traces import BasetenTrace, BaseTrace, WekaPublicDatasetTrace, trace_registry
from .analytics import (
    completion_timeline as _completion_timeline,
)
from .analytics import (
    latency_timeline as _latency_timeline,
)
from .analytics import (
    number as _number,
)
from .analytics import (
    rate_timelines as _rate_timelines,
)
from .analytics import (
    read_object as _read_object,
)
from .analytics import (
    visit_json_lines as _visit_json_lines,
)
from .analytics import artifact_error_message
from .base import CommandBackend
from .base import artifact as _artifact
from .registry import register_backend


def _average(raw: dict, name: str) -> float:
    value = raw.get(name)
    if isinstance(value, dict):
        value = value.get("avg", value.get("mean"))
    return _number(value)


def _metric(raw: dict, name: str) -> dict | None:
    value = raw.get(name)
    if not isinstance(value, dict):
        return None
    fields = {
        "mean_ms": value.get("avg", value.get("mean")),
        "p50_ms": value.get("p50"),
        "p90_ms": value.get("p90"),
        "p95_ms": value.get("p95"),
        "p99_ms": value.get("p99"),
        "min_ms": value.get("min"),
        "max_ms": value.get("max"),
        "std_ms": value.get("std"),
        "count": value.get("count"),
        "sum_ms": value.get("sum"),
    }
    metric = {
        key: parsed for key, raw_value in fields.items() if math.isfinite(parsed := _number(raw_value, float("nan")))
    }
    if not metric:
        return None
    if value.get("unit"):
        metric["unit"] = value["unit"]
    return metric


def _aiperf_profiling_record(record: dict) -> bool:
    metadata = record.get("metadata")
    return not isinstance(metadata, dict) or metadata.get("benchmark_phase") in (None, "profiling")


def _response_status_code(record: dict) -> int | None:
    for container in (record, record.get("metadata"), record.get("trace_data"), record.get("error")):
        if not isinstance(container, dict):
            continue
        for key in ("status", "status_code", "response_status_code", "code"):
            value = _number(container.get(key), float("nan"))
            if math.isfinite(value) and value.is_integer() and 100 <= value <= 599:
                return int(value)
    return None


def _error_message(record: dict) -> str | None:
    return artifact_error_message(record, record.get("error"))


@register_backend
class AIPerfBackend(CommandBackend):
    name = "aiperf"
    display_name = "AIPerf"
    VERSION = "0.12.0"
    # aiperf is a pure Python PyPI package. In-cluster runs use the official
    # nvcr.io/nvidia/ai-dynamo/aiperf image (see
    # llm_d_bench.simulation.incluster.incluster_image), which already has it
    # pre-installed; ``pip install`` only kicks in as a fallback if that
    # image is overridden to a bare Python one.
    pip_installable_in_pod = "aiperf"
    _install_lock = asyncio.Lock()
    trace_formats: ClassVar[list[str]] = [
        "mooncake_trace",
        "bailian_trace",
        "baseten_trace",
        "burst_gpt_trace",
        "weka_public_dataset",
    ]
    capabilities: ClassVar[dict] = {
        "prompt_kinds": ["trace"],
        "arrival_patterns": [],
        "supports_duration": True,
        "supports_request_count": False,
        "supports_request_rate": False,
        "supports_streaming": True,
        "supports_per_request_results": True,
        "supports_cancellation": True,
        "supports_trace_range": True,
        "scale_factor_trace_formats": ["baseten_trace"],
        "trace_format_capabilities": {
            "mooncake_trace": {
                "supports_trace_range": True,
                "supports_scale_factor": False,
            },
            "bailian_trace": {
                "supports_trace_range": True,
                "supports_scale_factor": False,
            },
            "baseten_trace": {
                "supports_trace_range": True,
                "supports_scale_factor": True,
            },
            "burst_gpt_trace": {
                "supports_trace_range": True,
                "supports_scale_factor": False,
            },
            "weka_public_dataset": {
                "supports_trace_range": False,
                "supports_scale_factor": False,
            },
        },
        "advanced_options": [
            {"name": "random_seed", "label": "Random seed", "minimum": 0},
            {
                "name": "num_profile_runs",
                "label": "Profile runs",
                "minimum": 1,
                "maximum": 10,
                "default": 1,
            },
            {
                "name": "profile_run_cooldown_seconds",
                "label": "Run cooldown (seconds)",
                "minimum": 0,
            },
            {
                "name": "concurrency",
                "label": "Session concurrency",
                "minimum": 1,
                "default": 1,
                "trace_formats": ["weka_public_dataset"],
            },
            {
                "name": "max_context_length",
                "label": "Maximum context length",
                "minimum": 1,
                "trace_formats": ["weka_public_dataset"],
            },
            {
                "name": "trace_idle_gap_cap_seconds",
                "label": "Trace idle-gap cap (seconds)",
                "minimum": 0,
                "trace_formats": ["weka_public_dataset"],
            },
        ],
    }
    allowed_options: ClassVar[set[str]] = {
        "ui_type",
        "random_seed",
        "num_profile_runs",
        "profile_run_cooldown_seconds",
        "concurrency",
        "max_context_length",
        "trace_idle_gap_cap_seconds",
        "error_rate_slo",
        "ttft_slo",
        "tpot_slo",
    }

    @property
    def executable(self) -> str:
        configured = os.environ.get("AIPERF_EXECUTABLE")
        if configured:
            return str(Path(configured).expanduser())
        return which("aiperf") or str(self.managed_executable_path())

    @staticmethod
    def managed_executable_path() -> Path:
        # Do not resolve symlinks: in a venv ``sys.executable`` is often a
        # symlink chain to the base interpreter (e.g. .venv/bin/python ->
        # /usr/bin/python3). pip installs console scripts into
        # ``<venv>/bin``, so resolving would point at the wrong directory.
        return Path(sys.executable).parent / "aiperf"

    def descriptor(self) -> BackendDescriptor:
        executable = self.executable
        available = bool(Path(executable).is_file() or which(executable))
        configured = bool(os.environ.get("AIPERF_EXECUTABLE"))
        return BackendDescriptor(
            name=self.name,
            display_name=self.display_name,
            api_version=1,
            available=available or not configured,
            version=self.backend_version(None),
            unavailable_reason=(
                f"Configured AIPerf executable '{executable}' was not found" if configured and not available else None
            ),
            managed_installation=not available and not configured,
            scenarios=self.scenarios,
            capabilities={**self.capabilities, "trace_formats": self.trace_formats},
        )

    def backend_version(self, detected_version: str | None) -> str:
        return detected_version or self.VERSION

    @classmethod
    def _install(cls, destination: Path) -> None:
        cache = storage_path("cache", "backends")
        lock_root = cache / cls.name
        lock_root.mkdir(parents=True, exist_ok=True)
        with lock_root.joinpath(".install.lock").open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if destination.is_file():
                return
            try:
                result = run_sync(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        f"aiperf=={cls.VERSION}",
                    ],
                    timeout=1800,
                )
            except TimeoutError as error:
                raise SimulationConfigurationError("AIPerf installation timed out after 30 minutes") from error
            if result.returncode != 0 or not destination.is_file():
                detail = (result.stderr or result.stdout or "pip install failed").strip()
                raise SimulationConfigurationError(f"Unable to install AIPerf: {detail[-2000:]}")

    async def prepare_executable(self, context: RunContext) -> str:
        executable = self.executable
        if Path(executable).is_file() or which(executable):
            return executable
        if os.environ.get("AIPERF_EXECUTABLE"):
            raise SimulationConfigurationError(f"Configured AIPerf executable '{executable}' was not found")
        async with self._install_lock:
            destination = self.managed_executable_path()
            if not destination.is_file():
                context.log(
                    f"AIPerf was not found; installing version {self.VERSION} "
                    f"into the active Python environment at {Path(sys.executable).parent}"
                )
                await asyncio.to_thread(self._install, destination)
                context.log(f"AIPerf installation completed: {destination}")
            return str(destination)

    async def validate_backend(self, task: SimulationTask) -> None:
        trace = task.prompt.trace
        runtime_trace = trace_registry.resolve(trace.path, trace.format)
        options = task.simulation.backend_options
        unknown = sorted(set(options) - self.allowed_options)
        if unknown:
            raise SimulationConfigurationError(f"Unsupported backend options: {', '.join(unknown)}")
        integer_ranges = {
            "random_seed": (0, None),
            "num_profile_runs": (1, 10),
            "concurrency": (1, None),
            "max_context_length": (1, None),
        }
        for option, (minimum, maximum) in integer_ranges.items():
            if option not in options:
                continue
            value = options[option]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < minimum
                or (maximum is not None and value > maximum)
            ):
                maximum_text = f" and at most {maximum}" if maximum is not None else ""
                raise SimulationConfigurationError(
                    f"AIPerf {option} must be an integer of at least {minimum}{maximum_text}"
                )
        for option in ("profile_run_cooldown_seconds", "trace_idle_gap_cap_seconds"):
            if option in options and (
                not isinstance(options[option], (int, float))
                or isinstance(options[option], bool)
                or not math.isfinite(float(options[option]))
                or float(options[option]) < 0
            ):
                raise SimulationConfigurationError(f"AIPerf {option} must be a non-negative number")
        if float(options.get("profile_run_cooldown_seconds", 0)) > 0 and int(options.get("num_profile_runs", 1)) <= 1:
            raise SimulationConfigurationError(
                "AIPerf profile_run_cooldown_seconds requires num_profile_runs greater than 1"
            )
        if task.simulation.duration_seconds < 1:
            raise SimulationConfigurationError("AIPerf Simulation requires duration_seconds of at least 1")
        if trace.format not in self.trace_formats:
            raise SimulationConfigurationError(f"Unsupported AIPerf trace format '{trace.format}'")
        if trace.timeout_seconds <= 0:
            raise SimulationConfigurationError("AIPerf trace_timeout_seconds must be greater than zero")
        if trace.synthesis_speedup_ratio <= 0:
            raise SimulationConfigurationError("AIPerf synthesis_speedup_ratio must be greater than zero")
        self._validate_trace(runtime_trace, trace, options)

    @singledispatchmethod
    def _validate_trace(self, runtime_trace: BaseTrace, trace, options: dict) -> None:
        raise SimulationConfigurationError(f"AIPerf does not support {runtime_trace.format}")

    @_validate_trace.register
    def _(self, runtime_trace: BaseTrace, trace, options: dict) -> None:
        unsupported = {"concurrency", "max_context_length", "trace_idle_gap_cap_seconds"}.intersection(options)
        if unsupported:
            raise SimulationConfigurationError(
                f"AIPerf options are not supported by {trace.format}: {', '.join(sorted(unsupported))}"
            )
        if not trace.fixed_schedule:
            raise SimulationConfigurationError("AIPerf Simulation requires fixed-schedule trace replay")
        if trace.synthesis_speedup_ratio != 1:
            raise SimulationConfigurationError(
                f"AIPerf {trace.format} does not support synthesis_speedup_ratio values other than 1.0"
            )

    @_validate_trace.register
    def _(self, runtime_trace: BasetenTrace, trace, options: dict) -> None:
        unsupported = {"concurrency", "max_context_length", "trace_idle_gap_cap_seconds"}.intersection(options)
        if unsupported:
            raise SimulationConfigurationError(
                f"AIPerf options are not supported by {trace.format}: {', '.join(sorted(unsupported))}"
            )
        if not trace.fixed_schedule:
            raise SimulationConfigurationError("AIPerf Simulation requires fixed-schedule trace replay")

    @_validate_trace.register
    def _(self, runtime_trace: WekaPublicDatasetTrace, trace, options: dict) -> None:
        if trace.synthesis_speedup_ratio != 1:
            raise SimulationConfigurationError(
                f"AIPerf {trace.format} does not support synthesis_speedup_ratio values other than 1.0"
            )

    def _file_trace_args(
        self,
        runtime_trace: BaseTrace,
        trace,
        *,
        scale_flag: str,
        relative_offsets: bool,
    ) -> list[str]:
        args = [
            "--input-file",
            str(runtime_trace.path),
            "--custom-dataset-type",
            runtime_trace.format,
            "--fixed-schedule",
            scale_flag,
            str(trace.synthesis_speedup_ratio),
        ]
        if trace.end_seconds is not None:
            index = runtime_trace.build_timeline_index()
            offset_origin = 0.0 if relative_offsets else float(index["minimum_timestamp"])
            args.extend(
                [
                    "--fixed-schedule-start-offset",
                    str(round((offset_origin + trace.start_seconds) * 1000)),
                    "--fixed-schedule-end-offset",
                    str(round((offset_origin + trace.end_seconds) * 1000)),
                ]
            )
        return args

    @singledispatchmethod
    def _trace_args(self, runtime_trace: BaseTrace, trace, options: dict) -> tuple[str, list[str]]:
        raise SimulationConfigurationError(f"AIPerf does not support {runtime_trace.format}")

    @_trace_args.register
    def _(self, runtime_trace: BaseTrace, trace, options: dict) -> tuple[str, list[str]]:
        return "chat", self._file_trace_args(
            runtime_trace,
            trace,
            scale_flag="--synthesis-speedup-ratio",
            relative_offsets=False,
        )

    @_trace_args.register
    def _(self, runtime_trace: BasetenTrace, trace, options: dict) -> tuple[str, list[str]]:
        return "completions", self._file_trace_args(
            runtime_trace,
            trace,
            scale_flag="--replay-speedup",
            relative_offsets=True,
        )

    @_trace_args.register
    def _(self, runtime_trace: WekaPublicDatasetTrace, trace, options: dict) -> tuple[str, list[str]]:
        args = [
            "--public-dataset",
            runtime_trace.dataset,
            "--concurrency",
            str(options.get("concurrency", 1)),
        ]
        flags = {
            "max_context_length": "--max-context-length",
            "trace_idle_gap_cap_seconds": "--trace-idle-gap-cap-seconds",
        }
        for option, flag in flags.items():
            if option in options:
                args.extend([flag, str(options[option])])
        return "chat", args

    async def command(self, task: SimulationTask) -> BackendCommand:
        trace = task.prompt.trace
        runtime_trace = trace_registry.resolve(trace.path, trace.format)
        options = task.simulation.backend_options
        endpoint_type, trace_args = await asyncio.to_thread(self._trace_args, runtime_trace, trace, options)
        args = [
            "profile",
            "--model",
            task.model_name,
            "--url",
            task.endpoint_url,
            "--endpoint-type",
            endpoint_type,
            "--artifact-dir",
            str(Path(task.task_dir) / "artifacts" / self.name),
            "--ui-type",
            str(options.get("ui_type", "none")),
        ]
        if task.simulation.stream:
            args.append("--streaming")
        args.extend(trace_args)
        args.extend(
            [
                "--benchmark-duration",
                str(task.simulation.duration_seconds),
                "--benchmark-grace-period",
                str(task.simulation.grace_period_seconds),
            ]
        )
        tokenizer = task.prompt.dataset.tokenizer
        if tokenizer:
            args.extend(["--tokenizer", tokenizer])
        if "random_seed" in options:
            args.extend(["--random-seed", str(options["random_seed"])])
        if "num_profile_runs" in options:
            args.extend(["--num-profile-runs", str(options["num_profile_runs"])])
        if "profile_run_cooldown_seconds" in options and int(options.get("num_profile_runs", 1)) > 1:
            args.extend(
                [
                    "--profile-run-cooldown-seconds",
                    str(options["profile_run_cooldown_seconds"]),
                ]
            )
        if task.api_key:
            # Shared-Gateway deployments require a model access token; AIPerf
            # sends it as ``Authorization: Bearer <api-key>``.
            args.extend(["--api-key", task.api_key])
        return BackendCommand(
            args=tuple(args),
            timeout_seconds=math.ceil(trace.timeout_seconds + task.simulation.grace_period_seconds + 180),
        )

    def artifact_records(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> list[ArtifactRecord]:
        root = Path(task.task_dir) / "artifacts" / self.name
        path = next((candidate for candidate in root.rglob("profile_export.jsonl") if candidate.is_file()), None)
        if path is None:
            return []
        records: list[ArtifactRecord] = []

        def visit(record: dict) -> None:
            if not _aiperf_profiling_record(record):
                return
            metadata = record.get("metadata")
            metrics = record.get("metrics")
            metadata = metadata if isinstance(metadata, dict) else {}
            metrics = metrics if isinstance(metrics, dict) else {}

            def metric(name: str) -> float | None:
                raw = metrics.get(name)
                value = _number(raw.get("value") if isinstance(raw, dict) else raw, float("nan"))
                return value if math.isfinite(value) else None

            output_tokens = metric("output_sequence_length")
            mismatch_percent = metric("osl_mismatch_diff_pct")
            requested_output_tokens = output_tokens
            if output_tokens is not None and mismatch_percent is not None:
                ratio = 1 + mismatch_percent / 100
                if ratio > 0:
                    requested_output_tokens = round(output_tokens / ratio)
            started = _number(metadata.get("request_start_ns"), float("nan")) / 1_000_000_000
            completed = _number(metadata.get("request_end_ns"), float("nan")) / 1_000_000_000
            arrived_value = started if math.isfinite(started) else None
            completed_value = completed if math.isfinite(completed) else None
            status_code = _response_status_code(record)
            error = bool(record.get("error") or record.get("timeout"))
            error_message = _error_message(record)
            if error_message is None and record.get("timeout"):
                error_message = "Request timed out"
            records.append(
                ArtifactRecord(
                    arrived=arrived_value,
                    completed=completed_value,
                    rate_completed=completed_value if completed_value is not None else arrived_value,
                    successful=not error,
                    issue_failed=error or (status_code is not None and not 200 <= status_code < 300),
                    status_code=status_code,
                    error=error_message,
                    request_id=(
                        record.get("request_id") or metadata.get("request_id") or record.get("id") or metadata.get("id")
                    ),
                    input_tokens=metric("input_sequence_length"),
                    output_tokens=output_tokens,
                    requested_output_tokens=requested_output_tokens,
                    latency_ms=metric("request_latency"),
                    ttft_ms=metric("time_to_first_token"),
                    tpot_ms=metric("inter_token_latency"),
                )
            )

        _visit_json_lines(path, "Invalid AIPerf record", visit, tolerate_incomplete=tolerate_incomplete)
        return records

    def artifact_time_origin(self, records: list[ArtifactRecord]) -> float:
        return min((record.arrived for record in records if record.arrived is not None), default=0)

    async def parse(
        self,
        task: SimulationTask,
        command: CommandResult,
        version: str | None,
    ) -> SimulationResult:
        root = Path(task.task_dir) / "artifacts" / self.name
        files = sorted(path for path in root.rglob("*") if path.is_file())

        def named(filename: str) -> Path | None:
            return next((path for path in files if path.name == filename), None)

        summary_path = named("profile_export_aiperf.json")
        if summary_path is None:
            raise SimulationResultParseError("AIPerf did not produce profile_export_aiperf.json")
        raw = _read_object(summary_path, "Invalid AIPerf summary")
        requests_path = named("profile_export.jsonl")
        successful = failed = 0
        total_input = total_output = 0.0
        completion_seconds: list[float] = []
        successful_completion_seconds: list[float] = []
        failed_completion_seconds: list[float] = []
        client_timeout_completion_seconds: list[float] = []
        backend_error_completion_seconds: list[float] = []
        request_start_seconds: list[float] = []
        latency_records: list[tuple[float, float]] = []
        ttft_records: list[tuple[float, float]] = []
        tpot_records: list[tuple[float, float]] = []
        rate_records: list[tuple[float, float, float, bool]] = []

        def visit(record: dict) -> None:
            nonlocal successful, failed, total_input, total_output
            if not _aiperf_profiling_record(record):
                return
            metadata = record.get("metadata")
            started = completed = float("nan")
            if isinstance(metadata, dict):
                metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
                started = _number(metadata.get("request_start_ns"), float("nan"))
                completed = _number(metadata.get("request_end_ns"), float("nan"))
                if math.isfinite(started):
                    request_start_seconds.append(started / 1_000_000_000)
                if math.isfinite(completed):
                    completion_seconds.append(completed / 1_000_000_000)
                request_latency = metrics.get("request_latency")
                latency_ms = _number(
                    (request_latency.get("value") if isinstance(request_latency, dict) else request_latency),
                    float("nan"),
                )
                if math.isfinite(started) and math.isfinite(latency_ms):
                    arrived = started / 1_000_000_000
                    latency_records.append((arrived, latency_ms))
                    for metric_name, destination in (
                        ("time_to_first_token", ttft_records),
                        ("inter_token_latency", tpot_records),
                    ):
                        metric = metrics.get(metric_name)
                        value = _number(
                            metric.get("value") if isinstance(metric, dict) else metric,
                            float("nan"),
                        )
                        if math.isfinite(value):
                            destination.append((arrived, value))
            if record.get("error"):
                failed += 1
                timestamp = completed / 1_000_000_000 if math.isfinite(completed) else started / 1_000_000_000
                if math.isfinite(timestamp):
                    failed_completion_seconds.append(timestamp)
                    if _response_status_code(record) is None:
                        client_timeout_completion_seconds.append(timestamp)
                    else:
                        backend_error_completion_seconds.append(timestamp)
                    rate_records.append((started / 1_000_000_000, timestamp, 0, True))
                return
            successful += 1
            metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
            input_metric = metrics.get("input_sequence_length")
            output_metric = metrics.get("output_sequence_length")
            total_input += _number(input_metric.get("value") if isinstance(input_metric, dict) else None)
            total_output += _number(output_metric.get("value") if isinstance(output_metric, dict) else None)
            timestamp = completed / 1_000_000_000 if math.isfinite(completed) else started / 1_000_000_000
            if math.isfinite(timestamp):
                successful_completion_seconds.append(timestamp)
                rate_records.append(
                    (
                        started / 1_000_000_000,
                        timestamp,
                        _number(output_metric.get("value") if isinstance(output_metric, dict) else output_metric),
                        False,
                    )
                )

        if requests_path:
            _visit_json_lines(requests_path, "Invalid AIPerf record", visit)
        total = successful + failed
        if raw.get("request_count") is not None:
            successful = _average(raw, "request_count")
        if raw.get("error_request_count") is not None:
            failed = _average(raw, "error_request_count")
        if raw.get("request_count") is not None or raw.get("error_request_count") is not None:
            total = successful + failed
        if raw.get("total_isl") is not None:
            total_input = _average(raw, "total_isl")
        if raw.get("total_osl") is not None:
            total_output = _average(raw, "total_osl")
        elif raw.get("total_output_tokens") is not None:
            total_output = _average(raw, "total_output_tokens")
        latency = _metric(raw, "request_latency")
        ttft = _metric(raw, "time_to_first_token")
        tpot = _metric(raw, "inter_token_latency")
        warnings: list[str] = []
        missing_streaming_metrics = [name for name, value in (("TTFT", ttft), ("TPOT", tpot)) if value is None]
        if missing_streaming_metrics:
            unavailable = " and ".join(missing_streaming_metrics)
            if task.simulation.stream:
                warnings.append(f"AIPerf did not export {unavailable} metrics for this task.")
            else:
                warnings.append(
                    f"{unavailable} metrics are unavailable because this task used non-streaming "
                    "responses. Enable Stream responses to collect them."
                )
        # AIPerf ends its own benchmark cleanly after the grace period, cancelling
        # any still in-flight requests, so `command.timed_out` is rarely set. Detect
        # the same condition from the summary: fewer completed requests than the
        # trace schedule expected means the grace period cut requests short.
        try:
            expected = self.expected_request_count(task)
        except SimulationError:
            expected = None
        if command.timed_out or (expected is not None and total < expected):
            warnings.append(
                "Simulation reached the grace-period limit while requests were still in-flight; "
                "results reflect only the requests that completed before the limit."
            )
        drift_values = {
            percentile: _average(raw, f"replay_sched_lag_{percentile}")
            for percentile in ("p50", "p90", "p99")
            if raw.get(f"replay_sched_lag_{percentile}") is not None
        }
        drift = {f"{percentile}_ms": value for percentile, value in drift_values.items()} if drift_values else None
        artifacts = self.command_artifacts(command)
        for path, kind, media in [
            (summary_path, "summary", "application/json"),
            (requests_path, "per_request", "application/x-ndjson"),
            (named("profile_export_aiperf.csv"), "summary_csv", "text/csv"),
            (named("inputs.json"), "inputs", "application/json"),
            (named("aiperf.log"), "tool_log", "text/plain"),
        ]:
            if path:
                artifacts.append(_artifact(kind, path, media))
        wall_time = (command.completed_at - command.started_at).total_seconds()
        origin_seconds = min(request_start_seconds) if request_start_seconds else 0
        throughput_timeline, error_timeline = _rate_timelines(
            rate_records,
            origin_seconds=origin_seconds,
        )
        latency_heatmaps = self.latency_timelines_from_artifacts(task)
        return SimulationResult(
            run_id=task.id,
            backend=self.name,
            backend_version=version,
            summary={
                "total_requests": total,
                "successful_requests": successful,
                "failed_requests": failed,
                "success_rate": successful / total * 100 if total else 0,
                "duration_seconds": _average(raw, "benchmark_duration") or wall_time,
                "throughput_rps": _average(raw, "request_throughput"),
                "throughput_tps": _average(raw, "output_token_throughput"),
                "input_throughput_tps": _average(raw, "input_token_throughput"),
                "total_throughput_tps": _average(raw, "total_token_throughput"),
                "request_error_rate": _average(raw, "request_error_rate"),
                "effective_concurrency": _average(raw, "effective_concurrency"),
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "completion_timeline": _completion_timeline(
                    completion_seconds,
                    arrival_seconds=(
                        [origin_seconds + value for value in schedule]
                        if (schedule := self.arrival_schedule_seconds(task))
                        else request_start_seconds
                    ),
                    successful_completion_seconds=successful_completion_seconds,
                    failed_completion_seconds=failed_completion_seconds,
                    client_timeout_completion_seconds=client_timeout_completion_seconds,
                    backend_error_completion_seconds=backend_error_completion_seconds,
                    origin_seconds=origin_seconds,
                ),
                "latency_timeline": _latency_timeline(
                    latency_records,
                    arrival_seconds=request_start_seconds,
                    origin_seconds=(min(request_start_seconds) if request_start_seconds else 0),
                    end_seconds=(max(completion_seconds) if completion_seconds else None),
                ),
                "ttft_timeline": _latency_timeline(
                    ttft_records,
                    arrival_seconds=request_start_seconds,
                    origin_seconds=(min(request_start_seconds) if request_start_seconds else 0),
                    end_seconds=(max(completion_seconds) if completion_seconds else None),
                ),
                "tpot_timeline": _latency_timeline(
                    tpot_records,
                    arrival_seconds=request_start_seconds,
                    origin_seconds=(min(request_start_seconds) if request_start_seconds else 0),
                    end_seconds=(max(completion_seconds) if completion_seconds else None),
                ),
                "ttft_heatmap": latency_heatmaps["ttft_heatmap"],
                "tpot_heatmap": latency_heatmaps["tpot_heatmap"],
                "throughput_timeline": throughput_timeline,
                "goodput_timeline": self.goodput_timeline_from_artifacts(task),
                "error_timeline": error_timeline,
                "status_code_breakdown": self.status_code_breakdown_from_artifacts(task),
                "latency": latency,
                "ttft": ttft,
                "tpot": tpot,
                "drift": drift,
            },
            artifacts=artifacts,
            per_request=[],
            backend_metrics=raw,
            warnings=warnings,
        )
