"""Typed contracts for multi-workload Evaluation orchestration."""

from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


def validate_inline_workload(content: str) -> dict:
    """Reject incomplete inline workloads before provisioning benchmark resources."""
    try:
        workload = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ValueError(f"workload_yaml is not valid YAML: {error}") from error
    if not isinstance(workload, dict):
        raise ValueError("workload_yaml must contain a YAML mapping")
    for required in ("load", "api", "data"):
        if not isinstance(workload.get(required), dict):
            raise ValueError(f"workload_yaml requires a {required} mapping")
    load = workload["load"]
    if load.get("type", "constant") in ("constant", "poisson", "concurrent"):
        stages = load.get("stages")
        sweep = load.get("sweep")
        if stages is not None and (
            not isinstance(stages, list) or any(not isinstance(stage, dict) or not stage for stage in stages)
        ):
            raise ValueError("workload_yaml load.stages must be a list of non-empty stage mappings")
        if sweep is not None and not isinstance(sweep, dict):
            raise ValueError("workload_yaml load.sweep must be a mapping")
        if not stages and not sweep:
            raise ValueError("workload_yaml requires non-empty load.stages or load.sweep")
    return workload


class RuntimeSpec(BaseModel):
    http_proxy: str = Field(default="", max_length=1000)
    https_proxy: str = Field(default="", max_length=1000)
    no_proxy: str = Field(
        default="",
        max_length=2000,
        description=(
            "Comma-separated benchmark proxy bypass entries, merged with backend NO_PROXY "
            "and no_proxy plus localhost, 127.0.0.1, .svc, and .cluster.local. "
            "Applies to harness Pods and Helm; the benchmark CLI does not inherit external proxies."
        ),
    )


class ConcurrencyStage(BaseModel):
    """One closed-loop concurrency stage of a generated matrix workload (inference-perf ``type: concurrent``)."""

    concurrency: int = Field(ge=1, le=4096)
    num_requests: int = Field(ge=1, le=100_000)


class WorkloadMatrixPoint(BaseModel):
    """One (input length, output length) point of an ISL x OSL x concurrency sweep.

    Each point runs as its own ``llmdbenchmark`` invocation against a generated, exact-length
    inference-perf workload (``ignore_eos: true``, fixed input/output distributions); the
    concurrency dimension is covered within that single invocation via multiple load stages,
    whose per-stage results are read back individually.
    """

    isl: int = Field(ge=1, le=1_000_000, description="Exact input sequence length in tokens")
    osl: int = Field(ge=1, le=1_000_000, description="Exact output sequence length in tokens")


class RateStage(BaseModel):
    """One open-loop, poisson-arrival rate stage of a generated shared-prefix workload."""

    rate: float = Field(gt=0, le=10_000, description="Target requests/second during this stage")
    duration: int = Field(ge=1, le=86_400, description="Stage duration in seconds")


class SharedPrefixWorkloadSpec(BaseModel):
    """An open-loop workload where many requests reuse a small set of shared system prompts.

    Unlike the closed-loop, exact-length ``matrix`` sweep above (fixed ISL/OSL, one concurrency
    stage at a time), this drives a poisson-arrival rate ramp against reused system-prompt
    groups so prefix-cache reuse/offloading dominates achievable throughput and latency. This
    is a generic workload shape usable by any Guide/BenchmarkPlan under evaluation -- it makes
    no assumption about which provider or guide is being benchmarked.
    """

    num_groups: int = Field(ge=1, le=100_000, description="Number of unique shared system-prompt groups")
    num_prompts_per_group: int = Field(ge=1, le=10_000, description="Requests sharing each group's system prompt")
    system_prompt_len: int = Field(ge=1, le=1_000_000, description="Shared system-prompt length in tokens")
    question_len: int = Field(ge=1, le=1_000_000, description="Per-request user question length in tokens")
    output_len: int = Field(ge=1, le=1_000_000, description="Generated output length in tokens")
    enable_multi_turn_chat: bool = False
    stages: list[RateStage] = Field(min_length=1, max_length=50)


