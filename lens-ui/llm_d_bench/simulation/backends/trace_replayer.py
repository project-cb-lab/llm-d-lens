"""Trace-Replayer simulation backend."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from contextlib import suppress
from datetime import datetime
from functools import singledispatchmethod
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from llm_d_bench.utils.paths import storage_path
from llm_d_bench.utils.shell import run_sync, which

from ..errors import SimulationConfigurationError
from ..models import (
    ArtifactRecord,
    BackendCommand,
    BackendDescriptor,
    SimulationResult,
    SimulationTask,
)
from ..process import CommandResult, RunContext
from ..traces import BailianTrace, BaseTrace, MooncakeTrace, trace_registry
from .analytics import (
    completion_timeline as _completion_timeline,
)
from .analytics import (
    distribution as _distribution,
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

_REQUEST_ERROR_PATTERN = re.compile(
    r"Request#(?P<request_id>\d+)::\((?P<input_tokens>\d+)\|(?P<output_tokens>\d+)\) "
    r"(?P<kind>stream )?error: (?P<error>.+)"
)

_LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _process_start_epoch(task: SimulationTask) -> float:
    """Wall-clock epoch (seconds) of the client's request loop (its BASETIME).

    ``requests.jsonl`` records ``s_time``/``e_time`` as milliseconds since a
    process-relative ``Instant`` (see the Rust ``requester.rs`` BASETIME), not
    Unix epoch. The first timestamped log line ("Warmup start...") is emitted
    immediately before the request loop spawns and initializes that instant, so
    adding it converts process-relative records into epoch seconds. Without it
    the live flow-map window (anchored to wall-clock) slides past the frozen
    process-relative timeline and every rate reads 0. Returns ``0.0`` when the
    log is unavailable, which leaves records process-relative.
    """
    path = Path(task.task_dir) / "artifacts" / "trace-replayer" / "stdout.log"
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                # tracing writes ANSI color codes around the timestamp; strip
                # them so the leading ISO-8601 timestamp matches cleanly.
                match = _LOG_TIMESTAMP_RE.match(_ANSI_ESCAPE_RE.sub("", line))
                if match is None:
                    continue
                try:
                    return datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    return 0.0
    except OSError:
        pass
    return 0.0


def _trace_tpot(record: dict) -> float:
    value = _number(record.get("avg_time_between_tokens"), float("nan"))
    if math.isfinite(value):
        return value
    total_time = _number(record.get("total_time"), float("nan"))
    output_length = _number(record.get("output_length"), float("nan"))
    return total_time / output_length if math.isfinite(total_time) and output_length > 0 else float("nan")


def _response_status_code(record: dict) -> int | None:
    value = _number(record.get("status"), float("nan"))
    return int(value) if math.isfinite(value) and value.is_integer() and 100 <= value <= 599 else None


def _error_message(record: dict) -> str | None:
    return artifact_error_message(record, record.get("error") or record.get("timeout"))


def _transport_error_records(path: Path) -> list[ArtifactRecord]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _REQUEST_ERROR_PATTERN.search(line)
        if match is None:
            continue
        kind = "Stream error" if match.group("kind") else "Transport error"
        records.append(
            ArtifactRecord(
                arrived=None,
                completed=None,
                successful=False,
                issue_failed=True,
                error=f"{kind}: {match.group('error')[:1000]}",
                request_id=int(match.group("request_id")),
                input_tokens=float(match.group("input_tokens")),
                output_tokens=float(match.group("output_tokens")),
            )
        )
    return records


@register_backend
class TraceReplayerBackend(CommandBackend):
    name = "trace-replayer"
    display_name = "Trace-Replayer"
    VERSION = "0.2.0"
    REPOSITORY = "https://github.com/blitz-serving/trace-replayer.git"
    REVISION = "84c9193ac67ebbf0e08db161bc9b048d597c2f80"
    DEFAULT_NUM_PRODUCERS = 16
    DEFAULT_CHANNEL_CAPACITY = 32
    DEFAULT_THREADS = 32
    # Per-request time budget passed to the client. Trace-Replayer derives a
    # hard per-request timeout from TTFT/TPOT SLOs (max(15s, ttft_slo + tpot_slo
    # * output_length)) and aborts requests that exceed it. The dashboard treats
    # SLOs as post-run goodput thresholds only, so we give the client a
    # permissive budget (24h) that is never hit in practice; the process is
    # still bounded by the backend command timeout. Goodput is computed from the
    # user-provided SLOs independently (see parse()).
    REQUEST_TIMEOUT_SECONDS = 24 * 60 * 60
    # The client drains in-flight requests indefinitely after the send window
    # (it has no grace-period flag). Bound the run to the configured duration +
    # grace period and treat a slow tail as a partial completion (like AIPerf)
    # instead of failing the task.
    tolerate_timeout: ClassVar[bool] = True
    # Fixed overhead beyond the duration + grace window for client teardown and
    # artifact flushing before the watchdog reports the partial result.
    TIMEOUT_OVERHEAD_SECONDS = 60
    DATASET_NAMES: ClassVar[dict[type[BaseTrace], str]] = {
        MooncakeTrace: "mooncake",
        BailianTrace: "bailian",
    }
    _install_lock = asyncio.Lock()
    _tokenizer_locks: ClassVar[dict[str, asyncio.Lock]] = {}
    trace_formats: ClassVar[list[str]] = ["mooncake_trace", "bailian_trace"]
    allowed_options: ClassVar[set[str]] = {
        "tokenizer_config",
        "num_producer",
        "channel_capacity",
        "threads",
        "ttft_slo",
        "tpot_slo",
        "early_stop_error_threshold",
        "metric_percentiles",
        "error_rate_slo",
    }

    @property
    def executable(self) -> str:
        configured = os.environ.get("TRACE_REPLAYER_EXECUTABLE")
        if configured:
            return str(Path(configured).expanduser())
        return which("trace-replayer") or str(self.managed_executable_path())

    @classmethod
    def managed_executable_path(cls) -> Path:
        cache = storage_path("cache", "backends")
        return cache / cls.name / cls.REVISION / "bin" / cls.name

    def descriptor(self) -> BackendDescriptor:
        executable = self.executable
        available = bool(Path(executable).is_file() or which(executable))
        configured = bool(os.environ.get("TRACE_REPLAYER_EXECUTABLE"))
        missing = [tool for tool in ("cargo", "git") if which(tool) is None]
        return BackendDescriptor(
            name=self.name,
            display_name=self.display_name,
            api_version=1,
            available=available or (not configured and not missing),
            version=self.backend_version(None),
            unavailable_reason=(
                f"Configured Trace-Replayer executable '{executable}' was not found"
                if configured and not available
                else (
                    "Trace-Replayer is not installed and automatic installation requires: " + ", ".join(missing)
                    if not available and missing
                    else None
                )
            ),
            managed_installation=not available and not configured,
            scenarios=self.scenarios,
            capabilities={**self.capabilities, "trace_formats": self.trace_formats},
        )

    def backend_version(self, detected_version: str | None) -> str:
        return detected_version or f"{self.VERSION}+{self.REVISION[:12]}"

    @classmethod
    def _install(cls, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        lock_path = destination.parents[1] / ".install.lock"
        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if destination.is_file():
                return
            cargo = which("cargo")
            git = which("git")
            if cargo is None or git is None:
                missing = [name for name, path in (("cargo", cargo), ("git", git)) if path is None]
                raise SimulationConfigurationError(
                    "Automatic Trace-Replayer installation requires: " + ", ".join(missing)
                )
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=".trace-replayer-",
                    dir=destination.parents[3],
                )
            )
            try:
                result = run_sync(
                    [
                        cargo,
                        "install",
                        "--git",
                        cls.REPOSITORY,
                        "--rev",
                        cls.REVISION,
                        "--bin",
                        "client",
                        "--root",
                        str(temporary),
                        "--locked",
                    ],
                    timeout=1800,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                installed = temporary / "bin" / "client"
                if result.returncode != 0 or not installed.is_file():
                    detail = (result.stderr or result.stdout or "cargo install failed").strip()
                    raise SimulationConfigurationError(f"Unable to install Trace-Replayer: {detail[-2000:]}")
                installed.chmod(0o755)
                os.replace(installed, destination)
            except TimeoutError as error:
                raise SimulationConfigurationError("Trace-Replayer installation timed out after 30 minutes") from error
            finally:
                shutil.rmtree(temporary, ignore_errors=True)

    async def prepare_executable(self, context: RunContext) -> str:
        executable = self.executable
        if Path(executable).is_file() or which(executable):
            return executable
        if os.environ.get("TRACE_REPLAYER_EXECUTABLE"):
            raise SimulationConfigurationError(f"Configured Trace-Replayer executable '{executable}' was not found")
        async with self._install_lock:
            destination = self.managed_executable_path()
            if not destination.is_file():
                context.log(
                    f"Trace-Replayer was not found; installing pinned revision {self.REVISION[:12]} to {destination}"
                )
                await asyncio.to_thread(self._install, destination)
                context.log(f"Trace-Replayer installation completed: {destination}")
            return str(destination)

    @property
    def capabilities(self) -> dict:
        return {
            "prompt_kinds": ["trace"],
            "arrival_patterns": [],
            "supports_duration": True,
            "supports_request_count": False,
            "supports_request_rate": False,
            "supports_streaming": True,
            "supports_per_request_results": True,
            "supports_cancellation": True,
            "supports_trace_range": True,
            "scale_factor_trace_formats": list(self.trace_formats),
            "trace_format_capabilities": {
                trace_format: {
                    "supports_trace_range": True,
                    "trace_range_mode": "duration",
                    "supports_scale_factor": True,
                }
                for trace_format in self.trace_formats
            },
            "default_tokenizer_configured": True,
            "managed_tokenizer": True,
            # Trace-Replayer always sends min_tokens == max_tokens on every request
            # (see src/apis/openai_api.rs upstream, no CLI flag to disable it). The
            # PD-disaggregation guide's router forces max_tokens=1 on the prefill
            # sub-request without clearing min_tokens, so every request is rejected
            # with "min_tokens must be less than or equal to max_tokens=1". Verified
            # against a live pd-disaggregation deployment: identical requests without
            # min_tokens succeed, and the same min_tokens request succeeds against a
            # non-PD (standard) deployment. Block this backend/guide combination.
            "incompatible_deployment_guides": ["pd-disaggregation"],
        }

    @classmethod
    def _tokenizer_cache_root(cls) -> Path:
        return storage_path("cache", "tokenizers")

    @classmethod
    def tokenizer_cache_path(cls, model_name: str) -> Path:
        readable = re.sub(r"[^A-Za-z0-9._-]+", "-", model_name).strip("-")[:80] or "model"
        digest = hashlib.sha256(model_name.encode("utf-8")).hexdigest()[:12]
        return cls._tokenizer_cache_root() / f"{readable}-{digest}"

    @classmethod
    def _validate_tokenizer_cache(cls, root: Path) -> tuple[Path, Path]:
        tokenizer = root / "tokenizer.json"
        config = root / "tokenizer_config.json"
        if not tokenizer.is_file() or not config.is_file():
            raise SimulationConfigurationError(f"Tokenizer cache is incomplete: {root}")
        try:
            for path in (tokenizer, config):
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise TypeError(f"{path.name} is not a JSON object")
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise SimulationConfigurationError(f"Tokenizer cache is invalid: {error}") from error
        return tokenizer, config

    @classmethod
    def _configured_tokenizer(cls) -> tuple[Path, Path] | None:
        value = os.environ.get("TRACE_REPLAYER_TOKENIZER")
        if not value:
            return None
        path = Path(value).expanduser().resolve()
        return cls._validate_tokenizer_cache(path if path.is_dir() else path.parent)

    @classmethod
    def _download_tokenizer(cls, model_name: str, destination: Path) -> tuple[Path, Path]:
        try:
            from huggingface_hub import hf_hub_download
        except ImportError as error:
            raise SimulationConfigurationError("Tokenizer download requires the huggingface-hub package") from error

        destination.mkdir(parents=True, exist_ok=True)
        token = os.environ.get("HF_TOKEN") or None
        try:
            for filename in ("tokenizer.json", "tokenizer_config.json"):
                hf_hub_download(
                    repo_id=model_name,
                    filename=filename,
                    local_dir=destination,
                    token=token,
                )
            return cls._validate_tokenizer_cache(destination)
        except Exception as error:
            raise SimulationConfigurationError(
                f"Unable to download the tokenizer for '{model_name}': {error}. "
                "Configure HF_TOKEN when the model repository requires authentication."
            ) from error

    @classmethod
    async def ensure_tokenizer(cls, model_name: str) -> tuple[Path, Path]:
        configured = cls._configured_tokenizer()
        if configured is not None:
            return configured
        destination = cls.tokenizer_cache_path(model_name)
        try:
            return cls._validate_tokenizer_cache(destination)
        except SimulationConfigurationError:
            pass
        lock = cls._tokenizer_locks.setdefault(model_name, asyncio.Lock())
        async with lock:
            try:
                return cls._validate_tokenizer_cache(destination)
            except SimulationConfigurationError:
                return await asyncio.to_thread(cls._download_tokenizer, model_name, destination)

    async def _tokenizer_paths(self, task: SimulationTask) -> tuple[Path, Path]:
        value = task.prompt.dataset.tokenizer
        if value:
            tokenizer = Path(value).expanduser().resolve()
            if tokenizer.is_dir():
                tokenizer = tokenizer / "tokenizer.json"
            default_config = tokenizer.parent / "tokenizer_config.json"
        else:
            tokenizer, default_config = await self.ensure_tokenizer(task.model_name)
        configured = task.simulation.backend_options.get("tokenizer_config")
        config = Path(str(configured)).expanduser().resolve() if configured else default_config
        if not tokenizer.is_file():
            raise SimulationConfigurationError(f"Trace-Replayer tokenizer.json does not exist: {tokenizer}")
        if not config.is_file():
            raise SimulationConfigurationError(f"Trace-Replayer tokenizer_config.json does not exist: {config}")
        return tokenizer, config

    @singledispatchmethod
    def _prepare_trace(self, trace: BaseTrace, destination: Path) -> Path:
        raise SimulationConfigurationError(f"Trace-Replayer does not support {trace.format}")

    @_prepare_trace.register
    def _(self, trace: BailianTrace, destination: Path) -> Path:
        return trace.path

    @_prepare_trace.register
    def _(self, trace: MooncakeTrace, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        origin: float | None = None
        try:
            with (
                trace.path.open("r", encoding="utf-8") as input_file,
                temporary.open("x", encoding="utf-8") as output_file,
            ):
                os.chmod(temporary, 0o600)
                for line_number, line in enumerate(input_file, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        timestamp = float(record["timestamp"])
                        if not isinstance(record, dict) or not math.isfinite(timestamp):
                            raise ValueError("record must contain a finite timestamp")
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                        raise SimulationConfigurationError(
                            f"Invalid Mooncake trace record at line {line_number}: {error}"
                        ) from error
                    if origin is None:
                        origin = timestamp
                    record["timestamp"] = max(0.0, timestamp - origin) / 1000
                    output_file.write(json.dumps(record, separators=(",", ":")) + "\n")
            if origin is None:
                raise SimulationConfigurationError("Mooncake trace contains no requests")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    async def _trace_path(self, task: SimulationTask) -> Path:
        trace = trace_registry.resolve(task.prompt.trace.path, task.prompt.trace.format)
        destination = Path(task.task_dir) / "artifacts" / self.name / "normalized_trace.jsonl"
        return await asyncio.to_thread(self._prepare_trace, trace, destination)

    async def validate_backend(self, task: SimulationTask) -> None:
        trace = task.prompt.trace
        options = task.simulation.backend_options
        unknown = sorted(set(options) - self.allowed_options)
        if unknown:
            raise SimulationConfigurationError(f"Unsupported backend options: {', '.join(unknown)}")
        if trace.format not in self.trace_formats:
            raise SimulationConfigurationError(f"Unsupported Trace-Replayer trace format '{trace.format}'")
        trace_registry.resolve(trace.path, trace.format)
        if not trace.fixed_schedule:
            raise SimulationConfigurationError("Trace-Replayer always replays recorded timestamps")
        if trace.synthesis_speedup_ratio <= 0:
            raise SimulationConfigurationError("Trace-Replayer scale factor must be greater than zero")
        if task.simulation.duration_seconds < 1:
            raise SimulationConfigurationError("Trace-Replayer duration_seconds must be at least 1")
        await self._tokenizer_paths(task)
        percentiles = options.get("metric_percentiles")
        if percentiles is not None and (not isinstance(percentiles, (list, tuple)) or not percentiles):
            raise SimulationConfigurationError("metric_percentiles must be a non-empty list")

    async def command(self, task: SimulationTask) -> BackendCommand:
        trace = task.prompt.trace
        options = task.simulation.backend_options
        tokenizer, config = await self._tokenizer_paths(task)
        root = Path(task.task_dir) / "artifacts" / self.name
        endpoint = task.endpoint_url
        trace_type = trace_registry.get_type(trace.format)
        try:
            dataset_name = self.DATASET_NAMES[trace_type]
        except KeyError as error:
            raise SimulationConfigurationError(f"Trace-Replayer does not support {trace.format}") from error
        execution_trace = await self._trace_path(task)
        if not urlparse(endpoint).path.rstrip("/").endswith("/v1/chat/completions"):
            endpoint = f"{endpoint}/v1/chat/completions"
        args = [
            "--tokenizer",
            str(tokenizer),
            "--tokenizer-config",
            str(config),
            "--endpoint",
            endpoint,
            "--api",
            "openai",
            "--dataset",
            dataset_name,
            "--dataset-path",
            str(execution_trace),
            "--scale-factor",
            str(trace.synthesis_speedup_ratio),
            "--time-in-secs",
            str(math.ceil(task.simulation.duration_seconds)),
            "--output-path",
            str(root / "requests.jsonl"),
            "--summary-path",
            str(root / "summary.json"),
            "--model-name",
            task.model_name,
            "--num-producer",
            str(options.get("num_producer", self.DEFAULT_NUM_PRODUCERS)),
            "--channel-capacity",
            str(options.get("channel_capacity", self.DEFAULT_CHANNEL_CAPACITY)),
            "--threads",
            str(options.get("threads", self.DEFAULT_THREADS)),
        ]
        if task.simulation.stream:
            args.append("--stream")
        # Do not forward the user TTFT/TPOT SLOs as the client's per-request
        # timeout budget. The client computes max(15s, ttft_slo + tpot_slo *
        # output_length) and aborts requests that run longer, which produces the
        # "status: timeout" records the dashboard shows as "no HTTP response
        # code". The SLOs are meant for post-run goodput reporting (see parse()),
        # so we hand the client a permissive budget instead and keep the real
        # SLOs in task.simulation.backend_options.
        args.extend(["--ttft-slo", str(self.REQUEST_TIMEOUT_SECONDS)])
        args.extend(["--tpot-slo", "0"])
        if "early_stop_error_threshold" in options:
            args.extend(["--early-stop-error-threshold", str(options["early_stop_error_threshold"])])
        if isinstance(options.get("metric_percentiles"), (list, tuple)):
            args.extend(["--metric-percentile", ",".join(map(str, options["metric_percentiles"]))])
        env = (("OPENAI_API_KEY", task.api_key),) if task.api_key else ()
        return BackendCommand(
            args=tuple(args),
            timeout_seconds=math.ceil(
                task.simulation.duration_seconds + task.simulation.grace_period_seconds + self.TIMEOUT_OVERHEAD_SECONDS
            ),
            env=env,
        )

    def artifact_records(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> list[ArtifactRecord]:
        path = Path(task.task_dir) / "artifacts" / self.name / "requests.jsonl"
        epoch = _process_start_epoch(task)
        records: list[ArtifactRecord] = []

        def visit(record: dict) -> None:
            arrived = _number(record.get("s_time"), float("nan")) / 1000 + epoch
            completed = _number(record.get("e_time"), float("nan")) / 1000 + epoch
            status_code = _response_status_code(record)
            failed = bool(record.get("error") or record.get("timeout")) or not (
                status_code is not None and 200 <= status_code < 300
            )
            tpot = _trace_tpot(record)

            def finite(value: float) -> float | None:
                return value if math.isfinite(value) else None

            records.append(
                ArtifactRecord(
                    arrived=finite(arrived),
                    completed=finite(completed),
                    latency_arrived=finite(arrived) if math.isfinite(arrived) and math.isfinite(completed) else None,
                    latency_completed=finite(completed)
                    if math.isfinite(arrived) and math.isfinite(completed)
                    else None,
                    successful=not failed,
                    issue_failed=failed,
                    status_code=status_code,
                    error=_error_message(record),
                    request_id=record.get("request_id") or record.get("id"),
                    input_tokens=finite(_number(record.get("input_length"), float("nan"))),
                    output_tokens=finite(_number(record.get("output_length"), float("nan"))),
                    requested_output_tokens=finite(_number(record.get("output_length"), float("nan"))),
                    latency_ms=(
                        max(0, completed - arrived) * 1000
                        if math.isfinite(arrived) and math.isfinite(completed)
                        else None
                    ),
                    ttft_ms=finite(_number(record.get("first_token_time"), float("nan"))),
                    tpot_ms=finite(tpot),
                )
            )

        if path.is_file():
            _visit_json_lines(
                path,
                "Invalid Trace-Replayer request output",
                visit,
                tolerate_incomplete=tolerate_incomplete,
            )
        records.extend(_transport_error_records(path.parent / "stdout.log"))
        return records

    def artifact_time_origin(self, records: list[ArtifactRecord]) -> float:
        earliest = min(
            (record.arrived for record in records if record.arrived is not None),
            default=0.0,
        )
        # When ``_process_start_epoch`` anchored the records, ``arrived`` is an
        # epoch timestamp (~1.7e9) and the origin is the client's first arrival.
        # Otherwise records stay process-relative (seconds since BASETIME) and
        # there is no epoch origin to subtract, so return 0 to signal that.
        return earliest if earliest > 1_000_000_000 else 0.0

    async def parse(
        self,
        task: SimulationTask,
        command: CommandResult,
        version: str | None,
    ) -> SimulationResult:
        root = Path(task.task_dir) / "artifacts" / self.name
        summary_path = root / "summary.json"
        requests_path = root / "requests.jsonl"
        # summary.json is only written after the client drains all in-flight
        # requests. When the grace-period watchdog stopped the client early it
        # is empty/missing, so fall back to the per-request records.
        raw: dict = {}
        if summary_path.is_file() and summary_path.stat().st_size > 0:
            raw = _read_object(summary_path, "Invalid Trace-Replayer summary")
        request_count = 0
        successful_count = 0
        total_input_tokens = 0.0
        total_output_tokens = 0.0
        drift_values: list[float] = []
        arrival_seconds: list[float] = []
        completion_seconds: list[float] = []
        successful_completion_seconds: list[float] = []
        failed_completion_seconds: list[float] = []
        client_timeout_completion_seconds: list[float] = []
        backend_error_completion_seconds: list[float] = []
        latency_records: list[tuple[float, float]] = []
        ttft_records: list[tuple[float, float]] = []
        tpot_records: list[tuple[float, float]] = []
        rate_records: list[tuple[float, float, float, bool]] = []

        def visit(record: dict) -> None:
            nonlocal request_count, successful_count, total_input_tokens, total_output_tokens
            request_count += 1
            total_input_tokens += _number(record.get("input_length"))
            total_output_tokens += _number(record.get("output_length"))
            drift = _number(record.get("s_time_drift"), float("nan"))
            if math.isfinite(drift):
                drift_values.append(drift)
            arrived = _number(record.get("s_time"), float("nan"))
            if math.isfinite(arrived):
                arrival_seconds.append(arrived / 1000)
            completed = _number(record.get("e_time"), float("nan"))
            if math.isfinite(completed):
                completion_seconds.append(completed / 1000)
            if math.isfinite(arrived) and math.isfinite(completed):
                arrived_seconds = arrived / 1000
                latency_records.append((arrived_seconds, max(0, completed - arrived)))
                ttft = _number(record.get("first_token_time"), float("nan"))
                tpot = _trace_tpot(record)
                if math.isfinite(ttft):
                    ttft_records.append((arrived_seconds, ttft))
                if math.isfinite(tpot):
                    tpot_records.append((arrived_seconds, tpot))
            status = int(_number(record.get("status"), 0))
            failed = bool(record.get("error") or record.get("timeout")) or not 200 <= status < 300
            if not failed:
                successful_count += 1
            if math.isfinite(completed):
                if failed:
                    failed_completion_seconds.append(completed / 1000)
                    if _response_status_code(record) is None:
                        client_timeout_completion_seconds.append(completed / 1000)
                    else:
                        backend_error_completion_seconds.append(completed / 1000)
                else:
                    successful_completion_seconds.append(completed / 1000)
                rate_records.append(
                    (
                        arrived / 1000,
                        completed / 1000,
                        _number(record.get("output_length")),
                        failed,
                    )
                )

        with suppress(FileNotFoundError):
            _visit_json_lines(requests_path, "Invalid Trace-Replayer request output", visit)
        transport_errors = _transport_error_records(command.stdout_path)
        total = _number(raw.get("requests_total"), request_count) + len(transport_errors)
        successful = _number(raw.get("requests_success"))
        if "requests_success" not in raw:
            successful = successful_count
        duration_seconds = _number(raw.get("duration_ms")) / 1000
        if "duration_ms" not in raw:
            duration_seconds = (command.completed_at - command.started_at).total_seconds()

        def latency(prefix: str, records: list[tuple[float, float]]) -> dict:
            result = {
                "mean_ms": _number(raw.get(f"{prefix}_mean_ms")),
                "p50_ms": _number(raw.get(f"{prefix}_p50_ms")),
                "p90_ms": _number(raw.get(f"{prefix}_p90_ms")),
                "p95_ms": _number(raw.get(f"{prefix}_p95_ms")),
                "p99_ms": _number(raw.get(f"{prefix}_p99_ms")),
            }
            # summary.json is only written once every in-flight request drains.
            # When the grace-period watchdog stopped the client early the
            # summary is empty, so derive the distribution from the per-request
            # records that did complete instead of reporting all zeros.
            if not any(value > 0 for value in result.values()):
                fallback = _distribution([value for _, value in records])
                if fallback:
                    result.update({key: fallback[key] for key in result})
            return result

        output_tokens_total = (
            _number(raw.get("output_tokens_total")) if "output_tokens_total" in raw else total_output_tokens
        )
        # Trace-Replayer only writes throughput fields once its summary.json is
        # finalized. When the grace-period watchdog stopped the client early the
        # summary is empty, so derive throughput from the per-request records:
        # requests and output tokens over the observed request window
        # (max end time - min start time), matching the client's own summary.
        request_window_seconds = 0.0
        if arrival_seconds and completion_seconds:
            request_window_seconds = max(completion_seconds) - min(arrival_seconds)
        throughput_rps = _number(raw.get("throughput_rps"))
        throughput_tps = _number(raw.get("throughput_tps"))
        if throughput_rps <= 0 and request_window_seconds > 0:
            throughput_rps = total / request_window_seconds
        if throughput_tps <= 0 and request_window_seconds > 0:
            throughput_tps = output_tokens_total / request_window_seconds

        artifacts = self.command_artifacts(command)
        artifacts.append(_artifact("summary", summary_path, "application/json"))
        if requests_path.exists():
            artifacts.append(_artifact("per_request", requests_path, "application/x-ndjson"))
        throughput_timeline, error_timeline = _rate_timelines(rate_records)
        latency_heatmaps = self.latency_timelines_from_artifacts(task)
        return SimulationResult(
            run_id=task.id,
            backend=self.name,
            backend_version=version,
            summary={
                "total_requests": total,
                "successful_requests": successful,
                "failed_requests": max(0, total - successful),
                "success_rate": successful / total * 100 if total else 0,
                "duration_seconds": duration_seconds,
                "throughput_rps": throughput_rps,
                "throughput_tps": throughput_tps,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": output_tokens_total,
                "completion_timeline": _completion_timeline(
                    completion_seconds,
                    arrival_seconds=self.arrival_schedule_seconds(task) or arrival_seconds,
                    successful_completion_seconds=successful_completion_seconds,
                    failed_completion_seconds=failed_completion_seconds,
                    client_timeout_completion_seconds=client_timeout_completion_seconds,
                    backend_error_completion_seconds=backend_error_completion_seconds,
                ),
                "latency_timeline": _latency_timeline(
                    latency_records,
                    arrival_seconds=arrival_seconds,
                    end_seconds=(max(completion_seconds) if completion_seconds else None),
                ),
                "ttft_timeline": _latency_timeline(
                    ttft_records,
                    arrival_seconds=arrival_seconds,
                    end_seconds=(max(completion_seconds) if completion_seconds else None),
                ),
                "tpot_timeline": _latency_timeline(
                    tpot_records,
                    arrival_seconds=arrival_seconds,
                    end_seconds=(max(completion_seconds) if completion_seconds else None),
                ),
                "ttft_heatmap": latency_heatmaps["ttft_heatmap"],
                "tpot_heatmap": latency_heatmaps["tpot_heatmap"],
                "throughput_timeline": throughput_timeline,
                "goodput_timeline": self.goodput_timeline_from_artifacts(task),
                "error_timeline": error_timeline,
                "status_code_breakdown": self.status_code_breakdown_from_artifacts(task),
                "client_error_requests": len(transport_errors),
                "latency": latency("e2e", latency_records),
                "ttft": latency("ttft", ttft_records),
                "tpot": latency("tpot", tpot_records),
                "drift": _distribution(drift_values),
            },
            artifacts=artifacts,
            per_request=[],
            backend_metrics=raw,
            warnings=(
                [
                    "Simulation reached the grace-period limit while requests were still in-flight; "
                    "results reflect only the requests that completed before the limit."
                ]
                if command.timed_out
                else []
            ),
        )
