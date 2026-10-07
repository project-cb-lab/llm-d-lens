"""Typed domain models for Simulation tasks, backends, and results."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeAlias
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SimulationTaskStatus: TypeAlias = Literal["queued", "running", "completed", "failed", "cancelled"]
SimulationScenario: TypeAlias = Literal["chat", "api-calling", "coding"]
Scenario: TypeAlias = SimulationScenario
BackendName: TypeAlias = Literal["aiperf", "trace-replayer"]
TraceFormat: TypeAlias = Literal[
    "mooncake_trace",
    "bailian_trace",
    "baseten_trace",
    "burst_gpt_trace",
    "weka_public_dataset",
]
EndpointMode: TypeAlias = Literal["external", "in-cluster", "deployment"]
LatencyMetric: TypeAlias = Literal["ttft", "tpot"]
SequenceLengthKind: TypeAlias = Literal["input", "output"]
SequenceLengthSource: TypeAlias = Literal["observed", "requested"]


class FrozenDomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _FrozenDict(dict):
    def _immutable(self, *_args, **_kwargs) -> None:
        raise TypeError("Frozen mapping cannot be modified")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> _FrozenDict:
        return _FrozenDict({deepcopy(key, memo): deepcopy(value, memo) for key, value in self.items()})


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


class SimulationTaskCreateRequest(FrozenDomainModel):
    name: str = "Trace simulation"
    description: str = ""
    scenario: SimulationScenario
    backend: BackendName = "trace-replayer"
    endpoint_mode: EndpointMode = "external"
    cluster_session_id: str | None = Field(default=None, min_length=36, max_length=36)
    endpoint_namespace: str | None = None
    endpoint_service: str | None = None
    endpoint_deployment_execution_id: str | None = None
    #: A published Model Service group id. When set, the backend resolves it to
    #: one of its currently healthy, authorized members and routes through the
    #: cluster's shared Gateway using its published name, exactly like
    #: endpoint_mode "deployment" -- the caller does not also pick a deployment
    #: execution directly. Requires api_key (the HTTPRoute requires auth).
    model_service_group_id: str | None = Field(default=None, min_length=1)
    endpoint_cluster_id: str | None = None
    endpoint_cluster_name: str | None = None
    endpoint_deployment_name: str | None = None
    endpoint_url: str
    model_name: str = Field(min_length=1)
    #: User-supplied model access token (``lens-mk-...``) used when the harness
    #: must call a deployment through the cluster's shared Gateway. Never
    #: persisted on the task and never returned by the API (``exclude=True``).
    api_key: str | None = Field(default=None, exclude=True, max_length=4096)
    trace_dataset: str = Field(min_length=1)
    trace_path: str = Field(min_length=1)
    duration_seconds: int = Field(default=60, ge=1)
    scale_factor: float = Field(default=1, gt=0)
    trace_start_seconds: float = Field(default=0, ge=0)
    trace_end_seconds: float | None = Field(default=None, gt=0)
    trace_timeout_seconds: int = Field(default=3600, ge=1)
    fixed_schedule: bool = True
    stream: bool = True
    backend_options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Input should be a valid HTTP or HTTPS URL")
        return value

    @model_validator(mode="after")
    def validate_trace_range(self) -> SimulationTaskCreateRequest:
        if self.trace_end_seconds is not None and self.trace_end_seconds <= self.trace_start_seconds:
            raise ValueError("trace_end_seconds must be greater than trace_start_seconds")
        return self

    @model_validator(mode="after")
    def validate_model_service_target(self) -> SimulationTaskCreateRequest:
        if self.model_service_group_id and not self.api_key:
            raise ValueError("model_service_group_id requires api_key (a model access token)")
        return self


class SimulationTaskRerunRequest(FrozenDomainModel):
    """Optional overrides accepted when re-running a simulation task.

    These fields let a client re-bind a legacy task (created before deployment
    identity was persisted) to the currently selected ready deployment, so the
    backend can re-resolve the latest port-forward URL instead of replaying the
    stale ``endpoint_url`` captured when the task was first created.
    """

    endpoint_url: str | None = None
    endpoint_deployment_execution_id: str | None = None
    endpoint_cluster_id: str | None = None
    endpoint_cluster_name: str | None = None
    endpoint_deployment_name: str | None = None
    #: Re-bind the legacy task to a published, health-probed Model Service instead
    #: of a raw deployment execution id. Mutually preferred over
    #: ``endpoint_deployment_execution_id`` when both are supplied.
    model_service_group_id: str | None = None
    #: Re-supply the model access token for a rerun; it is never persisted.
    api_key: str | None = Field(default=None, exclude=True, max_length=4096)


class TraceDatasetDownloadRequest(FrozenDomainModel):
    dataset: str = Field(min_length=1)
    force: bool = False


class ModelDiscoveryRequest(FrozenDomainModel):
    endpoint_url: str = Field(min_length=1, max_length=2000)


class SimulationArtifact(FrozenDomainModel):
    uri: str | None = None
    kind: str
    path: str
    media_type: str


class SimulationRequestResult(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=True)

    request_id: str | int | None = None
    status_code: int | None = None
    successful: bool | None = None
    error: str | None = None
    started_at_seconds: float | None = None
    completed_at_seconds: float | None = None
    latency_ms: float | None = None
    ttft_ms: float | None = None
    tpot_ms: float | None = None
    input_tokens: float | None = None
    output_tokens: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SimulationResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, validate_assignment=True)

    run_id: str
    backend: BackendName
    backend_version: str | None = None
    summary: dict[str, Any]
    artifacts: list[SimulationArtifact]
    per_request: list[SimulationRequestResult]
    backend_metrics: dict[str, Any]
    warnings: list[str]


class SimulationConfig(FrozenDomainModel):
    backend: BackendName
    backend_options: dict[str, Any]
    duration_seconds: float = Field(gt=0)
    num_requests: None = None
    stream: bool
    warmup_enabled: Literal[False] = False
    grace_period_seconds: float = Field(ge=0)

    @field_validator("backend_options", mode="after")
    @classmethod
    def freeze_backend_options(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _freeze_json_value(value)


class SimulationDataset(FrozenDomainModel):
    name: str | None = None
    scenario: SimulationScenario | None = None
    tokenizer: str | None = None


class SimulationTrace(FrozenDomainModel):
    path: str
    format: TraceFormat
    fixed_schedule: bool
    timeout_seconds: float = Field(gt=0)
    synthesis_speedup_ratio: float = Field(gt=0)
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> SimulationTrace:
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class SimulationPrompt(FrozenDomainModel):
    type: Literal["trace"]
    dataset: SimulationDataset
    trace: SimulationTrace


class SimulationTask(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, validate_assignment=True)

    id: str
    name: str
    description: str
    scenario: SimulationScenario
    status: SimulationTaskStatus
    endpoint_mode: EndpointMode = "external"
    endpoint_namespace: str | None = None
    endpoint_service: str | None = None
    endpoint_deployment_execution_id: str | None = None
    # Set when this task targets a published Model Service rather than a raw
    # deployment: its endpoint always resolves through the shared Gateway
    # (see ``_resolve_task_endpoint_url``), regardless of what data plane the
    # underlying deployment happens to have rendered at deploy time.
    model_service_group_id: str | None = None
    # Legacy identifiers retained so historical task records stay readable.
    endpoint_deployment_run_id: str | None = None
    endpoint_deployment_case_id: str | None = None
    endpoint_cluster_id: str | None = None
    endpoint_cluster_name: str | None = None
    endpoint_deployment_name: str | None = None
    endpoint_url: str
    model_name: str
    #: In-memory model access token for this run only: excluded from persistence
    #: and API responses so the plaintext never lands at rest.
    api_key: str | None = Field(default=None, exclude=True)
    simulation: SimulationConfig
    prompt: SimulationPrompt
    task_dir: str
    artifact_uri: str | None = None
    artifact_error: str | None = None
    logs_truncated: bool = False
    artifacts_incomplete: bool = False
    backend_version_inferred: bool = False
    backend_version: str | None = None
    configuration_ids: list[str] = Field(default_factory=list)
    progress_percent: float = Field(ge=0, le=100)
    progress_message: str
    logs: list[str]
    result: SimulationResult | None = None
    error_message: str | None = None
    created_at: str
    started_at: str | None = None
    execution_started_at: str | None = None
    completed_at: str | None = None
    owner_pid: int | None = None
    owner_instance_id: str | None = None
    live_summary: dict[str, Any] | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if len(value) != 8 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("task id must be 8 lowercase hexadecimal characters")
        return value

    @field_validator("created_at", "started_at", "execution_started_at", "completed_at")
    @classmethod
    def validate_timestamp(cls, value: str | None) -> str | None:
        if value is not None:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class SimulationTaskListPage(FrozenDomainModel):
    tasks: list[SimulationTask]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    counts: dict[SimulationTaskStatus, int]
    facets: dict[str, list[str]]


class ScenarioDescriptor(FrozenDomainModel):
    name: SimulationScenario
    display_name: str
    description: str
    supported_backends: list[str] = Field(default_factory=list)


class BackendAvailability(FrozenDomainModel):
    available: bool
    version: str | None
    unavailable_reason: str | None


class BackendDescriptor(FrozenDomainModel):
    name: str
    display_name: str
    api_version: int = 1
    available: bool
    version: str | None
    unavailable_reason: str | None
    scenarios: list[ScenarioDescriptor]
    capabilities: dict[str, Any]
    managed_installation: bool | None = None


class SimulationResponseCodeIssue(FrozenDomainModel):
    request_number: int = Field(ge=1)
    request_id: str | int | None
    status_code: int | None
    error: str | None
    started_at_seconds: float | None
    latency_ms: float | None
    input_tokens: float | None
    output_tokens: float | None


class SimulationResponseCodeIssueSlice(FrozenDomainModel):
    total: int = Field(ge=0)
    items: list[SimulationResponseCodeIssue]


class SimulationResponseCodeIssuePage(FrozenDomainModel):
    task_id: str
    status_code: int | None
    items: list[SimulationResponseCodeIssue] = Field(serialization_alias="issues")
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)


class SimulationLatencyHeatmapCell(FrozenDomainModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    sequence_length_start: int = Field(ge=0)
    sequence_length_end: int = Field(ge=1)
    request_count: int = Field(ge=1)
    average_metric_ms: float = Field(ge=0)


class SimulationLatencyHeatmap(FrozenDomainModel):
    metric: LatencyMetric
    sequence_length: SequenceLengthKind
    sequence_length_source: SequenceLengthSource
    time_bin_count: int = Field(ge=1)
    sequence_length_bin_count: int = Field(ge=1)
    minimum_metric_ms: float = Field(ge=0)
    maximum_metric_ms: float = Field(ge=0)
    cells: list[SimulationLatencyHeatmapCell]


class SimulationGoodputTimelinePoint(FrozenDomainModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    completed_requests: int = Field(ge=0)
    good_requests: int = Field(ge=0)
    request_throughput_rps: float = Field(ge=0)
    goodput_rps: float = Field(ge=0)


@dataclass(frozen=True, slots=True)
class BackendCommand:
    args: tuple[str, ...]
    timeout_seconds: int
    #: Extra environment for the backend process (e.g. ``OPENAI_API_KEY`` for a
    #: model access token). Never logged; the command line is not env-aware.
    env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("Backend command timeout_seconds must be greater than zero")


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    arrived: float | None
    completed: float | None
    successful: bool
    status_code: int | None = None
    error: str | None = None
    request_id: str | int | None = None
    input_tokens: float | None = None
    output_tokens: float | None = None
    requested_output_tokens: float | None = None
    latency_ms: float | None = None
    ttft_ms: float | None = None
    tpot_ms: float | None = None
    latency_arrived: float | None = None
    latency_completed: float | None = None
    rate_completed: float | None = None
    rate_output_tokens: float | None = None
    issue_failed: bool | None = None

    @property
    def latency_arrival(self) -> float | None:
        return self.latency_arrived if self.latency_arrived is not None else self.arrived

    @property
    def latency_completion(self) -> float | None:
        return self.latency_completed if self.latency_completed is not None else self.completed

    @property
    def rate_completion(self) -> float | None:
        return self.rate_completed if self.rate_completed is not None else self.completed

    @property
    def rate_tokens(self) -> float:
        value = self.rate_output_tokens if self.rate_output_tokens is not None else self.output_tokens
        return value or 0

    @property
    def failed_issue(self) -> bool:
        return self.issue_failed if self.issue_failed is not None else not self.successful


# Compatibility aliases for callers importing the previous validation-only names.
ArtifactModel = SimulationArtifact
ResultModel = SimulationResult
SimulationConfigModel = SimulationConfig
DatasetModel = SimulationDataset
TraceModel = SimulationTrace
PromptModel = SimulationPrompt
TaskModel = SimulationTask