class BenchmarkSpec(BaseModel):
    harness: str = Field(default="inference-perf", pattern=r"^[a-z0-9][a-z0-9-]*$")
    workload: str = Field(default="sanity_random.yaml", pattern=r"^[A-Za-z0-9._-]+$")
    workload_yaml: str | None = Field(
        default=None,
        max_length=262_144,
        description="Optional inline workload YAML, scoped to this benchmark run.",
    )
    parallelism: int = Field(default=1, ge=1, le=32)
    wait_timeout_seconds: int = Field(default=1800, ge=1, le=14400)
    harness_memory_gib: int = Field(
        default=8,
        ge=1,
        le=512,
        strict=True,
        description="Host memory request and limit in GiB per benchmark worker; independent of model GPU memory.",
    )
    accelerator_profile: str | None = Field(
        default=None, max_length=253, pattern=r"^[a-z0-9]([-.a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-.a-z0-9]*[a-z0-9])?)+$"
    )
    storage_class_name: str | None = Field(default=None, max_length=253, pattern=r"^[a-z0-9]([-.a-z0-9]*[a-z0-9])?$")
    matrix: list[WorkloadMatrixPoint] = Field(default_factory=list, max_length=50)
    concurrency_stages: list[ConcurrencyStage] = Field(default_factory=list, max_length=20)
    warmup_requests: int = Field(
        default=2,
        ge=0,
        le=50,
        description=(
            "Uncounted requests run once before the matrix sweep starts (at the first "
            "point's ISL/OSL, concurrency 1) to absorb JIT/torch-compile cold-start cost "
            "before the first measured point. 0 disables warm-up. Ignored when matrix is empty."
        ),
    )
    shared_prefix: SharedPrefixWorkloadSpec | None = Field(
        default=None,
        description=(
            "Optional open-loop, shared-system-prompt workload generator -- an alternative to "
            "`matrix` for stressing prefix-cache reuse/offloading. Mutually exclusive with `matrix`."
        ),
    )

    @model_validator(mode="after")
    def exclusive_workload_generators(self) -> BenchmarkSpec:
        generators = int(bool(self.matrix)) + int(self.shared_prefix is not None) + int(bool(self.workload_yaml))
        if generators > 1:
            raise ValueError("matrix, shared_prefix, and workload_yaml are mutually exclusive workload generators")
        if self.workload_yaml:
            validate_inline_workload(self.workload_yaml)
        return self


class BenchmarkSource(BaseModel):
    repository: str = Field(min_length=1, max_length=500, pattern=r"^https://[^\s@]+$")
    revision: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class ComparisonCoreParameters(BaseModel):
    replicas: int | None = Field(
        default=None,
        ge=1,
        le=32,
        description=(
            "Explicit independent-baseline replica count; overrides automatic GPU-budget matching. "
            "When omitted for a PD source, defaults to (prefill replicas * prefill TP + decode replicas * "
            "decode TP) / effective baseline TP, which must be an integer in 1-32 or creation returns 409. "
            "Non-PD sources inherit decode/serving replicas. Same-pod comparisons do not create replicas."
        ),
    )
    tensor_parallel_size: int | None = Field(
        default=None,
        ge=1,
        le=16,
        description=(
            "Independent-baseline TP; defaults to source decode/serving TP. For PD sources, changing TP without "
            "explicit replicas recalculates replicas to preserve the total P+D GPU budget."
        ),
    )
    max_model_len: int | None = Field(default=None, ge=1, le=1_000_000)
    max_num_seqs: int | None = Field(default=None, ge=1, le=100_000)
    gpu_memory_utilization: float | None = Field(default=None, ge=0.1, le=1.0)
    block_size: int | None = Field(default=None, ge=1, le=1024)
    max_num_batched_tokens: int | None = Field(default=None, ge=1, le=1_000_000)


class BenchmarkSlaTargets(BaseModel):
    """Optional pass targets attached to one benchmark scenario."""

    ttft_ms: float | None = Field(default=None, gt=0, le=3_600_000)
    ttft_percentile: Literal["p50", "p90", "p95", "p99"] = "p99"
    tpot_ms: float | None = Field(default=None, gt=0, le=3_600_000)
    tpot_percentile: Literal["p50", "p90", "p95", "p99"] = "p99"
    throughput_min_tps: float | None = Field(default=None, ge=0)
    success_rate_min_percent: float | None = Field(default=None, ge=0, le=100)


class BenchmarkScenario(BaseModel):
    """One reusable workload in a deployment's benchmark suite."""

    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    benchmark: BenchmarkSpec = Field(default_factory=BenchmarkSpec)
    sla_targets: BenchmarkSlaTargets = Field(default_factory=BenchmarkSlaTargets)


class BenchmarkPlan(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    configuration_artifact_id: str = Field(min_length=36, max_length=36)
    deployment_name: str | None = Field(default=None, min_length=1, max_length=120)
    deployment_description: str | None = Field(default=None, max_length=1000)
    benchmark: BenchmarkSpec = Field(default_factory=BenchmarkSpec)
    include_configuration: bool = True
    include_baseline: bool = True
    baseline_type: Literal[
        "direct-vllm",
        "router-neutral",
        "router-round-robin",
        "load-only",
        "affinity-only",
        "optimized-baseline",
        "kubernetes-service",
    ] = "direct-vllm"
    baseline_types: list[
        Literal[
            "direct-vllm",
            "router-neutral",
            "router-round-robin",
            "load-only",
            "affinity-only",
            "optimized-baseline",
            "kubernetes-service",
        ]
    ] = Field(default_factory=list, max_length=7)
    baseline_parameters: dict[str, ComparisonCoreParameters] = Field(default_factory=dict)
    preserve_deployment: bool = False
    scenarios: list[BenchmarkScenario] = Field(default_factory=list, min_length=0, max_length=20)

    @model_validator(mode="after")
    def normalize_baseline_types(self) -> BenchmarkPlan:
        if not self.include_baseline:
            self.baseline_types = []
            return self
        requested = self.baseline_types or [self.baseline_type]
        normalized = ["router-neutral" if item == "router-round-robin" else item for item in requested]
        self.baseline_types = list(dict.fromkeys(normalized))
        self.baseline_type = self.baseline_types[0]
        return self

    @model_validator(mode="after")
    def unique_scenario_ids(self) -> BenchmarkPlan:
        if not self.include_configuration and not self.baseline_types:
            raise ValueError("Select at least one configuration or comparison")
        if not self.include_configuration and "kubernetes-service" in self.baseline_types:
            raise ValueError("Same-pod comparison requires the full configuration")
        ids = [scenario.id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark scenario ids must be unique within a plan")
        return self


class EvaluationCreateRequest(BaseModel):
    name: str = Field(default="Evaluation", min_length=1, max_length=200)
    cluster_session_id: str = Field(min_length=36, max_length=36)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    benchmark_source: BenchmarkSource | None = None
    benchmark_plans: list[BenchmarkPlan] = Field(min_length=1, max_length=50)
    compare_configurations: bool = Field(
        default=True,
        description=(
            "When multiple candidate configurations are in one Evaluation, generate pairwise "
            "absolute/matrix comparison rows in addition to each candidate's explicit baselines."
        ),
    )

    @model_validator(mode="after")
    def unique_workload_ids(self) -> EvaluationCreateRequest:
        ids = [plan.id for plan in self.benchmark_plans]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark plan ids must be unique")
        return self


class EvaluateRunRequest(BenchmarkSpec):
    """Target binding around the same workload contract used by workflow plans."""

    sla_targets: BenchmarkSlaTargets = Field(default_factory=BenchmarkSlaTargets)
    deployment_execution_id: str | None = Field(
        default=None,
        min_length=1,
        description="A specific deployment execution to benchmark directly. Mutually exclusive with model_service_group_id.",
    )
    model_service_group_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "A published Model Service to benchmark instead of a raw deployment: the run resolves "
            "to one of the group's currently healthy, authorized members and calls it by its "
            "published name through the cluster's shared Gateway, the same path real clients use. "
            "Requires api_key. Mutually exclusive with deployment_execution_id."
        ),
    )
    cluster_session_id: str | None = Field(default=None, min_length=36, max_length=36)

    @model_validator(mode="after")
    def exactly_one_target(self) -> EvaluateRunRequest:
        if bool(self.deployment_execution_id) == bool(self.model_service_group_id):
            raise ValueError("set exactly one of deployment_execution_id or model_service_group_id")
        if self.model_service_group_id and not self.api_key:
            raise ValueError("model_service_group_id requires api_key (a model access token)")
        return self
    specification_file: str = Field(
        default="guides/optimized-baseline",
        min_length=1,
        max_length=500,
        description=(
            "Benchmark specification; when omitted, resolved from the deployment's guide, with optimized-baseline "
            "used for plain vLLM or unknown guides."
        ),
    )
    http_proxy: str = Field(default="", max_length=1000)
    https_proxy: str = Field(default="", max_length=1000)
    no_proxy: str = Field(
        default="",
        max_length=2000,
        description=(
            "Comma-separated benchmark proxy bypass entries, merged with backend NO_PROXY "
            "and no_proxy plus localhost, 127.0.0.1, .svc, and .cluster.local. "
            "Applies to harness Pods and Helm; the benchmark CLI does not inherit external proxies."
        ),
    )
    benchmark_source: BenchmarkSource | None = None
    use_baseline_endpoint: bool = Field(
        default=False,
        description=(
            "Target the deployment execution's `baseline_url` (a plain Kubernetes Service that "
            "bypasses the Guide's routing layer, e.g. round-robin across the same pods) instead "
            "of its normal routed `url`. Requires the Guide provider to expose a baseline "
            "endpoint; fails clearly otherwise."
        ),
    )
    api_key: str | None = Field(
        default=None,
        exclude=True,
        max_length=4096,
        description=(
            "Model access token (lens-mk-...) the harness sends as OPENAI_API_KEY when the "
            "deployment is reached through the cluster's shared Gateway. Run-only; never persisted."
        ),
    )


class EvaluateWorkflowRequest(BaseModel):
    model: str = Field(min_length=1, max_length=300)
    cluster_session_id: str = Field(min_length=36, max_length=36)
    replicas: int = Field(default=1, ge=1, le=32)
    tensor_parallel_size: int = Field(default=1, ge=1, le=16)
    image: str = Field(default="", max_length=500)
    harness: str = Field(default="inference-perf", pattern=r"^[a-z0-9][a-z0-9-]*$")
    workload: str = Field(default="sanity_random.yaml", pattern=r"^[A-Za-z0-9._-]+$")
    parallelism: int = Field(default=1, ge=1, le=32)
