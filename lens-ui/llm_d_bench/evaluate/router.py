"""Evaluate ready deployments with the local llm-d-benchmark CLI."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml
from fastapi import APIRouter, HTTPException, Request

from llm_d_bench.auth.access import (
    current_principal,
    owner_for_create,
    resource_readable,
)
from llm_d_bench.cluster import deployment_runtime_overrides, require_active_session, sessions
from llm_d_bench.cluster import registry as cluster_registry
from llm_d_bench.cluster.deployment_source import resolve_cluster_benchmark_source
from llm_d_bench.configuration.service import get_configuration_artifact
from llm_d_bench.db.dao.evaluate_run import EvaluateRunDao
from llm_d_bench.db.dao.evaluate_workflow import EvaluateWorkflowDao
from llm_d_bench.db.evaluate_persistence_models import EvaluateRunRecord, EvaluateWorkflowRecord
from llm_d_bench.deploy.application import cluster_deployment_source, deployment_run_manager
from llm_d_bench.deploy.capabilities import provider_supports
from llm_d_bench.deploy.contracts import (
    DeployableConfiguration,
    DeploymentRunCreateRequest,
    DeploymentStatus,
    RuntimeBinding,
)
from llm_d_bench.deploy.data_plane import deployment_uses_shared_gateway
from llm_d_bench.deploy.usage import register_usage_probe
from llm_d_bench.evaluate.api_models import (
    BenchmarkDefaultsResponse,
    BenchmarkRunListResponse,
    BenchmarkRunResponse,
    EvaluationCancelResponse,
    EvaluationDetailsResponse,
    EvaluationWorkflowListResponse,
    EvaluationWorkflowResponse,
    evaluation_problem_responses,
)
from llm_d_bench.evaluate.comparison import (
    matrix_comparison as _matrix_comparison,
)
from llm_d_bench.evaluate.comparison import (
    rate_stage_comparison as _rate_stage_comparison,
)
from llm_d_bench.evaluate.execution import case_benchmark_request, execute_case_benchmark
from llm_d_bench.evaluate.harness_watch import watch_harness
from llm_d_bench.evaluate.models import (
    BenchmarkSpec,
    ConcurrencyStage,
    EvaluateRunRequest,
    EvaluateWorkflowRequest,
    EvaluationCreateRequest,
    SharedPrefixWorkloadSpec,
    WorkloadMatrixPoint,
    validate_inline_workload,
)
from llm_d_bench.evaluate.request_evidence import apply_request_evidence, enable_request_reports
from llm_d_bench.evaluate.workflow_state import TERMINAL_EVALUATION_STATUSES as _TERMINAL_EVALUATE_STATUSES
from llm_d_bench.hardware.resolver import resolve_by_accelerator_key, resolve_by_device_class
from llm_d_bench.model_service.resolution import resolve_model_service_target
from llm_d_bench.monitoring.deployment import service as deployment_monitoring
from llm_d_bench.monitoring.kv_trace.collector import KVTraceCollector
from llm_d_bench.monitoring.profiling.service import (
    collect_benchmark_observability,
    wait_for_deployment_metrics,
)
from llm_d_bench.utils.artifact_store import register_artifacts
from llm_d_bench.utils.artifacts import configuration_checksum
from llm_d_bench.utils.kubernetes_proxy import api_server_host
from llm_d_bench.utils.paths import storage_path
from llm_d_bench.versions import router_chart_version, stack

router = APIRouter(prefix="/api/v1/evaluate", tags=["evaluate"])
_store = deployment_run_manager.store
_application_root = Path(__file__).resolve().parents[2]
_results_root = storage_path("data", "artifacts", "evaluations")
_runs_directory = storage_path("data", "metadata", "evaluations")
_benchmark_directory = _runs_directory / "runs"
_workflow_directory = _runs_directory / "workflows"
_benchmark_directory.mkdir(parents=True, exist_ok=True)
_workflow_directory.mkdir(parents=True, exist_ok=True)
_benchmark_records = EvaluateRunDao()
_workflow_records = EvaluateWorkflowDao()
_tasks: dict[str, asyncio.Task[None]] = {}
_workflow_tasks: dict[str, asyncio.Task[None]] = {}
_active_workflows: dict[str, dict] = {}
#: Run-only model access tokens (never persisted) keyed by benchmark run id.
_run_api_keys: dict[str, str] = {}
_benchmark_install_lock = asyncio.Lock()
_MONITORING_PREPARE_TIMEOUT = 30.0
_OBSERVABILITY_COLLECT_TIMEOUT = 120.0
_BENCHMARK_REPOSITORY = "https://github.com/llm-d/llm-d-benchmark.git"
_BENCHMARK_REVISION = stack().llm_d_benchmark
_BENCHMARK_PLANNER = "git+https://github.com/llm-d-incubation/llm-d-planner.git@v0.1.0"
# llm-d-benchmark owns its component versions (its `chartVersions` defaults);
# Lens only pins the shared llm-d-router release so benchmark and deployment
# runs stay on the same router.
_BENCHMARK_VERSION_OVERRIDES = (f"chartVersions.llmDRouter={router_chart_version()}",)
# Default concurrency sweep for a matrix point when the caller doesn't supply its own
# concurrency_stages: closed-loop (inference-perf `type: concurrent`) stages, each run
# in the same llmdbenchmark invocation and read back individually via its own
# stage_<index>_lifecycle_metrics.json.
_DEFAULT_CONCURRENCY_STAGES = (
    ConcurrencyStage(concurrency=1, num_requests=32),
    ConcurrencyStage(concurrency=8, num_requests=96),
    ConcurrencyStage(concurrency=32, num_requests=96),
    ConcurrencyStage(concurrency=64, num_requests=192),
)


def _cluster_benchmark_runtime(session_id: str) -> dict[str, str | None]:
    """Use the selected cluster's registered benchmark checkout."""
    session = require_active_session(session_id)
    cluster = cluster_registry.require_cluster(session.server_id)
    return resolve_cluster_benchmark_source(cluster)


def _validate_evaluation_capacity(configuration: DeployableConfiguration) -> None:
    """Reject clearly impossible XPU topologies before any cluster mutation."""
    content = configuration.content
    runtime = content.get("runtime") or {}
    source = (content.get("officialGuide") or {}).get("source") or {}
    if source.get("accelerator") != "xpu" and "xpu" not in str(runtime.get("image") or "").lower():
        return
    match = re.search(
        r"(?:^|[-_/])(\d+(?:\.\d+)?)b(?:[-_/]|$)", str((content.get("model") or {}).get("name") or ""), re.IGNORECASE
    )
    memory_gib = float(os.environ.get("PRISM_ACCELERATOR_MEMORY_GIB", "0") or 0)
    if match and memory_gib > 0:
        footprint_gib = float(match.group(1)) * 2 * 1.2 * 1e9 / 1024**3
        recommended_tp = max(1, math.ceil(footprint_gib / (memory_gib * 0.9)))
        components = [
            (name, content.get(name))
            for name in ("serving", "prefill", "decode")
            if isinstance(content.get(name), dict)
        ]
        for name, component in components:
            tp = component.get("tensorParallelSize")
            if isinstance(tp, int) and tp < recommended_tp:
                raise ValueError(
                    f"{name} TP={tp} is too small for the estimated {footprint_gib:.1f} GiB model footprint "
                    f"on {memory_gib:g} GiB XPU cards; use TP>={recommended_tp} or a smaller/quantized model"
                )
    allowed_devices = [item for item in os.environ.get("PRISM_GPU_PCI_ALLOWLIST", "").split(",") if item.strip()]
    if allowed_devices:
        components = [
            component
            for name, component in (
                ("prefill", content.get("prefill")),
                ("decode", content.get("decode") or content.get("serving")),
            )
            if isinstance(component, dict) and (name == "decode" or configuration.provider_ref == "pd-disaggregation")
        ]
        required = sum(
            int(component.get("replicaCount") or 0) * int(component.get("tensorParallelSize") or 0)
            for component in components
        )
        if required > len(allowed_devices):
            raise ValueError(
                f"configuration requires {required} XPU cards, but only {len(allowed_devices)} are available to Prism"
            )


@router.get(
    "/benchmark-defaults",
    summary="Get llm-d-benchmark default repository and runtime settings Lens Evaluate will use.",
    description=(
        "Get llm-d-benchmark default repository and runtime settings Lens Evaluate will use. "
        "Use this to understand whether benchmarking runs locally or from a managed source checkout."
    ),
    operation_id="get_benchmark_defaults",
    responses=evaluation_problem_responses(401, 403, 422),
    response_model=BenchmarkDefaultsResponse,
    response_model_exclude_unset=True,
)
async def benchmark_defaults() -> dict[str, object]:
    configured_executable = os.environ.get("LLM_D_BENCHMARK_EXECUTABLE", "").strip()
    configured_root = os.environ.get("LLM_D_BENCHMARK_ROOT", "").strip()
    return {
        "mode": "local-runtime" if configured_executable or shutil.which("llmdbenchmark") else "managed-source",
        "repository": _BENCHMARK_REPOSITORY,
        "revision": _BENCHMARK_REVISION,
        "localRuntimeConfigured": bool(configured_executable or configured_root or shutil.which("llmdbenchmark")),
    }


def _baseline_key(configuration: DeployableConfiguration, benchmark: dict) -> str:
    content = configuration.content
    payload = {
        "model": content.get("model"),
        "decode": content.get("decode") or content.get("serving"),
        "runtime": content.get("runtime"),
        "custom_parameters": content.get("customParameters"),
        "benchmark": benchmark,
    }
    return configuration_checksum(payload)


def _baseline_configuration(
    source: DeployableConfiguration,
    baseline_type: str,
    workload_ids: list[str],
    source_artifact_id: str,
    parameters: dict | None = None,
) -> DeployableConfiguration:
    """Build an independent baseline, preserving a PD candidate's total GPU budget by default."""
    source_content = source.content
    baseline_content = {
        "model": source_content.get("model"),
        "decode": source_content.get("decode") or source_content.get("serving"),
        **({"runtime": source_content["runtime"]} if isinstance(source_content.get("runtime"), dict) else {}),
        **(
            {"customParameters": source_content["customParameters"]}
            if isinstance(source_content.get("customParameters"), list)
            else {}
        ),
        **(
            {"modelSecret": source_content["officialGuide"]["modelSecret"]}
            if isinstance(source_content.get("officialGuide"), dict)
            and isinstance(source_content["officialGuide"].get("modelSecret"), dict)
            else {}
        ),
    }
    provider_ref = "baseline-vllm"
    if baseline_type in {"router-neutral", "router-round-robin", "load-only", "affinity-only", "optimized-baseline"}:
        provider_ref = "optimized-baseline"
        if source.provider_ref == "optimized-baseline" and not parameters:
            # Ablations change only EPP policy. Reuse the exact saved manifest,
            # chart version and effective values instead of rerendering live defaults.
            baseline_content = deepcopy(source_content)
        baseline_content["routerProfile"] = (
            "router-neutral" if baseline_type in {"router-neutral", "router-round-robin"} else baseline_type
        )
    parameters = parameters or {}
    decode = dict(baseline_content.get("decode") or {})
    field_map = {
        "replicas": "replicaCount",
        "tensor_parallel_size": "tensorParallelSize",
        "max_model_len": "maxModelLen",
        "max_num_seqs": "maxNumSeqs",
    }
    for source_name, target_name in field_map.items():
        if parameters.get(source_name) is not None:
            decode[target_name] = parameters[source_name]
    if source.provider_ref == "pd-disaggregation" and parameters.get("replicas") is None:
        # An aggregated instance performs both prefill and decode. Matching only
        # the decode pool would silently compare different hardware budgets.
        source_decode = source_content.get("decode") or source_content.get("serving") or {}
        prefill = source_content.get("prefill") or {}
        dimensions = [
            prefill.get("replicaCount"),
            prefill.get("tensorParallelSize"),
            source_decode.get("replicaCount"),
            source_decode.get("tensorParallelSize"),
            decode.get("tensorParallelSize"),
        ]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in dimensions):
            raise ValueError("PD baseline GPU budget requires positive integer P/D replicas and tensor parallel sizes")
        prefill_replicas, prefill_tp, decode_replicas, decode_tp, baseline_tp = dimensions
        total_gpus = prefill_replicas * prefill_tp + decode_replicas * decode_tp
        replicas, remainder = divmod(total_gpus, baseline_tp)
        if remainder or not 1 <= replicas <= 32:
            raise ValueError(
                f"PD baseline GPU budget of {total_gpus} cards cannot be matched with "
                f"tensor_parallel_size={baseline_tp} and 1-32 replicas; choose a compatible "
                "tensor_parallel_size or explicitly set replicas for a different resource budget"
            )
        decode["replicaCount"] = replicas
    baseline_content["decode"] = decode
    argument_map = {
        "gpu_memory_utilization": "gpu-memory-utilization",
        "block_size": "block-size",
        "max_num_batched_tokens": "max-num-batched-tokens",
    }
    replaced_arguments = {name for field, name in argument_map.items() if parameters.get(field) is not None}
    custom = [
        item
        for item in baseline_content.get("customParameters", [])
        if not (item.get("kind") == "argument" and item.get("name") in replaced_arguments)
    ]
    custom.extend(
        {"target": "decode", "kind": "argument", "name": argument_name, "value": str(parameters[field_name])}
        for field_name, argument_name in argument_map.items()
        if parameters.get(field_name) is not None
    )
    if custom:
        baseline_content["customParameters"] = custom
    cluster_ref = source.provenance.get("cluster_ref")
    if not isinstance(cluster_ref, dict) or not cluster_ref.get("id"):
        official = source_content.get("officialGuide")
        cluster_ref = official.get("cluster") if isinstance(official, dict) else None
    return DeployableConfiguration(
        type="baseline",
        provider_ref=provider_ref,
        format="helm",
        content=baseline_content,
        checksum=configuration_checksum(baseline_content),
        provenance={
            **({"cluster_ref": deepcopy(cluster_ref)} if isinstance(cluster_ref, dict) else {}),
            "system_generated": True,
            "baseline_type": baseline_type,
            "derived_from_benchmark_plans": workload_ids,
            "derived_from_configuration_artifact_id": source_artifact_id,
        },
    )


def _evaluation_cases(request: EvaluationCreateRequest, artifacts: dict) -> list[dict]:
    cases: list[dict] = []
    baseline_ids: dict[str, str] = {}
    for ordinal, plan in enumerate(request.benchmark_plans):
        artifact = artifacts[plan.configuration_artifact_id]
        configuration = artifact.deployable_configuration
        metadata = {}
        if plan.deployment_name is not None:
            metadata["deployment_name"] = plan.deployment_name
        if plan.deployment_description is not None:
            metadata["description"] = plan.deployment_description
        if metadata:
            configuration = configuration.model_copy(
                update={
                    "provenance": {**configuration.provenance, **metadata},
                }
            )
        if "kubernetes-service" in plan.baseline_types and not provider_supports(
            artifact.deployable_configuration.provider_ref, "supports_kubernetes_service_baseline"
        ):
            raise ValueError(
                f"kubernetes-service baseline is not supported for "
                f"{artifact.deployable_configuration.provider_ref} configurations"
            )
        scenarios = plan.scenarios or [None]
        for scenario_ordinal, scenario in enumerate(scenarios):
            benchmark = (scenario.benchmark if scenario else plan.benchmark).model_dump(mode="json")
            workload_id = f"{plan.id}--{scenario.id}" if scenario else plan.id
            guide_case_id = f"guide-{ordinal + 1}-{scenario_ordinal + 1}" if scenario else f"guide-{ordinal + 1}"
            scenario_metadata = {
                "scenario_id": scenario.id if scenario else None,
                "scenario_name": scenario.name if scenario else None,
                "scenario_description": scenario.description if scenario else "",
                "sla_targets": scenario.sla_targets.model_dump(mode="json", exclude_none=True) if scenario else {},
            }
            plan_baseline_ids = []
            for baseline_type in plan.baseline_types:
                parameter_model = plan.baseline_parameters.get(baseline_type)
                baseline_parameters = (
                    parameter_model.model_dump(mode="json", exclude_none=True) if parameter_model else {}
                )
                baseline_benchmark = {
                    **benchmark,
                    "baseline_type": baseline_type,
                    "baseline_parameters": baseline_parameters,
                }
                key = (
                    f"kubernetes-service:{guide_case_id}"
                    if baseline_type == "kubernetes-service"
                    else f"{plan.id}:{_baseline_key(artifact.deployable_configuration, baseline_benchmark)}"
                )
                baseline_id = baseline_ids.get(key)
                if baseline_id is None:
                    baseline_id = f"baseline-{len(baseline_ids) + 1}"
                    baseline_ids[key] = baseline_id
                    cases.append(
                        {
                            "id": baseline_id,
                            "kind": "baseline",
                            "status": "queued",
                            "workload_ids": [workload_id],
                            "benchmark": baseline_benchmark,
                            "deployment_run_id": None,
                            "evaluation_run_id": None,
                            "configuration_artifact_id": None,
                            "benchmark_plan_id": plan.id,
                            "deployment_group_id": f"{plan.id}:baseline:{baseline_type}",
                            "baseline_type": baseline_type,
                            "baseline_parameters": baseline_parameters,
                            "dependent_guide_case_id": guide_case_id if baseline_type == "kubernetes-service" else None,
                            "preserve_deployment": False,
                            **scenario_metadata,
                        }
                    )
                else:
                    baseline_case = next(item for item in cases if item["id"] == baseline_id)
                    baseline_case["workload_ids"].append(workload_id)
                plan_baseline_ids.append(baseline_id)
            if plan.include_configuration:
                cases.append(
                    {
                        "id": guide_case_id,
                        "kind": "guide",
                        "status": "queued",
                        "workload_ids": [workload_id],
                        "baseline_case_id": plan_baseline_ids[0] if plan_baseline_ids else None,
                        "baseline_case_ids": plan_baseline_ids,
                        "benchmark": benchmark,
                        "deployment_run_id": None,
                        "evaluation_run_id": None,
                        "configuration_artifact_id": plan.configuration_artifact_id,
                        "benchmark_plan_id": plan.id,
                        "deployment_group_id": f"{plan.id}:guide",
                        "preserve_deployment": plan.preserve_deployment,
                        "deployment_configuration": configuration.model_dump(mode="json"),
                        **scenario_metadata,
                    }
                )
    # Candidate correctness is the primary objective. Run user-selected Guide
    # configurations before optional comparison references so an experimental
    # control cannot prevent the actual Guide from being deployed and measured.
    return sorted(cases, key=lambda case: 0 if case["kind"] == "guide" else 1)


def _ordered_evaluation_cases(cases: list[dict]) -> list[dict]:
    """Run each Guide immediately before its same-pod Kubernetes Service baseline.

    Independently deployed baselines remain after all candidate Guides, preserving the original
    candidate-first failure policy while guaranteeing that a same-pod baseline sees the exact
    execution and cache state produced by its owning candidate.
    """
    ordered: list[dict] = []
    consumed: set[str] = set()
    for guide in (item for item in cases if item["kind"] == "guide"):
        ordered.append(guide)
        consumed.add(guide["id"])
        for baseline in cases:
            if (
                baseline.get("baseline_type") == "kubernetes-service"
                and baseline.get("dependent_guide_case_id") == guide["id"]
            ):
                ordered.append(baseline)
                consumed.add(baseline["id"])
    ordered.extend(item for item in cases if item["id"] not in consumed)
    return ordered


def _embedded_configuration(case: dict) -> DeployableConfiguration | None:
    embedded = case.get("deployment_configuration")
    if not isinstance(embedded, dict):
        return None
    configuration = DeployableConfiguration.model_validate(embedded)
    if configuration.checksum != configuration_checksum(configuration.content):
        raise ValueError(f"stored configuration snapshot for case {case['id']} failed checksum validation")
    return configuration


def _case_configuration(workflow: dict, case: dict) -> DeployableConfiguration:
    embedded = _embedded_configuration(case)
    if embedded is not None:
        return embedded
    artifact_id = case.get("configuration_artifact_id")
    legacy_artifact_id = workflow.get("configuration_artifact_id") if not artifact_id else None
    artifact_id = artifact_id or legacy_artifact_id
    if artifact_id:
        artifact = get_configuration_artifact(artifact_id)
        if artifact is None:
            raise ValueError(
                f"configuration snapshot for case {case['id']} was not retained and artifact {artifact_id} "
                "is no longer available; retry cannot reproduce the original configuration"
            )
        configuration = artifact.deployable_configuration
        content = configuration.content
        runtime_source = "configuration"
        if legacy_artifact_id and not content.get("runtime"):
            spec = case.get("spec", {})
            runtime = workflow.get("runtime", {})
            image = (
                runtime.get("build_image_name")
                if runtime.get("image_mode") == "build-from-source"
                else runtime.get("image")
            )
            content = {
                **content,
                "runtime": {
                    "image": image,
                    "imageMode": runtime.get("image_mode"),
                    "buildSourceUrl": runtime.get("build_source_url", ""),
                    "mountPath": spec.get("mount_path", ""),
                },
            }
            runtime_source = "legacy-evaluation-overlay"
        return configuration.model_copy(
            update={
                "content": content,
                "checksum": configuration_checksum(content),
                "provenance": {
                    **configuration.provenance,
                    "configuration_artifact_id": artifact.artifact_id,
                    "evaluation_id": workflow["id"],
                    "evaluation_case_id": case["id"],
                    "runtime_source": runtime_source,
                },
            }
        )
    # Compatibility path for workflow records created before artifact-first evaluations.
    spec = case["spec"]
    runtime = workflow["runtime"]
    image = runtime["build_image_name"] if runtime["image_mode"] == "build-from-source" else runtime["image"]
    legacy_deployment_runtime = {
        "image": image,
        "imageMode": runtime["image_mode"],
        "buildSourceUrl": runtime["build_source_url"],
        "mountPath": spec["mount_path"],
    }
    provider = "baseline-vllm" if case["kind"] == "baseline" else spec["guide"]
    decode = {
        "replicaCount": spec.get("decode_replicas") or spec.get("replicas"),
        "tensorParallelSize": spec.get("decode_tensor_parallel_size") or spec.get("tensor_parallel_size"),
    }
    content = {
        "model": {"name": spec["model"]},
        "decode": decode,
        "runtime": legacy_deployment_runtime,
        "customParameters": spec.get("custom_parameters", []),
        "guideVariant": spec.get("guide_variant") or None,
    }
    if spec["guide"] == "pd-disaggregation":
        content["prefill"] = {
            "replicaCount": spec["prefill_replicas"],
            "tensorParallelSize": spec["prefill_tensor_parallel_size"],
        }
    return DeployableConfiguration(
        type=provider,
        provider_ref=provider,
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
        provenance={"evaluation_id": workflow["id"], "evaluation_case_id": case["id"]},
    )


def _should_cleanup_deployment(*, owns_deployment: bool, benchmark_succeeded: bool, case: dict) -> bool:
    return owns_deployment and (not benchmark_succeeded or not bool(case.get("preserve_deployment", False)))


def _share_suite_deployment(workflow: dict, case: dict, deployment_run_id: str) -> None:
    """Link later scenarios in the same suite arm to one reusable deployment."""
    group_id = case.get("deployment_group_id")
    if not group_id:
        return
    for sibling in workflow.get("cases", []):
        if sibling.get("deployment_group_id") == group_id and sibling.get("status") == "queued":
            sibling["deployment_run_id"] = deployment_run_id


def _has_pending_suite_scenario(workflow: dict, case: dict) -> bool:
    group_id = case.get("deployment_group_id")
    return bool(group_id) and any(
        sibling["id"] != case["id"]
        and sibling.get("deployment_group_id") == group_id
        and sibling.get("status") not in _TERMINAL_EVALUATE_STATUSES
        for sibling in workflow.get("cases", [])
    )


def _runtime_binding(workflow: dict) -> RuntimeBinding:
    runtime = workflow["runtime"]
    # Prefer the target cluster's saved proxy configuration; per-run values
    # override it. resolve_proxy_env falls back to this backend's environment
    # for auto/unknown clusters.
    cluster_proxy: dict[str, str] = {}
    try:
        from llm_d_bench.cluster import require_active_session
        from llm_d_bench.cluster.service import resolve_proxy_env

        cluster_proxy = resolve_proxy_env(require_active_session(workflow["cluster_session_id"]).server_id)
    except Exception:
        cluster_proxy = {}
    environment = {
        **cluster_proxy,
        **{
            key: value
            for key, value in {
                "HTTP_PROXY": runtime.get("http_proxy", ""),
                "HTTPS_PROXY": runtime.get("https_proxy", ""),
                "NO_PROXY": runtime.get("no_proxy", ""),
            }.items()
            if value
        },
    }
    return RuntimeBinding(cluster_session_id=workflow["cluster_session_id"], environment=environment)


def _rebind_retry_session(workflow: dict) -> None:
    """Bind Retry runtime to the current session for the original target cluster."""
    session_id = workflow.get("cluster_session_id")
    if not session_id:
        return
    try:
        deployment_runtime_overrides(session_id)
        return
    except ValueError as error:
        if str(error) != "cluster session is no longer active":
            raise
    cluster_ids = set()
    for case in workflow.get("cases", []):
        configuration = _case_configuration(workflow, case)
        cluster_ref = configuration.provenance.get("cluster_ref")
        if isinstance(cluster_ref, dict) and cluster_ref.get("id"):
            cluster_ids.add(str(cluster_ref["id"]))
            continue
        official = configuration.content.get("officialGuide")
        cluster = official.get("cluster") if isinstance(official, dict) else None
        if isinstance(cluster, dict) and cluster.get("id"):
            cluster_ids.add(str(cluster["id"]))
    if len(cluster_ids) != 1:
        raise ValueError(
            "the original target cluster cannot be determined from this evaluation; "
            "create a new evaluation on the ready cluster"
        )
    cluster_id = next(iter(cluster_ids))
    session = sessions.get_session_for_server(cluster_id)
    if session is None:
        raise ValueError(f"cluster {cluster_id} has no active session; reconnect that cluster before retrying")
    deployment_runtime_overrides(session.id)
    workflow["cluster_session_id"] = session.id


def _benchmark_specification(case: dict) -> str:
    configured = os.environ.get("LLM_D_BENCH_SPECIFICATION_FILE")
    if configured:
        return configured
    if case["kind"] == "baseline":
        return "guides/optimized-baseline"
    embedded = _embedded_configuration(case)
    if embedded is not None:
        return f"guides/{embedded.provider_ref}"
    artifact = get_configuration_artifact(case.get("configuration_artifact_id", ""))
    if artifact is not None:
        return f"guides/{artifact.deployable_configuration.provider_ref}"
    return f"guides/{case['spec']['guide']}"


def _configuration_facts(case: dict) -> dict:
    configuration = _embedded_configuration(case)
    artifact_id = case.get("configuration_artifact_id")
    if configuration is None:
        artifact = get_configuration_artifact(artifact_id or "")
        configuration = artifact.deployable_configuration if artifact is not None else None
        artifact_id = artifact.artifact_id if artifact is not None else artifact_id
    if configuration is None:
        return case.get("spec", {})
    content = configuration.content
    decode = content.get("decode") or content.get("serving") or {}
    prefill = content.get("prefill") or {}
    runtime = content.get("runtime") or {}
    guide_source = configuration.provenance.get("guide_source") or {}
    return {
        "configuration_artifact_id": artifact_id,
        "model": (content.get("model") or {}).get("name"),
        "guide": configuration.provider_ref,
        "guide_variant": content.get("guideVariant"),
        "runtime": runtime.get("modelServer") or guide_source.get("modelServer") or guide_source.get("model_server"),
        "accelerator": guide_source.get("accelerator"),
        "image": runtime.get("image"),
        "replicas": decode.get("replicaCount"),
        "tensor_parallel_size": decode.get("tensorParallelSize"),
        "decode_replicas": decode.get("replicaCount"),
        "decode_tensor_parallel_size": decode.get("tensorParallelSize"),
        "prefill_replicas": prefill.get("replicaCount"),
        "prefill_tensor_parallel_size": prefill.get("tensorParallelSize"),
        "benchmark": case.get("benchmark") or case.get("spec", {}).get("benchmark", {}),
    }


def _accelerator_count(configuration: dict) -> int | None:
    if configuration.get("prefill_replicas") is not None:
        values = (
            configuration.get("prefill_replicas"),
            configuration.get("prefill_tensor_parallel_size"),
            configuration.get("decode_replicas"),
            configuration.get("decode_tensor_parallel_size"),
        )
        if all(isinstance(value, int) for value in values):
            return values[0] * values[1] + values[2] * values[3]
        return None
    replicas = configuration.get("replicas")
    tp = configuration.get("tensor_parallel_size")
    return replicas * tp if isinstance(replicas, int) and isinstance(tp, int) else None


def _comparison_parity(guide: dict, baseline: dict) -> dict:
    checks = {
        "model": bool(guide.get("model") and guide.get("model") == baseline.get("model")),
        "runtime": bool(guide.get("runtime") and guide.get("runtime") == baseline.get("runtime")),
        "accelerator_type": bool(guide.get("accelerator") and guide.get("accelerator") == baseline.get("accelerator")),
        "accelerator_count": _accelerator_count(guide) is not None
        and _accelerator_count(guide) == _accelerator_count(baseline),
        "workload": bool(guide.get("benchmark") and guide.get("benchmark") == baseline.get("benchmark")),
    }
    return {
        "valid": all(checks.values()),
        "checks": checks,
        "guide_accelerators": _accelerator_count(guide),
        "baseline_accelerators": _accelerator_count(baseline),
    }


def _sla_evaluation(metrics: dict, targets: dict) -> dict:
    """Evaluate optional scenario targets without changing benchmark execution status."""
    checks: dict[str, dict] = {}
    distributions = metrics.get("latency_distributions") or {}
    for metric, distribution in (("ttft", "ttft"), ("tpot", "tpot")):
        threshold = targets.get(f"{metric}_ms")
        if threshold is None:
            continue
        percentile = targets.get(f"{metric}_percentile", "p99")
        actual = (distributions.get(distribution) or {}).get(f"{percentile}_ms")
        checks[metric] = {
            "actual": actual,
            "target": threshold,
            "unit": "ms",
            "percentile": percentile,
            "met": actual <= threshold if isinstance(actual, (int, float)) else None,
        }
    for name, metric_key, target_key, higher_is_better, unit in (
        ("throughput", "throughput_tps", "throughput_min_tps", True, "tok/s"),
        ("success_rate", "success_rate", "success_rate_min_percent", True, "%"),
    ):
        threshold = targets.get(target_key)
        if threshold is None:
            continue
        actual = metrics.get(metric_key)
        checks[name] = {
            "actual": actual,
            "target": threshold,
            "unit": unit,
            "met": actual >= threshold if higher_is_better and isinstance(actual, (int, float)) else None,
        }
    outcomes = [item["met"] for item in checks.values()]
    met = False if False in outcomes else True if outcomes and all(value is True for value in outcomes) else None
    return {"met": met, "checks": checks}


def _apply_capacity_metrics(record: dict) -> bool:
    """Derive stable capacity and the first SLO/saturation boundary from a rate sweep."""
    stages = record.get("rate_stage_results") or []
    metrics = record.get("metrics")
    if not stages or not isinstance(metrics, dict):
        return False
    if not any(
        isinstance((stage.get("metrics") or {}).get(key), (int, float))
        for stage in stages
        for key in ("throughput_rps", "success_rate")
    ):
        return False
    targets = record.get("sla_targets") or {}
    rows = []
    for stage in sorted(stages, key=lambda item: item.get("rate") or 0):
        rate = stage.get("rate")
        stage_metrics = stage.get("metrics") or {}
        achieved = stage_metrics.get("throughput_rps")
        success = stage_metrics.get("success_rate")
        target_success = targets.get("success_rate_min_percent")
        if target_success is None:
            target_success = 99.0
        sla = _sla_evaluation(stage_metrics, {**targets, "success_rate_min_percent": target_success})
        load_met = (
            achieved >= rate * 0.95 if isinstance(rate, (int, float)) and isinstance(achieved, (int, float)) else None
        )
        stable = sla["met"] is True and load_met is True
        # A stage without either achieved request rate or success rate cannot prove stability.
        if achieved is None and success is None:
            stable = False
        rows.append(
            {
                "offered_rate_rps": rate,
                "achieved_rate_rps": achieved,
                "success_rate": success,
                "stable": stable,
                "load_met": load_met,
                "sla": sla,
            }
        )
    stable_rows = [row for row in rows if row["stable"] and isinstance(row["offered_rate_rps"], (int, float))]
    violation = next((row["offered_rate_rps"] for row in rows if not row["stable"]), None)
    saturation = next((row["offered_rate_rps"] for row in rows if row["load_met"] is False), None)
    maximum_stable = max((row["offered_rate_rps"] for row in stable_rows), default=None)
    goodput = max(
        (row["achieved_rate_rps"] for row in stable_rows if isinstance(row["achieved_rate_rps"], (int, float))),
        default=None,
    )
    analysis = {
        "method": (
            "highest offered rate meeting configured SLOs, >=99% success by default, and >=95% offered-rate attainment"
        ),
        "maximum_stable_qps": maximum_stable,
        "slo_goodput_rps": goodput,
        "slo_violation_point": violation,
        "saturation_point": saturation,
        "rows": rows,
    }
    changed = metrics.get("capacity_analysis") != analysis
    metrics["capacity_analysis"] = analysis
    for key, value in {
        "maximum_stable_qps": maximum_stable,
        "slo_goodput_rps": goodput,
        "slo_violation_point": violation,
        "saturation_point": saturation,
    }.items():
        if value is not None:
            metrics[key] = value
    return changed


def _attach_rate_stage_observability(record: dict, observability: dict) -> None:
    """Align Prometheus samples to sequential open-loop stages and persist stage summaries."""
    stages = record.get("rate_stage_results") or []
    series = observability.get("series") or []
    if not stages or not series:
        return
    total_duration = sum(float(stage.get("duration") or 0) for stage in stages)
    try:
        window_end = datetime.fromisoformat(str(observability["window"]["end"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return
    traffic_start = window_end - timedelta(seconds=total_duration)
    cursor = traffic_start
    for stage in stages:
        stage_end = cursor + timedelta(seconds=float(stage.get("duration") or 0))
        samples = []
        for point in series:
            try:
                timestamp = datetime.fromisoformat(str(point.get("timestamp")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if cursor <= timestamp <= stage_end:
                samples.append(point)
        summary = {}
        metric_names = sorted({key for point in samples for key in point if key != "timestamp"})
        for metric_name in metric_names:
            values = sorted(
                float(point[metric_name]) for point in samples if isinstance(point.get(metric_name), (int, float))
            )
            if not values:
                continue

            def pick(fraction: float, metric_values: list[float] = values) -> float:
                index = min(len(metric_values) - 1, max(0, math.ceil(len(metric_values) * fraction) - 1))
                return metric_values[index]

            summary[metric_name] = {
                "mean": sum(values) / len(values),
                "p50": pick(0.50),
                "p95": pick(0.95),
                "p99": pick(0.99),
                "max": values[-1],
            }
        stage["observability"] = {
            "window": {"start": cursor.isoformat(), "end": stage_end.isoformat()},
            "summary": summary,
        }
        stage_metrics = stage.setdefault("metrics", {})
        for source, target in (
            ("queue_depth", "queue_depth"),
            ("running_requests", "running_requests"),
            ("prefix_cache_hit_percent", "prefix_cache_hit_rate"),
            ("epp_inflight_requests", "epp_inflight_requests"),
        ):
            if isinstance((summary.get(source) or {}).get("mean"), (int, float)):
                stage_metrics[target] = summary[source]["mean"]
        cursor = stage_end


def _apply_guide_mechanism_metrics(record: dict) -> bool:
    metrics = record.get("metrics")
    if not isinstance(metrics, dict):
        return False
    mechanism = dict(metrics.get("mechanism_metrics") or {})
    configuration = _embedded_configuration(record)
    content = (
        configuration.content
        if configuration is not None
        else (record.get("configuration") or record.get("spec") or {})
    )
    guide = configuration.provider_ref if configuration is not None else content.get("guide")
    decode = content.get("decode") or content.get("serving") or {}
    prefill = content.get("prefill") or {}
    if guide == "pd-disaggregation" or prefill:
        prefill_replicas = prefill.get("replicaCount")
        prefill_tp = prefill.get("tensorParallelSize")
        decode_replicas = decode.get("replicaCount")
        decode_tp = decode.get("tensorParallelSize")
        mechanism["prefill_pool"] = {"replicas": prefill_replicas, "tensor_parallel_size": prefill_tp}
        mechanism["decode_pool"] = {"replicas": decode_replicas, "tensor_parallel_size": decode_tp}
        mechanism["pd_topology"] = (
            f"{prefill_replicas or '—'}P×TP{prefill_tp or '—'} / {decode_replicas or '—'}D×TP{decode_tp or '—'}"
        )
        if all(isinstance(value, int) for value in (prefill_replicas, prefill_tp, decode_replicas, decode_tp)):
            prefill_accelerators = prefill_replicas * prefill_tp
            decode_accelerators = decode_replicas * decode_tp
            mechanism["pd_accelerator_ratio"] = (
                round(prefill_accelerators / decode_accelerators, 4) if decode_accelerators else None
            )
        source = (content.get("officialGuide") or {}).get("source") or {}
        model_server = str(source.get("modelServer") or source.get("model_server") or "")
        mechanism["rdma_tcp_ucx_path"] = (
            "NIXL / UCX (configured)" if "rdma" in model_server.lower() else "TCP / runtime default (configured)"
        )
    variant = content.get("guideVariant") or ((content.get("officialGuide") or {}).get("source") or {}).get("variant")
    if guide == "tiered-prefix-cache" or variant:
        mechanism["cache_topology"] = variant or "HBM-only"
        mechanism["offload_enabled_disabled"] = "enabled" if variant and variant != "base" else "disabled"
        mechanism["connector_backend"] = (
            "LMCache" if "lmcache" in str(variant).lower() else "Native" if variant and variant != "base" else "None"
        )
    custom_parameters = content.get("customParameters") or []
    parameter_values = {
        str(item.get("name")): item.get("value") for item in custom_parameters if isinstance(item, dict)
    }
    block_size = next((item.get("value") for item in custom_parameters if item.get("name") == "block-size"), None)
    if block_size is not None:
        mechanism["block_size"] = block_size
    for target, names, unit in (
        ("cpu_cache_capacity", ("cpu-offload-gb", "cpu-cache-gb", "kv-cache-cpu-gb"), "GiB configured"),
        ("filesystem_capacity", ("filesystem-cache-gb", "fs-cache-gb", "kv-cache-fs-gb"), "GiB configured"),
    ):
        configured = next(
            (parameter_values[name] for name in names if parameter_values.get(name) not in (None, "")), None
        )
        if configured is not None:
            with contextlib.suppress(TypeError, ValueError):
                configured = float(configured)
            mechanism[target] = {"value": configured, "unit": unit, "status": "configured"}
    router_config = content.get("router") or {}
    if router_config.get("peakPrefillThroughput") is not None:
        mechanism["calibration"] = {
            "peak_prefill_throughput": router_config["peakPrefillThroughput"],
            "status": "configured",
        }
    benchmark = record.get("benchmark") or (record.get("spec") or {}).get("benchmark") or {}
    shared_prefix = benchmark.get("shared_prefix") or {}
    if shared_prefix:
        groups = shared_prefix.get("num_groups")
        prompt_length = shared_prefix.get("system_prompt_len")
        if isinstance(groups, int) and isinstance(prompt_length, int):
            mechanism["working_set"] = {
                "value": groups * prompt_length,
                "unit": "unique prompt tokens",
                "status": "derived from workload",
            }
    observability = metrics.get("observability") or {}
    observed_capacity = observability.get("cache_config") or {}
    hbm_capacity = observed_capacity.get("hbm_capacity_tokens")
    effective_capacity = observed_capacity.get("effective_capacity_tokens")
    if isinstance(hbm_capacity, (int, float)):
        mechanism["hbm_capacity"] = {"value": hbm_capacity, "unit": "tokens", "status": "runtime reported"}
    if isinstance(effective_capacity, (int, float)):
        mechanism["effective_cache_capacity"] = {
            "value": effective_capacity,
            "unit": "tokens",
            "status": "runtime reported",
        }
    working_set = mechanism.get("working_set") or {}
    working_set_tokens = working_set.get("value") if isinstance(working_set, dict) else None
    if isinstance(working_set_tokens, (int, float)) and isinstance(hbm_capacity, (int, float)) and hbm_capacity > 0:
        mechanism["working_set_hbm_ratio"] = round(working_set_tokens / hbm_capacity, 4)
    if (
        isinstance(working_set_tokens, (int, float))
        and isinstance(effective_capacity, (int, float))
        and effective_capacity > 0
    ):
        mechanism["working_set_effective_capacity_ratio"] = round(working_set_tokens / effective_capacity, 4)
    rate_stages = record.get("rate_stage_results") or []
    distributions = metrics.get("latency_distributions") or {}
    itl = distributions.get("itl") or {}
    tpot = distributions.get("tpot") or {}
    latency_tail = {
        key: value
        for key, value in {
            "itl_p95_ms": itl.get("p95_ms"),
            "itl_p99_ms": itl.get("p99_ms"),
            "tpot_p95_ms": tpot.get("p95_ms"),
            "tpot_p99_ms": tpot.get("p99_ms"),
        }.items()
        if isinstance(value, (int, float))
    }
    if latency_tail:
        mechanism["itl_tpot_p95_p99"] = latency_tail
    itl_p95 = []
    for stage in rate_stages:
        distribution = ((stage.get("metrics") or {}).get("latency_distributions") or {}).get("itl") or {}
        if isinstance(distribution.get("p95_ms"), (int, float)):
            itl_p95.append(distribution["p95_ms"])
    if len(itl_p95) >= 2 and itl_p95[0]:
        mechanism["decode_interference_penalty"] = round((max(itl_p95[1:]) - itl_p95[0]) / itl_p95[0] * 100, 3)
    contract = dict(metrics.get("evidence_contract") or {})
    units = {
        "decode_interference_penalty": "%",
        "pd_accelerator_ratio": "ratio",
        "working_set": "unique prompt tokens",
        "kv_transfer_success_rate": "%",
        "transfer_bandwidth": "bytes/s",
        "offloaded_bytes": "bytes",
        "restored_bytes": "bytes",
        "offload_bandwidth": "bytes/s",
        "restore_bandwidth": "bytes/s",
        "hbm_hit_rate": "%",
        "lower_tier_hit_rate": "%",
        "miss_rate": "%",
        "hbm_cache_utilization": "%",
        "cpu_cache_utilization": "%",
        "kv_event_subscriber_coverage": "%",
        "index_lookup_latency": "ms p95",
        "working_set_hbm_ratio": "ratio",
        "working_set_effective_capacity_ratio": "ratio",
    }
    for key, value in mechanism.items():
        if key in contract or value in (None, [], {}):
            continue
        status = value.get("status", "derived") if isinstance(value, dict) else "derived"
        contract[key] = {
            "status": status,
            "value": value,
            **({"unit": units[key]} if key in units and not isinstance(value, dict) else {}),
            "source": "benchmark configuration and persisted benchmark-window measurements",
        }
    changed = metrics.get("mechanism_metrics") != mechanism or metrics.get("evidence_contract") != contract
    if mechanism:
        metrics["mechanism_metrics"] = mechanism
    if contract:
        metrics["evidence_contract"] = contract
    return changed


def _apply_diagnosis(record: dict) -> bool:
    metrics = record.get("metrics")
    if not isinstance(metrics, dict):
        return False
    mechanism = metrics.get("mechanism_metrics") or {}
    observability = metrics.get("observability") or {}
    summary = observability.get("summary") or {}
    capacity = metrics.get("capacity_analysis") or {}
    diagnoses = []

    def add(category, severity, diagnosis_summary, evidence, possible_cause, recommended_action):
        diagnoses.append(
            {
                "category": category,
                "severity": severity,
                "diagnosis_summary": diagnosis_summary,
                "evidence": evidence,
                "possible_cause": possible_cause,
                "recommended_action": recommended_action,
            }
        )

    if observability and observability.get("status") != "available":
        add(
            "Data Availability",
            "info",
            "Benchmark-window system metrics are incomplete.",
            observability.get("reason") or "No Prometheus samples",
            "Monitoring was unavailable, warming, or the runtime did not export compatible metrics.",
            "Verify ServiceMonitor targets before rerunning the benchmark if these signals are required.",
        )
    availability = (observability.get("availability") or {}).get("metrics") or {}
    embedded = _embedded_configuration(record)
    guide = (
        observability.get("guide_type")
        or (embedded.provider_ref if embedded is not None else None)
        or record.get("guide")
        or (record.get("spec") or {}).get("guide")
    )
    required_by_guide = {
        "optimized-baseline": ("prefix_cache_hit_percent", "per_endpoint.inflight_token_load"),
        "pd-disaggregation": ("router.pd_decision_rate_rps", "running_requests"),
        "precise-prefix-cache-routing": ("router.index_lookups_per_second", "router.index_lookup_latency_p95_ms"),
        "tiered-prefix-cache": ("external_prefix_cache_hit_percent", "per_pod.memory_working_set_bytes"),
    }
    missing_required = [key for key in required_by_guide.get(guide, ()) if availability.get(key) != "available"]
    if missing_required and not any(item["category"] == "Data Availability" for item in diagnoses):
        add(
            "Data Availability",
            "info",
            "One or more guide-critical runtime signals were not exported during the benchmark.",
            {"missing_metrics": missing_required},
            "The deployed Router, runtime, or monitoring stack does not expose a compatible series "
            "for this configuration.",
            "Check the metric availability map and enable the corresponding exporter before the next run.",
        )
    if capacity.get("slo_violation_point") is not None:
        add(
            "Saturation",
            "warning",
            f"The first unstable stage occurred at {capacity['slo_violation_point']} req/s.",
            capacity.get("rows"),
            "Offered load exceeded an SLO or the endpoint stopped attaining at least 95% of offered rate.",
            "Use the maximum stable rate as the operating bound and inspect queue/resource evidence at the next stage.",
        )
    queue_p99 = (summary.get("queue_depth") or {}).get("p99")
    if isinstance(queue_p99, (int, float)) and queue_p99 > 0:
        add(
            "Queue Growth",
            "warning",
            "Requests queued during the benchmark window.",
            {"queue_depth_p99": queue_p99},
            "Serving or routing capacity did not keep up with arrivals.",
            "Compare the first queueing stage with accelerator, KV-cache, and per-pod load.",
        )
    if isinstance(metrics.get("error_rate"), (int, float)) and metrics["error_rate"] > 0:
        add(
            "Timeout / Failure",
            "critical" if metrics["error_rate"] >= 5 else "warning",
            "The benchmark recorded failed requests.",
            {"error_rate_percent": metrics["error_rate"], "failure_count": metrics.get("failure_count")},
            "Timeouts, backend errors, overload, or transfer failure may have occurred.",
            "Inspect benchmark, Router, model-server, and Kubernetes event logs before comparing throughput.",
        )
    cache_hit = metrics.get("prefix_cache_hit_rate")
    load_cv = metrics.get("token_load_cv")
    if isinstance(cache_hit, (int, float)) and cache_hit < 20:
        add(
            "Low Cache Locality",
            "warning",
            "Prefix cache locality remained low.",
            {"prefix_cache_hit_rate": cache_hit},
            "The workload has little prefix reuse or affinity routing did not preserve locality.",
            "Validate the shared-prefix geometry and Router affinity plugin configuration.",
        )
    if isinstance(load_cv, (int, float)) and load_cv > 0.25:
        add(
            "Poor Token Load Balance",
            "warning",
            "Per-endpoint token work is imbalanced.",
            {"token_load_cv": load_cv},
            "Affinity may be too sticky or the token-load scorer may not see current endpoint load.",
            "Inspect per-endpoint in-flight tokens and affinity relaxation behavior.",
        )
    pd_ratio = mechanism.get("pd_decision_ratio")
    if isinstance(pd_ratio, (int, float)) and pd_ratio < 95:
        add(
            "Prefill Interference Not Isolated",
            "warning",
            "Not all requests followed the intended P/D decision path.",
            {"pd_decision_ratio_percent": pd_ratio},
            "The disaggregation decision plugin may be falling back or misconfigured.",
            "Validate the P/D decider, endpoint discovery, and KV connector readiness.",
        )
    penalty = mechanism.get("decode_interference_penalty")
    if isinstance(penalty, (int, float)) and penalty > 20:
        add(
            "Decode-bound",
            "warning",
            "Decode ITL degraded materially as offered load increased.",
            {"decode_interference_penalty_percent": penalty},
            "Decode capacity or KV pressure may be limiting isolation.",
            "Increase Decode share or reduce Prefill interference, then rerun the same rate sweep.",
        )
    if (
        guide == "pd-disaggregation"
        and "rdma" in str(mechanism.get("rdma_tcp_ucx_path", "")).lower()
        and mechanism.get("kv_transfer_success_rate") is None
    ):
        add(
            "Transfer-bound",
            "warning",
            "The configured RDMA/NIXL path did not expose transfer completion evidence.",
            {"configured_path": mechanism.get("rdma_tcp_ucx_path")},
            "NIXL metrics may be disabled, unscripted, or no KV handoff occurred.",
            "Verify vLLM NIXL Prometheus metrics and exercise at least one successful prefill-to-decode handoff.",
        )
    if guide == "precise-prefix-cache-routing":
        lookups = mechanism.get("index_lookups")
        if not isinstance(lookups, (int, float)) or lookups <= 0:
            add(
                "Precise Index Unhealthy",
                "warning",
                "No precise KV-index lookup activity was observed.",
                {"index_lookups_per_second": lookups},
                "The precise scorer may be disabled, metrics may be disabled, or requests bypassed EPP.",
                "Enable KV index metrics and verify that benchmark traffic uses the precise Router endpoint.",
            )
        event_errors = mechanism.get("kv_event_errors")
        if isinstance(event_errors, (int, float)) and event_errors > 0:
            add(
                "Stale Index / Recovery Failure",
                "critical",
                "KV-event processing errors were observed.",
                {"kv_event_error_rate_rps": event_errors},
                "The ZMQ event stream or subscriber is unhealthy.",
                "Inspect EPP KV-event logs and subscriber connectivity before interpreting precise-routing gains.",
            )
    if guide == "tiered-prefix-cache":
        if mechanism.get("offload_enabled_disabled") == "enabled" and not (
            isinstance(mechanism.get("offloaded_bytes"), (int, float)) and mechanism["offloaded_bytes"] > 0
        ):
            add(
                "CPU Offload Not Observed",
                "warning",
                "The tiered configuration was enabled but no offload traffic was measured.",
                {
                    "offloaded_bytes": mechanism.get("offloaded_bytes"),
                    "cache_topology": mechanism.get("cache_topology"),
                },
                "The workload may not exceed HBM capacity or the connector metric is unavailable.",
                "Increase cache pressure and verify vLLM kv_offload_total_bytes is exported with transfer_type labels.",
            )
        effective_hit = mechanism.get("effective_prefix_hit_rate")
        if isinstance(effective_hit, (int, float)) and effective_hit < 20:
            add(
                "Low Effective Cache Hit",
                "warning",
                "Effective prefix-cache hit rate remained low.",
                {"effective_prefix_hit_rate": effective_hit},
                "The lower tier may not retain the workload's reuse set or restore latency may prevent reuse.",
                "Inspect offload/restore traffic and rerun with repeated prefixes after warmup.",
            )
    if not diagnoses and "diagnoses" not in metrics:
        return False
    changed = metrics.get("diagnoses") != diagnoses
    metrics["diagnoses"] = diagnoses
    return changed


def _ready_deployment_case(workflow_id: str, evaluation_case: dict):
    # Evaluation-created runs already have a durable direct link. Older runs
    # may not carry evaluation IDs in each deployment request's provenance.
    candidate_run_ids = [evaluation_case["deployment_run_id"]] if evaluation_case.get("deployment_run_id") else []
    for run in sorted(_store.list_runs(), key=lambda item: item.created_at, reverse=True):
        if any(
            item.create_request.provenance.get("evaluation_id") == workflow_id
            and item.create_request.provenance.get("evaluation_case_id") == evaluation_case["id"]
            for item in run.cases
        ):
            candidate_run_ids.append(run.id)
    for run_id in candidate_run_ids:
        if not run_id:
            continue
        deployment = _store.get_run(run_id)
        if deployment is None:
            continue
        ready_cases = [
            item
            for item in deployment.cases
            if item.status.value == "ready"
            and item.execution_id
            and (execution := _store.get_execution(item.execution_id)) is not None
            and execution.status == DeploymentStatus.READY
        ]
        ready_case = max(ready_cases, key=lambda item: item.attempt, default=None)
        if ready_case is not None:
            return deployment, ready_case
    return None, None


def _restore_deployment_links(workflow: dict) -> bool:
    """Reconnect Guide deployments that were started outside the workflow task."""
    changed = False
    for evaluation_case in workflow.get("cases", []):
        matching_runs = [
            run
            for run in _store.list_runs()
            if any(
                case.create_request.provenance.get("evaluation_id") == workflow["id"]
                and case.create_request.provenance.get("evaluation_case_id") == evaluation_case["id"]
                for case in run.cases
            )
        ]
        if not matching_runs:
            continue
        deployment = max(matching_runs, key=lambda run: run.created_at)
        matching_cases = [
            case
            for case in deployment.cases
            if case.create_request.provenance.get("evaluation_id") == workflow["id"]
            and case.create_request.provenance.get("evaluation_case_id") == evaluation_case["id"]
        ]
        latest_case = max(matching_cases, key=lambda case: case.attempt, default=None)
        if latest_case is None:
            continue
        updates = {
            "deployment_run_id": deployment.id,
            "deployment_case_id": latest_case.id,
            "execution_id": latest_case.execution_id,
        }
        if evaluation_case.get("status") in {"queued", "deploying", "failed"}:
            updates["status"] = latest_case.status.value
        if any(evaluation_case.get(key) != value for key, value in updates.items()):
            evaluation_case.update(updates)
            changed = True
    return changed


async def _wait_for_deployment(run_id: str) -> tuple[object, object]:
    while True:
        deployment = _store.get_run(run_id)
        if deployment is None:
            raise ValueError("deployment run is no longer available")
        ready = next((item for item in deployment.cases if item.status.value == "ready" and item.execution_id), None)
        if ready:
            return deployment, ready
        # Another API process may reconcile the shared store before manifest
        # creation/readiness has finished. Let the owning worker settle first.
        if deployment.status.value == "failed" and deployment_run_manager.is_run_active(run_id):
            await asyncio.sleep(3)
            continue
        if deployment.status.value in {"failed", "cancelled", "cleaned"}:
            detail = next(
                (item.failure.detail for item in deployment.cases if item.failure and item.failure.detail), None
            )
            raise ValueError(detail or f"deployment ended with status {deployment.status.value}")
        await asyncio.sleep(3)


def _link_evaluation_benchmark(workflow: dict, case: dict, benchmark: dict) -> None:
    benchmark.update(
        deployment_ownership="evaluation" if _deployment_owned(workflow, case) else "existing-endpoint",
        evaluation_workflow_id=workflow["id"],
        evaluation_case_id=case["id"] if "cases" in workflow else None,
        configuration_artifact_id=case.get("configuration_artifact_id") or workflow.get("configuration_artifact_id"),
    )
    _save(benchmark)


def _enrich_benchmark_result(record: dict) -> bool:
    """One result pipeline for standalone runs and workflow cases, including history."""
    changed = _backfill_metric_summary(record)
    for enrich in (apply_request_evidence, _apply_capacity_metrics, _apply_guide_mechanism_metrics, _apply_diagnosis):
        changed = enrich(record) or changed
    return changed


def _hydrate_benchmark_context(run: dict) -> bool:
    """Retain deployment facts without transferring deployment lifecycle ownership."""
    execution_id = run.get("deployment_execution_id")
    execution = _store.get_execution(execution_id) if execution_id else None
    if execution is None:
        return False
    updates = {}
    artifact_ids = [item.artifact_id for item in getattr(execution, "configuration_artifacts", ())]
    if artifact_ids and not run.get("configuration_artifact_ids"):
        updates["configuration_artifact_ids"] = artifact_ids
    if not run.get("namespace"):
        updates["namespace"] = execution.namespace
    if not run.get("model"):
        # Keep legacy history readable when model metadata is incomplete. A Model
        # Service run must display its published name, matching what _execute()
        # will actually request through the Gateway.
        with contextlib.suppress(ValueError):
            updates["model"] = run.get("model_service_published_name") or _execution_model(execution)
    if not run.get("endpoint") and execution.endpoint:
        updates["endpoint"] = (
            execution.endpoint.baseline_url if run.get("use_baseline_endpoint") else execution.endpoint.url
        )
    if not run.get("deployment_configuration"):
        for deployment in _store.list_runs():
            case = next((item for item in deployment.cases if item.execution_id == execution_id), None)
            if case is not None:
                configuration = deployment.source_configurations[case.source_configuration_ordinal]
                updates.update(
                    deployment_configuration=configuration.model_dump(mode="json"),
                    deployment_run_id=deployment.id,
                    deployment_case_id=case.id,
                )
                break
    # Discovered executions can outlive their DeploymentRun. Their immutable
    # JSON configuration artifacts are still sufficient to recover guide facts.
    if not run.get("deployment_configuration") and "deployment_configuration" not in updates:
        for artifact in execution.configuration_artifacts:
            try:
                content = json.loads(artifact.content or "null")
            except json.JSONDecodeError:
                continue
            if not isinstance(content, dict) or not isinstance(content.get("model"), dict):
                continue
            configuration = DeployableConfiguration(
                type=artifact.provider_ref,
                provider_ref=artifact.provider_ref,
                format="helm",
                content=content,
                checksum=configuration_checksum(content),
            )
            updates["deployment_configuration"] = configuration.model_dump(mode="json")
            break
    context = {**run, **updates}
    if context.get("deployment_configuration"):
        updates["configuration"] = _configuration_facts(context)
        updates["guide"] = updates["configuration"].get("guide")
    changed = any(run.get(key) != value for key, value in updates.items())
    run.update(updates)
    return changed


def _benchmark_response(run: dict) -> dict:
    """Add read-only deployment evidence; leave the original benchmark fields intact."""
    response = dict(run)
    execution_id = run.get("deployment_execution_id")
    execution = _store.get_execution(execution_id) if execution_id else None
    response["deployment_cases"] = (
        [
            {
                "id": run.get("deployment_case_id") or execution_id,
                "execution_id": execution_id,
                "guide": run.get("guide"),
                "namespace": execution.namespace if execution else run.get("namespace"),
                "endpoint": run.get("endpoint_used") or run.get("endpoint"),
                "status": execution.status if execution else "unavailable",
                "execution_status": execution.status if execution else "unavailable",
                "monitoring_setup": execution.monitoring_setup if execution else None,
                "diagnostics": execution.diagnostics if execution else None,
                "resource_snapshot": run.get("resource_snapshot"),
            }
        ]
        if execution_id
        else []
    )
    return response


async def _prepare_benchmark_monitoring(run: dict) -> None:
    """Reuse existing scrapes first; setup is optional and bounded for every entry point."""
    run["monitoring"] = {"status": "preparing", "enabled": False}
    _save(run)
    try:
        async with asyncio.timeout(_MONITORING_PREPARE_TIMEOUT):
            execution_id = run["deployment_execution_id"]
            # A healthy target needs no monitor/RBAC mutation, regardless of owner.
            if await wait_for_deployment_metrics(execution_id, timeout_seconds=0):
                run["monitoring"].update(
                    status="ready", enabled=True, source="existing", message="Using existing Prometheus targets"
                )
            else:
                status = await deployment_monitoring.enable(execution_id)
                run["monitoring"].update(
                    enabled=True,
                    resources={
                        "servicemonitors": status.get("servicemonitors", []),
                        "podmonitors": status.get("podmonitors", []),
                    },
                )
                ready = await wait_for_deployment_metrics(execution_id)
                run["monitoring"].update(
                    status="ready" if ready else "warming",
                    message="Prometheus target is ready"
                    if ready
                    else "Prometheus targets are warming; benchmark will proceed",
                )
    except TimeoutError:
        run["monitoring"].update(
            status="unavailable",
            message=(
                f"Monitoring preparation exceeded {_MONITORING_PREPARE_TIMEOUT:g}s; benchmark will proceed and "
                "query recorded metrics afterwards"
            ),
        )
    except Exception as error:
        run["monitoring"].update(status="unavailable", message=str(error))
    finally:
        _save(run)


async def _wait_for_benchmark(run_id: str) -> dict:
    while True:
        benchmark = _get("benchmark", run_id)
        if benchmark is None:
            raise ValueError("benchmark run is no longer available")
        if benchmark["status"] in _TERMINAL_EVALUATE_STATUSES:
            return benchmark
        await asyncio.sleep(3)


def _comparison_report(workflow: dict) -> dict:
    by_id = {case["id"]: case for case in workflow["cases"]}
    comparisons = []
    for case in workflow["cases"]:
        if case["kind"] != "guide":
            continue
        guide_metrics = case.get("metrics") or {}
        guide_matrix = case.get("matrix_results") or []
        guide_rate_stages = case.get("rate_stage_results") or []
        baseline_case_ids = case.get("baseline_case_ids") or (
            [case["baseline_case_id"]] if case.get("baseline_case_id") else []
        )
        baselines = [by_id[item] for item in baseline_case_ids if item in by_id] or [None]
        for baseline in baselines:
            guide_configuration = _configuration_facts(case)
            baseline_configuration = _configuration_facts(baseline) if baseline else {}
            baseline_metrics = baseline.get("metrics") if baseline else {}
            baseline_matrix = (baseline.get("matrix_results") if baseline else None) or []
            baseline_rate_stages = (baseline.get("rate_stage_results") if baseline else None) or []
            guide_mechanism = guide_metrics.get("mechanism_metrics") or {}
            baseline_mechanism = (baseline_metrics or {}).get("mechanism_metrics") or {}
            comparisons.append(
                {
                    "workload_id": case["workload_ids"][0],
                    "scenario_id": case.get("scenario_id"),
                    "scenario_name": case.get("scenario_name"),
                    "sla": _sla_evaluation(guide_metrics, case.get("sla_targets") or {}),
                    "guide": guide_configuration.get("guide"),
                    "guide_configuration": guide_configuration,
                    "baseline_configuration": baseline_configuration,
                    "guide_case_id": case["id"],
                    "baseline_case_id": baseline.get("id") if baseline else None,
                    "baseline_type": baseline.get("baseline_type", "direct-vllm") if baseline else None,
                    "parity": _comparison_parity(guide_configuration, baseline_configuration)
                    if baseline
                    else {"valid": False, "checks": {}},
                    "guide_metrics": guide_metrics,
                    "baseline_metrics": baseline_metrics or {},
                    "delta_percent": {
                        metric: round(
                            (guide_metrics[metric] - baseline_metrics[metric]) / baseline_metrics[metric] * 100, 2
                        )
                        for metric in (
                            "throughput_tps",
                            "input_throughput_tps",
                            "throughput_rps",
                            "ttft_ms",
                            "itl_ms",
                            "tpot_ms",
                            "request_latency_ms",
                            "error_rate",
                            "maximum_stable_qps",
                            "slo_goodput_rps",
                            "prefix_cache_hit_rate",
                            "token_load_cv",
                        )
                        if isinstance(guide_metrics.get(metric), (int, float))
                        and isinstance((baseline_metrics or {}).get(metric), (int, float))
                        and baseline_metrics[metric]
                    },
                    "mechanism_delta_percent": {
                        metric: round(
                            (guide_mechanism[metric] - baseline_mechanism[metric]) / baseline_mechanism[metric] * 100, 2
                        )
                        for metric in set(guide_mechanism) & set(baseline_mechanism)
                        if isinstance(guide_mechanism.get(metric), (int, float))
                        and isinstance(baseline_mechanism.get(metric), (int, float))
                        and baseline_mechanism[metric]
                    },
                    "matrix_statistics": _matrix_comparison(guide_matrix, baseline_matrix)
                    if guide_matrix and baseline_matrix
                    else None,
                    "rate_stage_statistics": _rate_stage_comparison(guide_rate_stages, baseline_rate_stages)
                    if guide_rate_stages and baseline_rate_stages
                    else None,
                }
            )
    topology_comparisons = []
    if workflow.get("compare_configurations", True):
        guides = [case for case in workflow["cases"] if case.get("kind") == "guide"]
        for left_index, left in enumerate(guides):
            for right in guides[left_index + 1 :]:
                if left.get("scenario_id") != right.get("scenario_id"):
                    continue
                left_facts = _configuration_facts(left)
                right_facts = _configuration_facts(right)
                left_metrics = left.get("metrics") or {}
                right_metrics = right.get("metrics") or {}
                topology_comparisons.append(
                    {
                        "left_case_id": left["id"],
                        "right_case_id": right["id"],
                        "left": left_facts,
                        "right": right_facts,
                        "left_metrics": left_metrics,
                        "right_metrics": right_metrics,
                        "ratio": {
                            metric: round(left_metrics[metric] / right_metrics[metric], 4)
                            for metric in (
                                "throughput_tps",
                                "input_throughput_tps",
                                "throughput_rps",
                                "ttft_ms",
                                "itl_ms",
                                "tpot_ms",
                                "request_latency_ms",
                                "error_rate",
                            )
                            if isinstance(left_metrics.get(metric), (int, float))
                            and isinstance(right_metrics.get(metric), (int, float))
                            and right_metrics[metric]
                        },
                        "matrix_statistics": _matrix_comparison(
                            left.get("matrix_results") or [], right.get("matrix_results") or []
                        )
                        if left.get("matrix_results") and right.get("matrix_results")
                        else None,
                        "rate_stage_statistics": _rate_stage_comparison(
                            left.get("rate_stage_results") or [], right.get("rate_stage_results") or []
                        )
                        if left.get("rate_stage_results") and right.get("rate_stage_results")
                        else None,
                    }
                )
    return {
        "generated_at": _now(),
        "comparison_version": 2,
        "comparisons": comparisons,
        "topology_comparisons": topology_comparisons,
    }


def _comparison_report_needs_refresh(report: dict | None) -> bool:
    """Detect reports created before rate-stage absolutes/config identities were persisted."""
    if not report or report.get("comparison_version") != 2:
        return True
    for comparison in report.get("comparisons") or []:
        if "parity" not in comparison or "mechanism_delta_percent" not in comparison:
            return True
        statistics = comparison.get("rate_stage_statistics") or {}
        if statistics.get("rows") and (
            not comparison.get("guide_configuration")
            or not comparison.get("baseline_configuration")
            or any("guide" not in row or "baseline" not in row for row in statistics["rows"])
        ):
            return True
    return False


async def _run_case_benchmark(workflow, case, execution_id, *, use_baseline_endpoint=False):
    request = case_benchmark_request(
        workflow, case, execution_id, _benchmark_specification(case), use_baseline_endpoint=use_baseline_endpoint
    )
    await execute_case_benchmark(
        workflow,
        case,
        request,
        create_run=create_run,
        link_benchmark=_link_evaluation_benchmark,
        save=_save,
        wait_for_benchmark=_wait_for_benchmark,
        enrich=_enrich_benchmark_result,
        now=_now,
    )


async def _execute_shared_pods_baseline_case(workflow: dict, case: dict) -> None:
    """Benchmark a `kubernetes-service` baseline by reusing its sibling Guide case's already-
    ready deployment execution and hitting the Guide's registered baseline endpoint (a plain
    Kubernetes Service bypassing the routing layer) instead of provisioning a second deployment.

    This only works for Guide providers that expose `endpoint.baseline_url` on readiness
    (currently precise-prefix-cache-routing) -- reusing the same, already-warmed pods is the
    only way to match a Guide's own "vs a stock Kubernetes Service" benchmark methodology; a
    separately-deployed baseline would start from unrelated cache state and cost a second
    deployment's worth of accelerators. Never owns a deployment, so the shared cleanup guard
    (`_should_cleanup_deployment`) naturally leaves the reused deployment alone.
    """
    guide_case = next(
        (
            item
            for item in workflow["cases"]
            if item["id"] == case.get("dependent_guide_case_id") or case["id"] in (item.get("baseline_case_ids") or [])
        ),
        None,
    )
    if guide_case is None or not guide_case.get("execution_id"):
        case.update(
            status="failed",
            error="no ready Guide deployment is available to reuse for this baseline",
            finished_at=_now(),
        )
        _save(workflow)
        return
    execution = _store.get_execution(guide_case["execution_id"])
    if execution is None or execution.status != DeploymentStatus.READY or execution.endpoint is None:
        case.update(status="failed", error="the reused Guide deployment execution is not ready", finished_at=_now())
        _save(workflow)
        return
    if not execution.endpoint.baseline_url:
        case.update(
            status="failed",
            error="this Guide does not expose a kubernetes-service baseline endpoint for comparison",
            finished_at=_now(),
        )
        _save(workflow)
        return
    case.update(
        status="benchmarking",
        started_at=case.get("started_at") or _now(),
        execution_id=guide_case["execution_id"],
        namespace=execution.namespace,
        endpoint=execution.endpoint.baseline_url,
        endpoint_kind="kubernetes-service",
        error=None,
    )
    workflow["active_case_id"] = case["id"]
    _save(workflow)
    try:
        await _run_case_benchmark(workflow, case, guide_case["execution_id"], use_baseline_endpoint=True)
    finally:
        # The candidate deliberately retains ownership until this dependent baseline finishes.
        # Clean exactly once here unless the user explicitly requested preservation.
        deployment_run_id = guide_case.get("deployment_run_id")
        deployment_case_id = guide_case.get("deployment_case_id")
        if (
            not case.get("cancel_requested")
            and not guide_case.get("preserve_deployment")
            and deployment_run_id
            and deployment_case_id
        ):
            try:
                await deployment_run_manager.worker_for_run(deployment_run_id).clean_case(
                    deployment_run_id,
                    deployment_case_id,
                )
            except ValueError as cleanup_error:
                guide_case["cleanup_error"] = str(cleanup_error)
                _save(workflow)


async def _execute_evaluation(workflow_id: str) -> None:
    workflow = _get("workflow", workflow_id)
    if workflow is None:
        return
    current_task = asyncio.current_task()
    registered_task = _workflow_tasks.get(workflow_id)
    if registered_task is not None and registered_task is not current_task and not registered_task.done():
        return
    _active_workflows[workflow_id] = workflow
    workflow["status"] = "running"
    workflow.pop("failed_stage", None)
    for item in workflow["cases"]:
        if item["status"] != "succeeded":
            item.pop("failed_stage", None)
    workflow["started_at"] = workflow.get("started_at") or _now()
    _save(workflow)
    try:
        for case in _ordered_evaluation_cases(workflow["cases"]):
            if case["status"] in {"succeeded", "cancelled"} or case.get("cancel_requested"):
                continue
            workflow["active_case_id"] = case["id"]
            _save(workflow)
            if case["kind"] == "baseline" and case.get("baseline_type") == "kubernetes-service":
                await _execute_shared_pods_baseline_case(workflow, case)
                continue
            deployment = None
            ready_case = None
            owns_deployment = False
            benchmark_succeeded = False
            if case.get("deployment_run_id"):
                deployment = _store.get_run(case["deployment_run_id"])
                if deployment is not None:
                    ready_cases = [
                        item
                        for item in deployment.cases
                        if item.status.value == "ready"
                        and item.execution_id
                        and (execution := _store.get_execution(item.execution_id)) is not None
                        and execution.status == DeploymentStatus.READY
                    ]
                    ready_case = max(ready_cases, key=lambda item: item.attempt, default=None)
                    if ready_case is not None:
                        owns_deployment = True
                        execution = _store.get_execution(ready_case.execution_id)
                        case.update(
                            status="benchmarking",
                            deployment_case_id=ready_case.id,
                            execution_id=ready_case.execution_id,
                            namespace=execution.namespace if execution else None,
                            endpoint=execution.endpoint.url if execution and execution.endpoint else None,
                            endpoint_kind="direct" if ready_case.provider_ref == "baseline-vllm" else "routed",
                            error=None,
                        )
                    elif deployment.status.value not in {"failed", "cancelled", "cleaned"}:
                        owns_deployment = True
                        deployment_run_manager.resume_run(deployment.id)
                        case.update(status="deploying", error=None)
                        _save(workflow)
                        _deployment, ready_case = await _wait_for_deployment(deployment.id)
                        execution = _store.get_execution(ready_case.execution_id)
                        case.update(
                            status="benchmarking",
                            deployment_case_id=ready_case.id,
                            execution_id=ready_case.execution_id,
                            namespace=execution.namespace if execution else None,
                            endpoint=execution.endpoint.url if execution and execution.endpoint else None,
                            endpoint_kind="direct" if ready_case.provider_ref == "baseline-vllm" else "routed",
                        )
            if ready_case is None:
                case.update(status="deploying", started_at=_now(), error=None)
            workflow["active_case_id"] = case["id"]
            _save(workflow)
            try:
                if ready_case is None:
                    configuration = _case_configuration(workflow, case)
                    deployment = await deployment_run_manager.start_run(
                        DeploymentRunCreateRequest(
                            configurations=[configuration],
                            runtime_binding=_runtime_binding(workflow),
                            provenance={
                                "cluster_session_id": workflow["cluster_session_id"],
                                # DeploymentRunManager resolves the actual checkout
                                # from this session's durable cluster record.
                                "deployment_source": {"kind": "evaluation-cluster-source"},
                                "evaluate_workflow": True,
                                "evaluation_id": workflow["id"],
                                "evaluation_case_id": case["id"],
                                "preserve_deployment": bool(case.get("preserve_deployment")),
                            },
                        ),
                        owner_user_id=workflow.get("owner_user_id"),
                    )
                    owns_deployment = True
                    case["deployment_run_id"] = deployment.id
                    _share_suite_deployment(workflow, case, deployment.id)
                    _save(workflow)
                    _deployment, ready_case = await _wait_for_deployment(deployment.id)
                    execution = _store.get_execution(ready_case.execution_id)
                    case.update(
                        status="benchmarking",
                        deployment_case_id=ready_case.id,
                        execution_id=ready_case.execution_id,
                        namespace=execution.namespace if execution else None,
                        endpoint=execution.endpoint.url if execution and execution.endpoint else None,
                        endpoint_kind="direct" if ready_case.provider_ref == "baseline-vllm" else "routed",
                    )
                _save(workflow)
                await _run_case_benchmark(workflow, case, ready_case.execution_id)
                benchmark_succeeded = True
            finally:
                has_same_pod_baseline = case["kind"] == "guide" and any(
                    item.get("baseline_type") == "kubernetes-service"
                    and item.get("dependent_guide_case_id") == case["id"]
                    and item.get("status") != "succeeded"
                    for item in workflow["cases"]
                )
                if (
                    not case.get("cancel_requested")
                    and _should_cleanup_deployment(
                        owns_deployment=owns_deployment,
                        benchmark_succeeded=benchmark_succeeded,
                        case=case,
                    )
                    and not (benchmark_succeeded and has_same_pod_baseline)
                    and not (benchmark_succeeded and _has_pending_suite_scenario(workflow, case))
                    and deployment is not None
                    and ready_case is not None
                ):
                    try:
                        await deployment_run_manager.worker_for_run(deployment.id).clean_case(
                            deployment.id, ready_case.id
                        )
                    except ValueError as cleanup_error:
                        case["cleanup_error"] = str(cleanup_error)
                        _save(workflow)
        workflow.update(
            status="cancelled" if any(case.get("cancel_requested") for case in workflow["cases"]) else "succeeded",
            report=_comparison_report(workflow),
            active_case_id=None,
            finished_at=_now(),
        )
    except asyncio.CancelledError:
        workflow.update(
            status="cancelling" if workflow.get("status") == "cancelling" else "cancelled",
            active_case_id=None,
            finished_at=_now(),
        )
        raise
    except Exception as error:
        active = next((case for case in workflow["cases"] if case["id"] == workflow.get("active_case_id")), None)
        if active and active["status"] not in {"failed", "cancelled"}:
            active.update(failed_stage=active["status"], status="failed", error=str(error), finished_at=_now())
        workflow.update(
            status="failed",
            failed_stage=(active.get("failed_stage") or ("benchmarking" if active.get("evaluation_run_id") else None))
            if active
            else None,
            error=str(error),
            active_case_id=None,
            finished_at=_now(),
        )
    finally:
        _save(workflow)
        _workflow_tasks.pop(workflow_id, None)
        _active_workflows.pop(workflow_id, None)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError("evaluate run id is invalid") from error
    if str(parsed) != value:
        raise ValueError("evaluate run id is invalid")
    return value


def _directory(kind: str) -> Path:
    if kind == "benchmark":
        return _benchmark_directory
    if kind == "workflow":
        return _workflow_directory
    raise ValueError("evaluate record kind is invalid")


def _path(kind: str, run_id: str) -> Path:
    path = (_directory(kind) / f"{_identifier(run_id)}.json").resolve()
    if path.parent != _directory(kind):
        raise ValueError("evaluate run path is invalid")
    return path


def _save(record: dict) -> None:
    kind = record["kind"]
    record_id = _identifier(record["id"])
    if kind == "benchmark" and record.get("status") in _TERMINAL_EVALUATE_STATUSES:
        _register_evaluation_artifacts(record)
    payload = dict(record, id=record_id)
    if kind == "benchmark":
        _benchmark_records.save(EvaluateRunRecord.model_validate(payload))
        return
    if kind == "workflow":
        _workflow_records.save(EvaluateWorkflowRecord.model_validate(payload))
        return
    raise ValueError("evaluate record kind is invalid")


def _evaluation_output(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ValueError("evaluation artifact id is invalid")
    output = _results_root / run_id
    if output.is_symlink() or not output.resolve().is_relative_to(_results_root.resolve()):
        raise ValueError("evaluation artifact directory is outside its root or a symlink")
    return output


def _register_evaluation_artifacts(record: dict) -> None:
    """Publish native reports and a result snapshot before the metadata reference."""
    output = _evaluation_output(record["id"])
    output.mkdir(parents=True, exist_ok=True)
    snapshot = {key: value for key, value in record.items() if key not in {"artifact_manifest", "artifact_ref"}}
    temporary = output / f".evaluation-record.{uuid4()}.tmp"
    temporary.write_text(json.dumps(snapshot, ensure_ascii=True, indent=2), encoding="utf-8")
    os.replace(temporary, output / "evaluation-record.json")
    runtime = record.get("benchmark_runtime") or {}
    source = {
        "benchmark": {
            "ref": runtime.get("ref") or "unknown",
            "commit": runtime.get("commit") or "unknown",
            "resolved_from": runtime.get("resolved_from") or "unknown",
        },
        "lens": os.environ.get("LENS_SOURCE_VERSION") or "unknown",
    }
    configuration = record.get("configuration") or {}
    provenance = (record.get("deployment_configuration") or {}).get("provenance") or {}
    configuration_ids = sorted(
        {
            value
            for value in (
                record.get("configuration_artifact_id"),
                configuration.get("configuration_artifact_id"),
                provenance.get("configuration_artifact_id"),
                provenance.get("derived_from_configuration_artifact_id"),
                *(record.get("configuration_artifact_ids") or []),
            )
            if isinstance(value, str) and value
        }
    )
    # Older saved tails have unknown completeness; conservatively label them.
    truncated = any(record.get(f"{stream}_truncated", bool(record.get(stream))) for stream in ("stdout", "stderr"))
    files = {
        path.relative_to(output).as_posix(): {"truncated": False}
        for path in output.rglob("*")
        if path.is_file()
        and path.name != "manifest.json"
        and not any(part.startswith(".") for part in path.relative_to(output).parts)
        and not path.name.endswith((".tmp", ".lock"))
    }
    files["evaluation-record.json"] = {"truncated": truncated, "kind": "result-snapshot"}
    for name in ("stdout", "stderr"):
        log = f"process.{name}.log"
        if log in files:
            files[log] = {"truncated": bool(record.get("process_logs_incomplete")), "kind": "process-log"}
    manifest = register_artifacts(
        output,
        owner_type="evaluation",
        owner_id=record["id"],
        source_version=source,
        configuration_ids=configuration_ids,
        retention_class="evidence",
        status=record["status"],
        truncated=truncated,
        files=files,
    )
    record["artifact_manifest"] = manifest
    record["artifact_ref"] = manifest["uri"]


def _get(kind: str, run_id: str) -> dict | None:
    record_id = _identifier(run_id)
    if kind == "benchmark":
        record = _benchmark_records.get(record_id)
        return record.to_record_dict() if record else None
    if kind == "workflow":
        record = _workflow_records.get(record_id)
        return record.to_record_dict() if record else None
    raise ValueError("evaluate record kind is invalid")


def _records(kind: str) -> list[dict]:
    if kind == "benchmark":
        return [record.to_record_dict() for record in _benchmark_records.list()]
    if kind == "workflow":
        return [record.to_record_dict() for record in _workflow_records.list()]
    raise ValueError("evaluate record kind is invalid")


def _evaluate_record_readable(principal, record: dict, *, permission: str, resource_type: str) -> bool:
    """Owner/cluster/share-aware visibility for evaluate's persisted records.

    End-user reads are self-scoped, so the creator's own records must stay
    visible even though they hold only a cluster binding; maintainers/admins
    keep cluster-wide visibility.
    """
    if principal is None:
        return True
    return resource_readable(
        principal,
        permission=permission,
        resource_type=resource_type,
        resource_id=str(record.get("id")),
        cluster_id=record.get("cluster_id"),
        owner_user_id=record.get("owner_user_id"),
    )


def _require_evaluate_record(principal, record: dict, *, permission: str, resource_type: str, detail: str) -> None:
    if not _evaluate_record_readable(principal, record, permission=permission, resource_type=resource_type):
        raise HTTPException(status_code=403, detail=detail)


# Legacy records are migrated only by the explicit storage migration command.
def _delete(kind: str, run_id: str) -> None:
    record_id = _identifier(run_id)
    if kind == "benchmark":
        _benchmark_records.delete(record_id)
        return
    if kind == "workflow":
        _workflow_records.delete(record_id)
        return
    raise ValueError("evaluate record kind is invalid")


def _deployment_usage_reason(execution_id: str) -> str | None:
    """Report why a deployment must not be deleted yet, if anything still uses it.

    Only unfinished work blocks deletion. Finished benchmarks keep their
    ``deployment_execution_id`` purely as a historical reference and stay
    readable after the deployment record is gone.
    """
    for record in _records("benchmark"):
        if (
            record.get("deployment_execution_id") == execution_id
            and record.get("status") not in _TERMINAL_EVALUATE_STATUSES
        ):
            return f"benchmark run {record['id']} is {record.get('status')} against this deployment"
    for workflow in _records("workflow"):
        if workflow.get("status") in _TERMINAL_EVALUATE_STATUSES:
            continue
        for case in workflow.get("cases", []):
            if case.get("execution_id") == execution_id and case.get("status") not in _TERMINAL_EVALUATE_STATUSES:
                return f"evaluation {workflow['id']} is {workflow.get('status')} against this deployment"
    return None


register_usage_probe("evaluate", _deployment_usage_reason)


def _standardized_lifecycle_payload(report: dict) -> dict:
    """Adapt collected benchmark-report YAML to the inference-perf lifecycle schema.

    v0.1 retains the client measurement duration. v0.2+ run.time includes
    harness setup, so it must not be substituted for that duration.
    """
    version = str(report.get("version", ""))
    if version == "0.1":
        metrics = report.get("metrics") or {}
        throughput = metrics.get("throughput") or {}
    elif version in {"0.2", "0.2.1"}:
        metrics = ((report.get("results") or {}).get("request_performance") or {}).get("aggregate") or {}
        rates = metrics.get("throughput") or {}
        throughput = {
            target: (rates.get(source) or {}).get("mean")
            for target, source in {
                "output_tokens_per_sec": "output_token_rate",
                "input_tokens_per_sec": "input_token_rate",
                "total_tokens_per_sec": "total_token_rate",
                "requests_per_sec": "request_rate",
            }.items()
        }
    else:
        return {}
    if not metrics:
        return {}
    requests = metrics.get("requests") or {}
    total, failures = requests.get("total"), requests.get("failures")
    if not isinstance(total, int) or not isinstance(failures, int) or not 0 <= failures <= total:
        return {}
    latency = {}
    for name, values in (metrics.get("latency") or {}).items():
        unit = values.get("units", "s")
        if unit not in {"s", "s/token", "ms", "ms/token"}:
            continue
        scale = 0.001 if unit.startswith("ms") else 1
        latency[name] = {
            ("median" if key == "p50" else key): value * scale
            for key, value in values.items()
            if isinstance(value, (int, float))
        }

    def lengths(name):
        return {
            ("median" if key == "p50" else key): value
            for key, value in (requests.get(name) or {}).items()
            if isinstance(value, (int, float))
        }

    return {
        "successes": {
            "count": total - failures,
            "throughput": throughput,
            "latency": latency,
            "prompt_len": lengths("input_length"),
            "output_len": lengths("output_length"),
        },
        "failures": {"count": failures},
        "benchmark_time_seconds": (metrics.get("time") or {}).get("duration"),
    }


def _metric_summary(result_root: Path, pattern: str = "summary_lifecycle_metrics.json") -> dict:
    summaries = list(result_root.rglob(pattern))
    if summaries:
        summary_file = max(summaries, key=lambda item: item.stat().st_mtime)
        payload = json.loads(summary_file.read_text(encoding="utf-8"))
    else:
        # The collector may retain standardized YAML reports instead of raw JSON.
        # Match the exact lifecycle suffix so stages and summary never mix.
        reports = list(result_root.rglob(f"benchmark_report*,_{pattern}.yaml"))
        reports.sort(
            key=lambda item: (
                item.name.startswith("benchmark_report,_") or item.name.startswith("benchmark_report_v0.1,_"),
                item.stat().st_mtime,
                item.name,
            ),
            reverse=True,
        )
        payload = {}
        for summary_file in reports:
            payload = _standardized_lifecycle_payload(yaml.safe_load(summary_file.read_text(encoding="utf-8")) or {})
            if payload:
                break
        if not payload:
            return {}
    successes = payload.get("successes") or {}
    failures = payload.get("failures") or {}
    load = payload.get("load_summary") or {}
    latency = successes.get("latency") or {}
    throughput = successes.get("throughput") or {}
    token_lengths = {
        name: {
            key: value
            for key, source in {
                "mean": "mean",
                "min": "min",
                "max": "max",
                "p50": "median",
                "p75": "p75",
                "p90": "p90",
                "p95": "p95",
                "p99": "p99",
            }.items()
            if isinstance((value := (successes.get(source_name) or {}).get(source)), (int, float))
        }
        for name, source_name in {"input": "prompt_len", "output": "output_len"}.items()
    }
    success_count = successes.get("count") if isinstance(successes.get("count"), int) else 0
    failure_count = failures.get("count") if isinstance(failures.get("count"), int) else 0
    total_count = success_count + failure_count
    distributions = {
        name: _latency_distribution(latency.get(source) or {})
        for name, source in {
            "request_latency": "request_latency",
            "ttft": "time_to_first_token",
            "itl": "inter_token_latency",
            "tpot": "time_per_output_token",
            "ntpot": "normalized_time_per_output_token",
        }.items()
    }
    return {
        "throughput_tps": throughput.get("output_tokens_per_sec"),
        "input_throughput_tps": throughput.get("input_tokens_per_sec"),
        "total_throughput_tps": throughput.get("total_tokens_per_sec"),
        "throughput_rps": throughput.get("requests_per_sec"),
        "ttft_ms": distributions["ttft"].get("p50_ms"),
        "tpot_ms": distributions["tpot"].get("p50_ms"),
        "itl_ms": distributions["itl"].get("p50_ms"),
        "ntpot_ms": distributions["ntpot"].get("p50_ms"),
        "request_latency_ms": distributions["request_latency"].get("p50_ms"),
        "latency_distributions": distributions,
        "token_distributions": token_lengths,
        "total_input_tokens": (successes.get("prompt_tokens") or {}).get("total"),
        "total_output_tokens": (successes.get("output_tokens") or {}).get("total"),
        "success_count": success_count,
        "failure_count": failure_count,
        "request_count": total_count,
        "success_rate": round(success_count / total_count * 100, 3) if total_count else None,
        "error_rate": round(failure_count / total_count * 100, 3) if total_count else None,
        "scheduled_request_count": load.get("count"),
        "schedule_delay": _latency_distribution(load.get("schedule_delay") or {}),
        "benchmark_time_seconds": payload.get("benchmark_time_seconds"),
        "summary_path": str(summary_file),
    }


def _seconds_to_ms(value):
    return round(float(value) * 1000, 3) if isinstance(value, (int, float)) else None


def _latency_distribution(values: dict) -> dict:
    return {
        key: converted
        for key, source in {
            "mean_ms": "mean",
            "min_ms": "min",
            "max_ms": "max",
            "p50_ms": "median",
            "p75_ms": "p75",
            "p90_ms": "p90",
            "p95_ms": "p95",
            "p99_ms": "p99",
        }.items()
        if (converted := _seconds_to_ms(values.get(source))) is not None
    }


def _backfill_metric_summary(record: dict) -> bool:
    """Fill missing client metrics from persisted reports, preserving stage scope."""
    changed = False

    def fill(target: dict, expanded: dict) -> None:
        nonlocal changed
        for key, value in expanded.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                fill(target[key], value)
            elif target.get(key) in (None, {}, []) and value not in (None, {}, []):
                target[key] = value
                changed = True

    metrics = record.get("metrics")
    if not isinstance(metrics, dict):
        return False
    summary_path = metrics.get("summary_path")
    if isinstance(summary_path, str) and Path(summary_path).is_file():
        fill(metrics, _metric_summary(Path(summary_path).parent))
    output = record.get("result_output") or record.get("output")
    if not isinstance(output, str) or not Path(output).is_dir():
        return changed
    root = Path(output)
    matrix = record.get("matrix_results") or []
    if matrix:
        for index, point in enumerate(matrix):
            point_root = root / f"matrix-{index}"
            fill(point.setdefault("metrics", {}), _metric_summary(point_root))
            for stage_index, stage in enumerate(point.get("stage_metrics") or []):
                fill(stage.setdefault("metrics", {}), _stage_metric_summary(point_root, stage_index))
    else:
        fill(metrics, _metric_summary(root))
        for index, stage in enumerate(record.get("rate_stage_results") or []):
            fill(stage.setdefault("metrics", {}), _stage_metric_summary(root, index))
    return changed


def _stage_metric_summary(result_root: Path, stage_index: int) -> dict:
    """Read one closed-loop concurrency stage's results out of a matrix-point run directory."""
    return _metric_summary(result_root, pattern=f"stage_{stage_index}_lifecycle_metrics.json")


def _inference_server_config(model: str, endpoint_url: str, api_key: str | None) -> dict:
    """inference-perf ``server`` block; carries ``api_key`` when routed by a Gateway."""
    server = {"type": "vllm", "model_name": model, "base_url": endpoint_url, "ignore_eos": True}
    if api_key:
        server["api_key"] = api_key
    return server


def _matrix_workload_yaml(
    point: WorkloadMatrixPoint,
    stages: list[ConcurrencyStage],
    model: str,
    endpoint_url: str,
    targets: dict | None = None,
    api_key: str | None = None,
) -> str:
    """Build an exact-length, closed-loop inference-perf workload for one ISL/OSL matrix point.

    Mirrors the methodology used by hand-built P/D disaggregation benchmarks: fixed (not
    sampled) input/output token lengths, `ignore_eos` so the server always emits the full
    output length, and a `type: concurrent` load with one stage per requested concurrency
    level so a single invocation covers the whole concurrency sweep for this ISL/OSL point.
    """
    workload = {
        "load": {
            "type": "concurrent",
            "stages": [
                {"concurrency_level": stage.concurrency, "num_requests": stage.num_requests} for stage in stages
            ],
        },
        "api": {"type": "completion", "streaming": True},
        "server": _inference_server_config(model, endpoint_url, api_key),
        "tokenizer": {"pretrained_model_name_or_path": model},
        "data": {
            "type": "random",
            "input_distribution": {"min": point.isl, "max": point.isl, "mean": point.isl, "std_dev": 0},
            "output_distribution": {"min": point.osl, "max": point.osl, "mean": point.osl, "std_dev": 0},
        },
        "report": {
            "request_lifecycle": {"summary": True, "per_stage": True, "per_request": False},
        },
    }
    return yaml.safe_dump(enable_request_reports(workload, targets or {}), sort_keys=False)


def _shared_prefix_workload_yaml(
    spec: SharedPrefixWorkloadSpec,
    model: str,
    endpoint_url: str,
    targets: dict | None = None,
    api_key: str | None = None,
) -> str:
    """Build an open-loop, shared-system-prompt inference-perf workload.

    Guide-agnostic counterpart to `_matrix_workload_yaml`: instead of a closed-loop sweep of
    exact-length random requests, this drives a poisson-arrival rate ramp (`load.stages`)
    against a small set of reused system-prompt groups (`data.type: shared_prefix`), stressing
    prefix-cache reuse/offloading on whichever deployment endpoint is under test. All rate
    stages run within a single llmdbenchmark invocation; each stage's results are read back
    individually via `_stage_metric_summary`, same as the matrix sweep's concurrency stages.
    """
    workload = {
        "load": {
            "type": "poisson",
            "interval": 0,  # Generated rate stages run without an extra pause.
            "stages": [{"rate": stage.rate, "duration": stage.duration} for stage in spec.stages],
            # Bound client-side fanout so stalled requests cannot leave the
            # harness waiting indefinitely while the service is already idle.
            "num_workers": 1,
            "worker_max_concurrency": 8,
            "worker_max_tcp_connections": 32,
            "request_timeout": 120,
        },
        "api": {"type": "completion", "streaming": True},
        "server": _inference_server_config(model, endpoint_url, api_key),
        "tokenizer": {"pretrained_model_name_or_path": model},
        "data": {
            "type": "shared_prefix",
            "shared_prefix": {
                "num_groups": spec.num_groups,
                "num_prompts_per_group": spec.num_prompts_per_group,
                "system_prompt_len": spec.system_prompt_len,
                "question_len": spec.question_len,
                "output_len": spec.output_len,
                "enable_multi_turn_chat": spec.enable_multi_turn_chat,
            },
        },
        "report": {
            "request_lifecycle": {"summary": True, "per_stage": True, "per_request": False},
        },
    }
    return yaml.safe_dump(enable_request_reports(workload, targets or {}), sort_keys=False)


def _inline_workload_yaml(
    content: str, model: str, endpoint_url: str, targets: dict | None = None, api_key: str | None = None
) -> str:
    """Bind a user-authored inference-perf workload to the selected deployment."""
    workload = validate_inline_workload(content)
    workload["server"] = _inference_server_config(model, endpoint_url, api_key)
    return yaml.safe_dump(enable_request_reports(workload, targets or {}), sort_keys=False)


async def _kubectl_json(arguments: list[str], environment: dict[str, str]) -> dict | None:
    from llm_d_bench.utils.kubernetes_commands import execute_sdk_command, sdk_enabled

    if sdk_enabled():
        result = await execute_sdk_command(
            ["kubectl", *arguments, "-o", "json"],
            kubeconfig=environment.get("KUBECONFIG") or os.environ.get("KUBECONFIG"),
            timeout=5,
        )
        if result is not None:
            return json.loads(result.stdout) if result.returncode == 0 else None
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        *arguments,
        "-o",
        "json",
        env={**os.environ, **environment},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return None
    if process.returncode:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


async def _kubectl_raw(path: str, environment: dict[str, str]) -> str | None:
    from llm_d_bench.utils.kubernetes_commands import execute_sdk_command, sdk_enabled

    if sdk_enabled():
        result = await execute_sdk_command(
            ["kubectl", "get", "--raw", path],
            kubeconfig=environment.get("KUBECONFIG") or os.environ.get("KUBECONFIG"),
            timeout=5,
        )
        if result is not None:
            return result.stdout if result.returncode == 0 else None
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        "get",
        "--raw",
        path,
        env={**os.environ, **environment},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    return stdout.decode() if process.returncode == 0 else None


# Semantic key for each ``device_metric_sources`` entry the direct scrape
# reports; an entry the profile does not configure is omitted from the scrape
# (empty value == disabled).
_XPUMD_METRIC_KEYS = {
    "utilization": "utilization_ratio",
    "framebuffer_used": "memory_used_bytes",
    "vram": "memory_total_bytes",
    "memory_utilization": "memory_utilization_ratio",
    "power": "power_watts",
    "temperature": "temperature_celsius",
}


def _intel_telemetry():
    profile = resolve_by_accelerator_key("intel_gpu")
    return profile.telemetry if profile is not None else None


def _parse_xpumd_metrics(payload: str | None) -> dict | None:
    if not payload:
        return None
    telemetry = _intel_telemetry()
    if telemetry is None:
        return None
    sources = telemetry.device_metric_sources
    wanted = {
        source.metric: semantic
        for source_name, semantic in _XPUMD_METRIC_KEYS.items()
        if (source := sources.get(source_name)) is not None and source.metric
    }
    if not wanted:
        return None
    utilization = sources.get("utilization")
    utilization_metric = utilization.metric if utilization is not None else None
    utilization_match = dict(utilization.match) if utilization is not None else {}
    pci_label = telemetry.label_schema.get("pci") or "pci_bdf"
    devices: dict[str, dict] = {}
    pattern = re.compile(r"^(\w+)\{([^}]*)\}\s+([-+\deE.]+)$")
    for line in payload.splitlines():
        match = pattern.match(line)
        if not match or match.group(1) not in wanted:
            continue
        labels = dict(re.findall(r'(\w+)="([^"]*)"', match.group(2)))
        if match.group(1) == utilization_metric and any(
            labels.get(key) != value for key, value in utilization_match.items()
        ):
            continue
        device_id = labels.get(pci_label) or labels.get("hw_id")
        if not device_id:
            continue
        device = devices.setdefault(device_id, {"id": device_id, "name": labels.get("hw_name")})
        device[wanted[match.group(1)]] = float(match.group(3))
    if not devices:
        return None
    values = list(devices.values())
    utilization_values = [item["utilization_ratio"] for item in values if "utilization_ratio" in item]
    return {
        "source": "xpumd-direct",
        "captured_at": _now(),
        "scope": "cluster-device",
        "device_count": len(values),
        "devices": values,
        "average_utilization_ratio": sum(utilization_values) / len(utilization_values) if utilization_values else None,
        "maximum_utilization_ratio": max(utilization_values) if utilization_values else None,
    }


def _xpumd_metrics_proxy_path() -> str | None:
    """Device-telemetry scrape path from the hardware profile (None = disabled)."""
    telemetry = _intel_telemetry()
    if telemetry is not None:
        return telemetry.scrape_path or None
    return "/api/v1/namespaces/intel-xpumd/services/http:xpumd:8080/proxy/metrics"


async def _kubernetes_resource_snapshot(session_id: str | None, namespace: str | None) -> dict | None:
    """Return configured allocation/capacity, not runtime utilization."""
    if not session_id:
        return None
    try:
        environment = deployment_runtime_overrides(session_id)
    except ValueError:
        return None
    pods = await _kubectl_json(["get", "pods", "-n", namespace], environment) if namespace else None
    nodes = await _kubectl_json(["get", "nodes"], environment)
    scrape_path = _xpumd_metrics_proxy_path()
    gpu_telemetry = (
        _parse_xpumd_metrics(await _kubectl_raw(scrape_path, environment)) if scrape_path else None
    )
    if pods is None and nodes is None:
        return None
    pod_allocations = []
    requested_gpus = 0
    for pod in (pods or {}).get("items", []):
        containers = []
        for container in pod.get("spec", {}).get("containers", []):
            resources = container.get("resources") or {}
            requests = resources.get("requests") or {}
            limits = resources.get("limits") or {}
            requested_gpus += sum(
                int(value)
                for key, value in {**requests, **limits}.items()
                if ("gpu" in key.lower() or "xpu" in key.lower()) and str(value).isdigit()
            )
            containers.append({"name": container.get("name"), "requests": requests, "limits": limits})
        pod_allocations.append(
            {
                "name": pod.get("metadata", {}).get("name"),
                "phase": pod.get("status", {}).get("phase"),
                "node": pod.get("spec", {}).get("nodeName"),
                "containers": containers,
            }
        )
    node_capacity = []
    allocatable_gpus = 0
    for node in (nodes or {}).get("items", []):
        allocatable = node.get("status", {}).get("allocatable") or {}
        gpu_resources = {
            key: value for key, value in allocatable.items() if "gpu" in key.lower() or "xpu" in key.lower()
        }
        allocatable_gpus += sum(int(value) for value in gpu_resources.values() if str(value).isdigit())
        node_capacity.append(
            {
                "name": node.get("metadata", {}).get("name"),
                "cpu": allocatable.get("cpu"),
                "memory": allocatable.get("memory"),
                "gpus": gpu_resources,
            }
        )
    return {
        "source": "kubernetes-api",
        "captured_at": _now(),
        "namespace": namespace,
        "requested_gpus": requested_gpus,
        "allocatable_gpus": allocatable_gpus,
        "pods": pod_allocations,
        "nodes": node_capacity,
        "utilization_available": gpu_telemetry is not None,
        "gpu_telemetry": gpu_telemetry,
    }


def _benchmark_ready(root: Path) -> bool:
    """True when the managed benchmark install is present *and* executable.

    A venv is not relocatable: its console scripts embed an absolute shebang. If the
    venv was ever moved after install, the script still exists but its interpreter
    points at a deleted path. Validate the shebang so such a broken install is
    detected and rebuilt rather than surfaced as a confusing ENOENT at exec time.
    """
    script = root / ".venv" / "bin" / "llmdbenchmark"
    if not script.is_file():
        return False
    try:
        shebang = script.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return False
    if not shebang.startswith("#!"):
        return False
    interpreter = shebang[2:].strip()
    return bool(interpreter) and Path(interpreter).exists()


async def _prepare_benchmark_runtime(source: dict | None = None) -> tuple[str, Path]:
    resolved_repository = str((source or {}).get("resolved_repository") or "").strip()
    if resolved_repository:
        root = Path(resolved_repository).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"cluster llm-d-benchmark source path is unavailable: {root}")
        if not _benchmark_ready(root):
            async with _benchmark_install_lock:
                if not _benchmark_ready(root):
                    await asyncio.to_thread(_install_benchmark_checkout, root)
        # Cheap and idempotent: re-check on every call (not just right after a
        # fresh install) so checkouts installed before this patch existed --
        # or reinstalled/upgraded later -- still get it applied.
        await asyncio.to_thread(_patch_kubernetes_client_no_proxy_bug, root / ".venv")
        return str(root / ".venv" / "bin" / "llmdbenchmark"), root
    raise ValueError(
        "Cluster llm-d-benchmark source is required. Configure and download its Software Versions before benchmarking."
    )


def _install_benchmark_checkout(root: Path) -> None:
    """Install the CLI in the cluster-owned checkout without cloning another source tree."""
    lock_path = root.parent / f".{root.name}.install.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _benchmark_ready(root):
            return
        venv = root / ".venv"
        shutil.rmtree(venv, ignore_errors=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv)], timeout=120, check=True)
        # Newer checkouts split reports into a workspace distribution which may
        # not yet be published on PyPI. Resolve it from the same source revision
        # in the same pip transaction; older checkouts have no separate package.
        report = root / "benchmark-report"
        workspace_packages = [str(report)] if (report / "pyproject.toml").is_file() else []
        install = subprocess.run(
            [
                str(venv / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "-e",
                str(root),
                *workspace_packages,
                _BENCHMARK_PLANNER,
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if install.returncode:
            shutil.rmtree(venv, ignore_errors=True)
            raise RuntimeError(install.stderr.strip()[-2000:] or "benchmark dependency installation failed")
        verify = subprocess.run(
            [
                str(venv / "bin" / "python"),
                "-c",
                "import llmdbenchmark; from planner.capacity_planner import model_memory_req",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if verify.returncode:
            shutil.rmtree(venv, ignore_errors=True)
            raise RuntimeError(verify.stderr.strip()[-2000:] or "benchmark dependency verification failed")


# kubernetes==35.0.0's generated Configuration.__init__ reads NO_PROXY/no_proxy
# from the environment and then, a few lines later, unconditionally resets
# ``self.no_proxy = None`` -- a duplicate-assignment artifact of its OpenAPI
# generator, not intentional behavior. That silently makes NO_PROXY a no-op
# for every Kubernetes API call the harness process makes: any corporate
# HTTP(S)_PROXY set for external network access (model/package downloads)
# also gets applied to the cluster's own API server, so node
# auto-detection fails with a proxy 403 even though NO_PROXY correctly lists
# the API server host. Patch it out in-place; best-effort/idempotent so it
# quietly no-ops if a future kubernetes version changes or fixes this.
_KUBERNETES_NO_PROXY_BUG_PATTERN = (
    '        if os.getenv("no_proxy"): self.no_proxy = os.getenv("no_proxy")\n'
    '        """Proxy URL\n'
    '        """\n'
    "        self.no_proxy = None\n"
    '        """bypass proxy for host in the no_proxy list.\n'
    '        """\n'
)
_KUBERNETES_NO_PROXY_BUG_REPLACEMENT = (
    '        if os.getenv("no_proxy"): self.no_proxy = os.getenv("no_proxy")\n'
    '        """Proxy URL\n'
    '        """\n'
    '        """bypass proxy for host in the no_proxy list.\n'
    '        """\n'
)


def _patch_kubernetes_client_no_proxy_bug(venv: Path) -> None:
    """Work around kubernetes-python-client's Configuration clobbering no_proxy.

    See ``_KUBERNETES_NO_PROXY_BUG_PATTERN`` above for the bug this fixes.
    """
    for configuration_file in venv.glob("lib/python*/site-packages/kubernetes/client/configuration.py"):
        try:
            text = configuration_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if _KUBERNETES_NO_PROXY_BUG_PATTERN in text:
            configuration_file.write_text(
                text.replace(_KUBERNETES_NO_PROXY_BUG_PATTERN, _KUBERNETES_NO_PROXY_BUG_REPLACEMENT, 1),
                encoding="utf-8",
            )


def _harness_proxy_environment(run: dict, *, kubeconfig_path: str | None = None) -> dict[str, str]:
    """Add harness bypass entries without dropping backend cluster connectivity."""
    environment: dict[str, str] = {}
    http_proxy = str(
        run.get("http_proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    ).strip()
    https_proxy = str(
        run.get("https_proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    ).strip()
    # Run settings are intended for the harness and Helm. Preserve the
    # backend's API-server IPs/CIDRs for their cluster requests;
    # cluster-local DNS suffixes alone do not bypass an IP-based API endpoint.
    no_proxy_entries: list[str] = []
    for value in (run.get("no_proxy"), os.environ.get("NO_PROXY"), os.environ.get("no_proxy")):
        for raw_entry in str(value or "").split(","):
            entry = raw_entry.strip()
            if entry and entry not in no_proxy_entries:
                no_proxy_entries.append(entry)
    for entry in ("localhost", "127.0.0.1", ".svc", ".cluster.local"):
        if entry not in no_proxy_entries:
            no_proxy_entries.append(entry)
    # The cluster's own API server (often an internal IP a corporate proxy
    # can't/won't tunnel to -- see the "warm-up run failed ... ProxyError"
    # class of bug) must always bypass the proxy, regardless of whether the
    # backend's own NO_PROXY happens to include it.
    api_server = api_server_host(kubeconfig_path)
    if api_server and api_server not in no_proxy_entries:
        no_proxy_entries.append(api_server)
    if http_proxy:
        environment.update(HTTP_PROXY=http_proxy, http_proxy=http_proxy)
    if https_proxy:
        environment.update(HTTPS_PROXY=https_proxy, https_proxy=https_proxy)
    if no_proxy_entries:
        normalized_no_proxy = ",".join(no_proxy_entries)
        environment.update(NO_PROXY=normalized_no_proxy, no_proxy=normalized_no_proxy)
    return environment


def _install_helm_proxy_wrapper(output: Path, environment: dict[str, str], proxy_environment: dict[str, str]) -> None:
    """Give Helm external-network proxy settings without proxying Kubernetes clients."""
    helm = shutil.which("helm", path=environment.get("PATH"))
    proxies = {
        name: value
        for name, value in proxy_environment.items()
        if name in {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"}
    }
    if not helm or not any(name.lower() in {"http_proxy", "https_proxy"} for name in proxies):
        return
    wrapper_directory = output / ".bin"
    wrapper_directory.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_directory / "helm"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"os.environ.update(json.loads({json.dumps(proxies)!r}))\n"
        f"os.execv({helm!r}, [{helm!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    environment["PATH"] = f"{wrapper_directory}{os.pathsep}{environment.get('PATH', '')}"


def _harness_proxy_override(proxy_environment: dict[str, str]) -> str | None:
    """Build the legacy-compatible proxy override for benchmark harnesses."""
    items = [
        {"name": name, "value": proxy_environment[name]}
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "OPENAI_API_KEY")
        if name in proxy_environment
    ]
    return f"harness.extraEnvVars={json.dumps(items, separators=(',', ':'))}" if items else None


def _harness_resource_overrides(run: dict, benchmark_root: Path) -> list[str]:
    """Override accelerator overlays that size the load generator for smoke tests.

    Older benchmark templates use memory for both request and limit. Newer ones
    have a separate memoryLimit, which must also override the XPU overlay's 8Gi.
    """
    memory = f"{run.get('harness_memory_gib', 32)}Gi"
    overrides = [f"harness.resources.memory={memory}"]
    template = benchmark_root / "config/templates/jinja/20_harness_pod.yaml.j2"
    if template.is_file() and "harness.resources.memoryLimit" in template.read_text(encoding="utf-8"):
        overrides.append(f"harness.resources.memoryLimit={memory}")
    return overrides


def _benchmark_failure_message(message: str, run: dict) -> str:
    # Exit 137 alone is also possible for other SIGKILL causes; require the
    # Kubernetes OOM reason and the harness container identity before classifying.
    if re.search(r"[^\s/]+/harness\s+\(terminated:\s*OOMKilled\b", message):
        memory = run.get("harness_memory_gib", 32)
        return (
            f"Benchmark load generator (harness) was OOMKilled: its host memory budget was {memory} GiB "
            "per worker. Increase harness_memory_gib in Benchmark > Advanced or reduce the workload's "
            "request rate, concurrency, or dataset size. This is not a model GPU out-of-memory error.\n\n" + message
        )
    return message


def _execution_model(execution) -> str:
    if execution.endpoint and execution.endpoint.model_ref:
        return execution.endpoint.model_ref
    for artifact in execution.configuration_artifacts:
        try:
            model = json.loads(artifact.content or "{}").get("model") or {}
            if isinstance(model.get("name"), str) and model["name"]:
                return model["name"]
        except json.JSONDecodeError:
            continue
    raise ValueError("deployment execution does not define a benchmark model")


def _execution_accelerator_profile(execution, configured: str | None = None) -> str | None:
    """Resolve a benchmark runtime profile from request or the deployment's DRA driver."""
    if configured:
        return configured
    candidates: set[str] = set()
    xpu_runtime = False
    for artifact in execution.configuration_artifacts:
        try:
            content = json.loads(artifact.content or "{}")
        except json.JSONDecodeError:
            continue
        manifest = str((content.get("officialGuide") or {}).get("renderedManifest") or "")
        candidates.update(
            re.findall(
                r"^\s*deviceClassName:\s*['\"]?([a-z0-9.-]+)",
                manifest,
                flags=re.IGNORECASE | re.MULTILINE,
            )
        )
        runtime = content.get("runtime") or {}
        xpu_runtime = xpu_runtime or "xpu" in str(runtime.get("image") or "").lower()
        xpu_runtime = xpu_runtime or getattr(artifact, "provider_ref", "") == "baseline-vllm"
    accelerator_drivers = sorted(
        candidate for candidate in candidates if re.search(r"gpu|xpu|gaudi|tpu", candidate, flags=re.IGNORECASE)
    )
    if len(accelerator_drivers) == 1:
        profile = resolve_by_device_class(accelerator_drivers[0])
        return profile.benchmark_profile if profile else None
    profile = resolve_by_accelerator_key("xpu") if xpu_runtime else None
    return profile.benchmark_profile if profile else None


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def _benchmark_kubectl_json(environment: dict[str, str], *arguments: str, stdin: bytes | None = None) -> dict:
    kubectl = environment.get("LLM_D_BENCH_KUBECTL_PATH") or shutil.which("kubectl")
    if not kubectl:
        raise RuntimeError("kubectl is required to prepare benchmark storage")
    process = await asyncio.create_subprocess_exec(
        kubectl,
        *arguments,
        env=environment,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(stdin)
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip() or "kubectl command failed")
    return json.loads(stdout or b"{}") if stdout.strip() else {}


async def _prepare_benchmark_storage(
    run_id: str,
    namespace: str,
    environment: dict[str, str],
    configured_storage_class: str | None,
) -> tuple[str, str | None]:
    if configured_storage_class:
        return configured_storage_class, None
    storage_classes = await _benchmark_kubectl_json(environment, "get", "storageclass", "-o", "json")
    for item in storage_classes.get("items", []):
        annotations = (item.get("metadata") or {}).get("annotations") or {}
        if (
            annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
            or annotations.get("storageclass.beta.kubernetes.io/is-default-class") == "true"
        ):
            return str(item["metadata"]["name"]), None
    target_node = os.environ.get("PRISM_K8S_TARGET_NODE", "").strip()
    storage_class = "prism-benchmark-local"
    volume_name = f"prism-benchmark-{run_id}"
    persistent_volume_spec = {
        "capacity": {"storage": "20Gi"},
        # The benchmark harness creates its workload PVC as ReadWriteMany; a PV
        # must support every mode the claim requests, so list both (a hostPath
        # PV is node-local anyway). ReadWriteOnce-only left the PVC Pending
        # until the harness timed out.
        "accessModes": ["ReadWriteOnce", "ReadWriteMany"],
        "persistentVolumeReclaimPolicy": "Delete",
        "storageClassName": storage_class,
        "claimRef": {"namespace": namespace, "name": "workload-pvc"},
        "hostPath": {
            # Per-run node-local storage for the benchmark PV, not a host temp-file write.
            "path": f"/var/tmp/llm-d-prism/benchmarks/{run_id}",  # noqa: S108
            "type": "DirectoryOrCreate",
        },
    }
    if target_node:
        persistent_volume_spec["nodeAffinity"] = {
            "required": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": [target_node],
                            }
                        ]
                    }
                ]
            }
        }
    manifest = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": storage_class},
                "provisioner": "kubernetes.io/no-provisioner",
                "reclaimPolicy": "Delete",
                "volumeBindingMode": "Immediate",
            },
            {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {"name": volume_name},
                "spec": persistent_volume_spec,
            },
        ],
    }
    await _benchmark_kubectl_json(
        environment,
        "apply",
        "--filename=-",
        "--output=json",
        stdin=json.dumps(manifest).encode(),
    )
    return storage_class, volume_name


async def _cleanup_benchmark_storage(namespace: str, volume_name: str | None, environment: dict[str, str]) -> None:
    if not volume_name:
        return
    with contextlib.suppress(json.JSONDecodeError, RuntimeError):
        await _benchmark_kubectl_json(
            environment,
            "delete",
            "pvc",
            "workload-pvc",
            "--namespace",
            namespace,
            "--ignore-not-found=true",
            "--wait=false",
            "--output=json",
        )
    with contextlib.suppress(json.JSONDecodeError, RuntimeError):
        await _benchmark_kubectl_json(
            environment,
            "delete",
            "persistentvolume",
            volume_name,
            "--ignore-not-found=true",
            "--wait=false",
            "--output=json",
        )


async def _stream_benchmark_process(
    process, run: dict, phase: str = "benchmark", *, namespace: str = "", environment: dict | None = None
) -> tuple[bytes, bytes]:
    """Drain both pipes while publishing bounded output and a liveness heartbeat."""
    tails = {"stdout": b"", "stderr": b""}
    output = _evaluation_output(run["id"]) if run.get("id") else None
    log_paths = {name: output / f"process.{name}.log" for name in tails} if output and output.is_dir() else {}
    for path in log_paths.values():
        if path.is_symlink():
            raise ValueError("evaluation process log must not be a symlink")
    run.update(phase=phase, phase_started_at=_now())

    async def drain(stream, name):
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            if name in log_paths:
                with log_paths[name].open("ab") as log:
                    log.write(chunk)
            run[f"{name}_truncated"] = bool(run.get(f"{name}_truncated")) or len(tails[name]) + len(chunk) > 32768
            tails[name] = (tails[name] + chunk)[-32768:]
            run[name] = tails[name].decode(errors="replace")
            run["last_log_at"] = _now()

    async def heartbeat():
        while True:
            run["heartbeat_at"] = _now()
            _save(run)
            await asyncio.sleep(2)

    beat = asyncio.create_task(heartbeat())
    streams = asyncio.gather(drain(process.stdout, "stdout"), drain(process.stderr, "stderr"), process.wait())
    watcher = asyncio.create_task(watch_harness(run, namespace, environment or {}, _save, _now)) if namespace else None
    try:
        pending = {streams, watcher} if watcher else {streams}
        done, _ = await asyncio.wait(
            pending, timeout=run.get("wait_timeout_seconds", 7200), return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise RuntimeError("Benchmark exceeded its configured execution timeout")
        if watcher and watcher in done:
            await watcher
        await streams
    except BaseException:
        run["process_logs_incomplete"] = True
        await _terminate_process_group(process)
        raise
    finally:
        for task in (beat, watcher, streams):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (beat, watcher, streams) if task is not None), return_exceptions=True)
        _save(run)
    return tails["stdout"], tails["stderr"]


async def _cluster_gateway_endpoint(cluster_id: str | None) -> str | None:
    """The cluster's shared-Gateway base URL (no ``/v1``) for benchmark traffic.

    Gateway Mode deployments disable their own proxy, so the in-cluster harness
    reaches a deployment's EPP through the shared Gateway. Returns ``None`` when
    the cluster has no ready Gateway, so callers fall back to the stored endpoint.
    """
    if not cluster_id:
        return None
    try:
        from llm_d_bench.model_service.gateway_ops import GatewayOpsService

        base = await GatewayOpsService().cluster_gateway_base_url(cluster_id)
    except Exception:  # noqa: BLE001 - evaluation must not fail on a gateway lookup
        return None
    return base.removesuffix("/v1") if base else None


async def _execute(run_id: str) -> None:
    run = _get("benchmark", run_id)
    if run is None:
        return
    run.update(status="running", started_at=_now(), phase="preparing", phase_started_at=_now())
    _save(run)
    process: asyncio.subprocess.Process | None = None
    benchmark_volume: str | None = None
    benchmark_namespace = ""
    environment: dict[str, str] = {}
    observability_started_at: str | None = None
    kv_capture = None
    kv_workspace = None
    try:
        output = _evaluation_output(run_id)
        output.mkdir(parents=True, exist_ok=True)
        run["output"] = str(output)
        if run.get("workload_yaml"):
            validate_inline_workload(run["workload_yaml"])
        execution = _store.get_execution(run["deployment_execution_id"])
        if execution is None or execution.status != DeploymentStatus.READY or execution.endpoint is None:
            raise ValueError("deployment execution is not ready for evaluation")
        run.update(phase="preparing-monitoring", phase_started_at=_now())
        await _prepare_benchmark_monitoring(run)
        endpoint_url = execution.endpoint.url
        # All runs execute in an in-cluster harness. Never pass a
        # backend-local port-forward address (127.0.0.1) to that pod; resolve
        # the llm-d router Service in the deployment namespace instead.
        if endpoint_url and "127.0.0.1" in endpoint_url:
            endpoint_url = f"http://optimized-baseline-epp.{execution.namespace}.svc:80"
        if run.get("use_baseline_endpoint"):
            if not execution.endpoint.baseline_url:
                raise ValueError(
                    "this Guide deployment does not expose a kubernetes-service baseline endpoint for comparison"
                )
            endpoint_url = execution.endpoint.baseline_url
        elif run.get("model_service_group_id") or deployment_uses_shared_gateway(execution):
            # An "existing endpoint" run always targets a published Model
            # Service: its HTTPRoute reaches the InferencePool directly,
            # regardless of what data plane the underlying deployment
            # happened to resolve at deploy time (it may even still be
            # rendering its own, unrelated proxy). Design Configuration runs
            # (no model_service_group_id) fall back to the deployment's
            # recorded data plane: route through the Gateway only when the
            # provider declared it as this deployment's data plane.
            gateway_endpoint = await _cluster_gateway_endpoint(execution.provenance.get("cluster_server_id"))
            if gateway_endpoint:
                endpoint_url = gateway_endpoint
        # A Model Service run must request the group's published name, not the
        # execution's underlying model_ref: the Gateway's HTTPRoute only matches
        # the BASE_MODEL_HEADER that ext_authz injects after validating that name.
        model_name = run.get("model_service_published_name") or _execution_model(execution)
        run.update(endpoint_used=endpoint_url, namespace=execution.namespace, model=model_name)
        session_id = execution.provenance.get("cluster_session_id")
        environment = {**os.environ, **deployment_runtime_overrides(session_id)} if session_id else dict(os.environ)
        harness_environment = _harness_proxy_environment(run, kubeconfig_path=environment.get("KUBECONFIG"))
        api_key = _run_api_keys.pop(run_id, None)
        if api_key:
            # Shared-Gateway deployments need the user's model access token in
            # the harness Pod; it is injected via harness.extraEnvVars below.
            harness_environment["OPENAI_API_KEY"] = api_key
        # The CLI's independent Kubernetes SDK must not inherit external-network
        # proxies. Pass Pod settings as values instead of --envvarspod, which
        # couples Pod downloads to the control-plane client's environment.
        environment = {
            name: value
            for name, value in environment.items()
            if name.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        run.update(phase="preparing-runtime", phase_started_at=_now())
        _save(run)
        cli, benchmark_root = await _prepare_benchmark_runtime(run.get("benchmark_runtime"))
        try:
            revision = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(benchmark_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if revision.returncode == 0:
                run.setdefault("benchmark_runtime", {})["commit"] = revision.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass  # Explicit unknown versions cover non-Git installs.
        _install_helm_proxy_wrapper(output, environment, harness_environment)
        benchmark_namespace = execution.namespace or "default"
        run.update(phase="preparing-storage", phase_started_at=_now())
        _save(run)
        storage_class, benchmark_volume = await _prepare_benchmark_storage(
            run_id,
            benchmark_namespace,
            environment,
            run.get("storage_class_name"),
        )
        accelerator_profile = _execution_accelerator_profile(execution, run.get("accelerator_profile"))
        observability_started_at = _now()

        def _base_command(workspace: Path) -> list[str]:
            command = [
                cli,
                "--spec",
                run["specification_file"],
                "--workspace",
                str(workspace),
                "run",
                "--endpoint-url",
                endpoint_url,
                "--model",
                model_name,
                "--namespace",
                execution.namespace or "default",
                "--kubeconfig",
                environment.get("KUBECONFIG", ""),
                "--harness",
                run["harness"],
                "--parallelism",
                str(run["parallelism"]),
                "--wait-timeout",
                str(run["wait_timeout_seconds"]),
                "--output",
                "local",
            ]
            proxy_override = _harness_proxy_override(harness_environment)
            if proxy_override:
                command.extend(["--set", proxy_override])
            if accelerator_profile:
                command.extend(["--set", f"accelerator.profile={accelerator_profile}"])
            command.extend(["--set", f"storage.workloadPvc.storageClassName={storage_class}"])
            for resource_override in _harness_resource_overrides(run, benchmark_root):
                command.extend(["--set", resource_override])
            for version_override in _BENCHMARK_VERSION_OVERRIDES:
                command.extend(["--set", version_override])
            if not environment.get("KUBECONFIG"):
                command = [item for item in command if item != "--kubeconfig" and item != ""]
            return command

        kv_capture = KVTraceCollector(
            benchmark_namespace,
            environment,
            enabled=True,
            unsupported_reason="Automatic KV collection requires one benchmark worker (parallelism=1)."
            if run.get("parallelism", 1) != 1
            else None,
        )
        matrix_points = run.get("matrix") or []
        if not matrix_points:
            kv_workspace = output
            await kv_capture.begin(output)
        if matrix_points:
            # ISL x OSL matrix: one llmdbenchmark invocation per (isl, osl) point, against a
            # generated exact-length workload whose concurrency stages are read back
            # individually afterwards (see _stage_metric_summary). Never touches the shared
            # llm-d-benchmark checkout's bundled workload/profiles files.
            stages = [ConcurrencyStage(**stage) for stage in (run.get("concurrency_stages") or [])] or list(
                _DEFAULT_CONCURRENCY_STAGES
            )

            async def _run_workload(workspace: Path, workload_path: Path) -> tuple[int, str, str]:
                nonlocal process, kv_workspace
                if workspace.name != "warmup":
                    kv_workspace = workspace
                    await kv_capture.begin(workspace)
                command = _base_command(workspace) + ["--workload-file-path", str(workload_path)]
                point_process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(benchmark_root),
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                process = (
                    point_process  # so a cancelled evaluation can terminate the in-flight matrix/warm-up subprocess
                )
                point_stdout, point_stderr = await _stream_benchmark_process(
                    point_process,
                    run,
                    "warmup" if workspace.name == "warmup" else workspace.name,
                    namespace=benchmark_namespace,
                    environment=environment,
                )
                if workspace.name != "warmup":
                    await kv_capture.finish(workspace, _metric_summary(workspace), point_process.returncode == 0)
                for name, value in (("stdout", point_stdout), ("stderr", point_stderr)):
                    run[f"{name}_truncated"] = (
                        bool(run.get(f"{name}_truncated")) or len(value.decode(errors="replace")) > 4000
                    )
                return (
                    point_process.returncode,
                    point_stdout.decode(errors="replace")[-4000:],
                    point_stderr.decode(errors="replace")[-4000:],
                )

            warmup_requests = int(run.get("warmup_requests", 2) or 0)
            if warmup_requests > 0:
                # One uncounted pass at the first point's ISL/OSL, low concurrency, so the
                # first *measured* point isn't the one paying for JIT/torch-compile cold start.
                # This does not clear prefix/KV cache state between matrix points -- it only
                # protects the first point from an unfair cold-start penalty relative to the
                # rest of the sweep.
                warmup_point = WorkloadMatrixPoint(**matrix_points[0])
                warmup_workspace = output / "warmup"
                warmup_workspace.mkdir(parents=True, exist_ok=True)
                warmup_workload_path = warmup_workspace / "workload.yaml"
                warmup_workload_path.write_text(
                    _matrix_workload_yaml(
                        warmup_point,
                        [ConcurrencyStage(concurrency=1, num_requests=warmup_requests)],
                        model_name,
                        endpoint_url,
                        api_key=api_key,
                    ),
                    encoding="utf-8",
                )
                warmup_returncode, warmup_stdout, warmup_stderr = await _run_workload(
                    warmup_workspace, warmup_workload_path
                )
                run["warmup"] = {
                    "isl": warmup_point.isl,
                    "osl": warmup_point.osl,
                    "num_requests": warmup_requests,
                    "status": "failed" if warmup_returncode else "succeeded",
                    "stdout": warmup_stdout,
                    "stderr": warmup_stderr,
                }
                _save(run)
                if warmup_returncode:
                    raise RuntimeError(
                        f"warm-up run failed: {warmup_stderr or warmup_stdout or 'llmdbenchmark failed'}"
                    )

            matrix_results: list[dict] = run.setdefault("matrix_results", [])
            for index, raw_point in enumerate(matrix_points):
                point = WorkloadMatrixPoint(**raw_point)
                point_workspace = output / f"matrix-{index}"
                point_workspace.mkdir(parents=True, exist_ok=True)
                workload_path = point_workspace / "workload.yaml"
                workload_path.write_text(
                    _matrix_workload_yaml(
                        point, stages, model_name, endpoint_url, run.get("sla_targets"), api_key=api_key
                    ),
                    encoding="utf-8",
                )
                point_returncode, point_stdout, point_stderr = await _run_workload(point_workspace, workload_path)
                point_result = {
                    "isl": point.isl,
                    "osl": point.osl,
                    "stdout": point_stdout,
                    "stderr": point_stderr,
                }
                if point_returncode:
                    point_result["status"] = "failed"
                    matrix_results.append(point_result)
                    run["matrix_results"] = matrix_results
                    raise RuntimeError(
                        f"matrix point isl={point.isl} osl={point.osl} failed: "
                        f"{point_result['stderr'] or point_result['stdout'] or 'llmdbenchmark failed'}"
                    )
                point_result.update(
                    status="succeeded",
                    metrics=_metric_summary(point_workspace),
                    stage_metrics=[
                        {
                            "concurrency": stage.concurrency,
                            "num_requests": stage.num_requests,
                            "metrics": _stage_metric_summary(point_workspace, stage_index),
                        }
                        for stage_index, stage in enumerate(stages)
                    ],
                )
                matrix_results.append(point_result)
                run["matrix_results"] = matrix_results
                _save(run)
            run["command"] = [
                "llmdbenchmark",
                "--spec",
                run["specification_file"],
                "run",
                "--endpoint-url",
                "<deployment-endpoint>",
                "--model",
                model_name,
                "--namespace",
                execution.namespace or "default",
                "--harness",
                run["harness"],
                "--workload-file-path",
                "<generated-per-matrix-point>",
            ]
            run["output"] = str(output)
            run.update(status="succeeded", finished_at=_now())
        elif run.get("shared_prefix"):
            # Open-loop, shared-system-prompt rate ramp: a single llmdbenchmark invocation
            # covers every rate stage (inference-perf's own `type: poisson` ramp internally),
            # with each stage's results read back individually afterwards (see
            # _stage_metric_summary). Guide-agnostic: drives whatever endpoint this run's
            # deployment execution resolved to, regardless of which Guide/provider produced it.
            spec = SharedPrefixWorkloadSpec(**run["shared_prefix"])
            workload_path = output / "workload.yaml"
            workload_path.write_text(
                _shared_prefix_workload_yaml(
                    spec, model_name, endpoint_url, run.get("sla_targets"), api_key=api_key
                ),
                encoding="utf-8",
            )
            command = _base_command(output) + ["--workload-file-path", str(workload_path)]
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(benchmark_root),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = await _stream_benchmark_process(
                process, run, namespace=benchmark_namespace, environment=environment
            )
            run["command"] = [
                "llmdbenchmark",
                "--spec",
                run["specification_file"],
                "run",
                "--endpoint-url",
                "<deployment-endpoint>",
                "--model",
                model_name,
                "--namespace",
                execution.namespace or "default",
                "--harness",
                run["harness"],
                "--workload-file-path",
                "<generated-shared-prefix-workload>",
            ]
            run["output"] = str(output)
            for name, value in (("stdout", stdout), ("stderr", stderr)):
                run[f"{name}_truncated"] = (
                    bool(run.get(f"{name}_truncated")) or len(value.decode(errors="replace")) > 12000
                )
            run["stdout"] = stdout.decode(errors="replace")[-12000:]
            run["stderr"] = stderr.decode(errors="replace")[-12000:]
            if process.returncode:
                raise RuntimeError(run["stderr"] or run["stdout"] or "llmdbenchmark failed")
            run["rate_stage_results"] = [
                {
                    "rate": stage.rate,
                    "duration": stage.duration,
                    "metrics": _stage_metric_summary(output, stage_index),
                }
                for stage_index, stage in enumerate(spec.stages)
            ]
            run.update(status="succeeded", metrics=_metric_summary(output), finished_at=_now())
        elif run.get("workload_yaml"):
            workload_path = output / "workload.yaml"
            workload_path.write_text(
                _inline_workload_yaml(
                    run["workload_yaml"],
                    model_name,
                    endpoint_url,
                    run.get("sla_targets"),
                    api_key=api_key,
                ),
                encoding="utf-8",
            )
            command = _base_command(output) + ["--workload-file-path", str(workload_path)]
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(benchmark_root),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = await _stream_benchmark_process(
                process, run, namespace=benchmark_namespace, environment=environment
            )
            run["command"] = [
                "llmdbenchmark",
                "--spec",
                run["specification_file"],
                "run",
                "--endpoint-url",
                "<deployment-endpoint>",
                "--model",
                model_name,
                "--namespace",
                execution.namespace or "default",
                "--harness",
                run["harness"],
                "--workload-file-path",
                "<inline-workload.yaml>",
            ]
            run["output"] = str(output)
            for name, value in (("stdout", stdout), ("stderr", stderr)):
                run[f"{name}_truncated"] = (
                    bool(run.get(f"{name}_truncated")) or len(value.decode(errors="replace")) > 12000
                )
            run["stdout"] = stdout.decode(errors="replace")[-12000:]
            run["stderr"] = stderr.decode(errors="replace")[-12000:]
            if process.returncode:
                raise RuntimeError(run["stderr"] or run["stdout"] or "llmdbenchmark failed")
            run.update(status="succeeded", metrics=_metric_summary(output), finished_at=_now())
        else:
            command = _base_command(output) + ["--workload", run["workload"]]
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(benchmark_root),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = await _stream_benchmark_process(
                process, run, namespace=benchmark_namespace, environment=environment
            )
            run["command"] = [
                "llmdbenchmark",
                "--spec",
                run["specification_file"],
                "run",
                "--endpoint-url",
                "<deployment-endpoint>",
                "--model",
                model_name,
                "--namespace",
                execution.namespace or "default",
                "--harness",
                run["harness"],
                "--workload",
                run["workload"],
            ]
            if accelerator_profile:
                run["command"].extend(["--set", f"accelerator.profile={accelerator_profile}"])
            run["command"].extend(["--set", f"storage.workloadPvc.storageClassName={storage_class}"])
            run["output"] = str(output)
            for name, value in (("stdout", stdout), ("stderr", stderr)):
                run[f"{name}_truncated"] = (
                    bool(run.get(f"{name}_truncated")) or len(value.decode(errors="replace")) > 12000
                )
            run["stdout"] = stdout.decode(errors="replace")[-12000:]
            run["stderr"] = stderr.decode(errors="replace")[-12000:]
            if process.returncode:
                raise RuntimeError(run["stderr"] or run["stdout"] or "llmdbenchmark failed")
            run.update(status="succeeded", metrics=_metric_summary(output), finished_at=_now())
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            await _terminate_process_group(process)
        run.update(status="cancelled", finished_at=_now())
        raise
    except Exception as error:
        run.update(status="failed", error=_benchmark_failure_message(str(error), run), finished_at=_now())
        _save(run)
    finally:
        if kv_capture is not None and kv_capture.session is not None and kv_workspace is not None:
            try:
                kv_metrics = _metric_summary(kv_workspace)
            except (OSError, ValueError):
                kv_metrics = {}  # An interrupted report must not prevent probe shutdown.
            await kv_capture.finish(kv_workspace, kv_metrics, run.get("status") == "succeeded")
        # Setup failures must not suppress reads from an already monitored endpoint.
        if observability_started_at:
            observability_finished_at = run.get("finished_at") or _now()
            try:
                observability = await asyncio.wait_for(
                    collect_benchmark_observability(
                        run["deployment_execution_id"],
                        observability_started_at,
                        observability_finished_at,
                    ),
                    timeout=_OBSERVABILITY_COLLECT_TIMEOUT,
                )
                _attach_rate_stage_observability(run, observability)
                normalized_metrics = run.setdefault("metrics", {})
                normalized_metrics["observability"] = observability
                observability_summary = observability.get("summary") or {}
                observability_derived = observability.get("derived") or {}
                router_summary = observability.get("router") or {}

                def router_mean(key: str) -> Any:
                    return (router_summary.get(key) or {}).get("mean")

                def observed_mean(key: str) -> Any:
                    return (observability_summary.get(key) or {}).get("mean")

                derived_evidence = observability.get("evidence") or {}
                role_summary = observability_derived.get("role_summary") or {}
                prefill_role = role_summary.get("prefill") or {}
                decode_role = role_summary.get("decode") or {}
                prefill_pressure = (
                    (prefill_role.get("waiting_requests") or 0) + (prefill_role.get("running_requests") or 0)
                    if prefill_role
                    else None
                )
                decode_pressure = (
                    (decode_role.get("waiting_requests") or 0) + (decode_role.get("running_requests") or 0)
                    if decode_role
                    else None
                )
                balance_indicator = (
                    (
                        "prefill-bound"
                        if prefill_pressure > decode_pressure
                        else "decode-bound"
                        if decode_pressure > prefill_pressure
                        else "balanced"
                    )
                    if isinstance(prefill_pressure, (int, float)) and isinstance(decode_pressure, (int, float))
                    else None
                )
                mechanism_metrics = {
                    key: value
                    for key, value in {
                        "prefix_cache_hit_rate": (observability_summary.get("prefix_cache_hit_percent") or {}).get(
                            "mean"
                        ),
                        "effective_prefix_hit_rate": next(
                            (
                                value
                                for value in (
                                    observability_derived.get("avoided_prefill_ratio"),
                                    (observability_summary.get("external_prefix_cache_hit_percent") or {}).get("mean"),
                                    (observability_summary.get("prefix_cache_hit_percent") or {}).get("mean"),
                                )
                                if isinstance(value, (int, float))
                            ),
                            None,
                        ),
                        "hbm_hit_rate": observed_mean("prefix_cache_hit_percent"),
                        "lower_tier_hit_rate": observed_mean("external_prefix_cache_hit_percent"),
                        "miss_rate": next(
                            (
                                100 - value
                                for value in (
                                    observability_derived.get("avoided_prefill_ratio"),
                                    observed_mean("external_prefix_cache_hit_percent"),
                                    observed_mean("prefix_cache_hit_percent"),
                                )
                                if isinstance(value, (int, float))
                            ),
                            None,
                        ),
                        "effective_cached_prompt_fraction": observability_derived.get("avoided_prefill_ratio"),
                        "cached_prompt_fraction": observability_derived.get("avoided_prefill_ratio"),
                        "prefill_recomputation_avoided": observability_derived.get("avoided_prefill_ratio"),
                        "recomputed_prompt_tokens": observability_derived.get("recomputed_prefill_tokens"),
                        "recomputed_prefill_tokens": observability_derived.get("recomputed_prefill_tokens"),
                        "pd_decision_ratio": observability_derived.get("pd_decision_ratio"),
                        "kv_transfer_success_rate": observability_derived.get("kv_transfer_success_rate"),
                        "kv_transfer_failure_rate": observed_mean("nixl_failed_transfer_rate_rps"),
                        "kv_transfer_success_failure": {
                            key: value
                            for key, value in {
                                "success_rate_percent": observability_derived.get("kv_transfer_success_rate"),
                                "failed_transfers_per_second": observed_mean("nixl_failed_transfer_rate_rps"),
                                "failed_notifications_per_second": observed_mean("nixl_failed_notification_rate_rps"),
                            }.items()
                            if isinstance(value, (int, float))
                        },
                        "kv_transfer_timeout_errors": observed_mean("nixl_expired_request_rate_rps"),
                        "kv_handoff_status": "active · successful transfers observed"
                        if isinstance(observed_mean("nixl_transfer_rate_rps"), (int, float))
                        and observed_mean("nixl_transfer_rate_rps") > 0
                        else None,
                        "transfer_latency_p50_p95_p99": {
                            key: value
                            for key, value in {
                                "p50_ms": observed_mean("nixl_transfer_latency_p50_ms"),
                                "p95_ms": observed_mean("nixl_transfer_latency_p95_ms"),
                                "p99_ms": observed_mean("nixl_transfer_latency_p99_ms"),
                            }.items()
                            if isinstance(value, (int, float))
                        },
                        "transfer_bandwidth": observed_mean("nixl_transfer_bytes_per_second"),
                        "prefill_endpoint_distribution": observability_derived.get("prefill_endpoint_distribution"),
                        "decode_endpoint_distribution": observability_derived.get("decode_endpoint_distribution"),
                        "index_admissions": router_mean("index_admissions_per_second"),
                        "index_evictions": router_mean("index_evictions_per_second"),
                        "index_lookups": router_mean("index_lookups_per_second"),
                        "matched_blocks_lookup": observability_derived.get("matched_blocks_per_lookup"),
                        "index_lookup_latency": router_mean("index_lookup_latency_p95_ms"),
                        "index_entry_count": router_mean("index_entry_count"),
                        "kv_event_rate": router_mean("kv_event_rate_rps"),
                        "kv_event_errors": router_mean("kv_event_error_rate_rps"),
                        "kv_event_subscriber_coverage": observability_derived.get("kv_event_subscriber_coverage"),
                        "active_subscribers": router_mean("active_subscribers"),
                        "subscriber_recovery": router_mean("subscriber_reconnect_rate_rps"),
                        "precise_index_health": (
                            "healthy · lookup activity observed"
                            if isinstance(router_mean("index_lookups_per_second"), (int, float))
                            and router_mean("index_lookups_per_second") > 0
                            and not (
                                isinstance(router_mean("kv_event_error_rate_rps"), (int, float))
                                and router_mean("kv_event_error_rate_rps") > 0
                            )
                            else None
                        ),
                        "offloaded_bytes": observability_derived.get("offloaded_bytes"),
                        "restored_bytes": observability_derived.get("restored_bytes"),
                        "offload_bandwidth": observed_mean("kv_offload_bytes_per_second"),
                        "restore_bandwidth": observed_mean("kv_restore_bytes_per_second"),
                        "hbm_cache_utilization": observed_mean("gpu_cache_usage_percent")
                        or observed_mean("kv_cache_usage_percent"),
                        "cpu_cache_utilization": observed_mean("cpu_cache_usage_percent"),
                        "epp_scheduler_latency": router_mean("scheduler_latency_p95_ms"),
                        "plugin_latency": router_mean("plugin_latency_p95_ms"),
                    }.items()
                    if value not in (None, [], {})
                }
                system_metrics = {
                    key: value
                    for key, value in {
                        "per_pod_requests_s": observability.get("per_pod"),
                        "per_pod_output_tokens_s": observability.get("per_pod"),
                        "in_flight_tokens": observability.get("per_endpoint"),
                        "recomputed_tokens_s": observability_derived.get("recomputed_token_rate_tps"),
                        "prefill_accelerator_utilization": prefill_role.get("gpu_utilization_percent"),
                        "prefill_running_requests": prefill_role.get("running_requests"),
                        "prefill_queue": prefill_role.get("waiting_requests"),
                        "prefill_pressure": prefill_pressure,
                        "decode_accelerator_utilization": decode_role.get("gpu_utilization_percent"),
                        "decode_active_requests": decode_role.get("running_requests"),
                        "decode_kv_utilization": decode_role.get("kv_cache_usage_percent"),
                        "decode_pressure": decode_pressure,
                        "pd_balance_gap": abs(prefill_pressure - decode_pressure)
                        if isinstance(prefill_pressure, (int, float)) and isinstance(decode_pressure, (int, float))
                        else None,
                        "prefill_bound_decode_bound_indicator": balance_indicator,
                        "accelerator_utilization": observed_mean("gpu_utilization_percent"),
                        "hbm_usage": observed_mean("gpu_memory_usage_bytes")
                        or observed_mean("gpu_framebuffer_used_bytes"),
                        "cpu_memory_usage": observed_mean("cpu_memory_usage_bytes"),
                        "cpu_cache_usage": observed_mean("cpu_cache_usage_percent"),
                        "cpu_utilization": observability.get("per_pod"),
                        "transfer_bandwidth": observed_mean("nixl_transfer_bytes_per_second"),
                        "filesystem_read_write_latency": observability.get("per_pod"),
                        "queue_depth": observed_mean("queue_depth"),
                        "restore_traffic": observed_mean("kv_restore_bytes_per_second"),
                    }.items()
                    if value not in (None, [], {})
                }
                normalized_metrics.update(
                    {
                        key: value
                        for key, value in {
                            "prefix_cache_hit_rate": (observability_summary.get("prefix_cache_hit_percent") or {}).get(
                                "mean"
                            ),
                            "token_load_cv": observability_derived.get("token_load_cv"),
                            "request_destination_distribution": observability_derived.get(
                                "request_destination_distribution"
                            ),
                            "avoided_prefill_ratio": observability_derived.get("avoided_prefill_ratio"),
                            "recomputed_prefill_tokens": observability_derived.get("recomputed_prefill_tokens"),
                            "per_pod": observability.get("per_pod"),
                            "per_endpoint": observability.get("per_endpoint"),
                            "routing_evidence": {
                                "router": observability.get("router") or {},
                                "availability": observability.get("availability") or {},
                                "metric_sources": observability.get("metric_sources") or {},
                            },
                            "evidence_contract": derived_evidence,
                            "mechanism_metrics": mechanism_metrics,
                            "system_metrics": system_metrics,
                        }.items()
                        if value not in (None, [], {})
                    }
                )
                run["monitoring"] = {
                    **run.get("monitoring", {}),
                    "status": observability["status"],
                    "window": observability["window"],
                    "message": observability.get("reason"),
                }
            except Exception as monitoring_error:
                run["monitoring"] = {
                    **run.get("monitoring", {}),
                    "status": "unavailable",
                    "message": str(monitoring_error) or "Benchmark observability collection timed out",
                }
        if run.get("monitoring"):
            run.setdefault("metrics", {}).setdefault(
                "observability",
                {
                    "status": "unavailable",
                    "reason": run["monitoring"].get("message"),
                    "series": [],
                    "summary": {},
                },
            )
        # Persist historical evidence before releasing any benchmark storage.
        # Deployment/PVC cleanup failures must not discard collected monitoring.
        if observability_started_at and run.get("cluster_session_id"):
            try:
                run["resource_snapshot"] = await asyncio.wait_for(
                    _kubernetes_resource_snapshot(
                        run["cluster_session_id"],
                        benchmark_namespace,
                    ),
                    timeout=30.0,
                )
            except Exception as snapshot_error:
                run["resource_snapshot_error"] = str(snapshot_error) or "Resource snapshot timed out"
        _enrich_benchmark_result(run)
        _save(run)
        if benchmark_namespace and environment:
            await _cleanup_benchmark_storage(benchmark_namespace, benchmark_volume, environment)
        _tasks.pop(run_id, None)


async def _execute_workflow(workflow_id: str) -> None:
    workflow = _get("workflow", workflow_id)
    if workflow is None:
        return
    _active_workflows[workflow_id] = workflow
    try:
        while True:
            deployment = _store.get_run(workflow["deployment_run_id"])
            if deployment is None:
                raise ValueError("deployment run is no longer available")
            ready_case = next(
                (case for case in deployment.cases if case.status.value == "ready" and case.execution_id), None
            )
            if ready_case:
                evaluation_id = workflow.get("evaluation_run_id")
                if not evaluation_id:
                    evaluation = await create_run(
                        EvaluateRunRequest(
                            deployment_execution_id=ready_case.execution_id,
                            harness=workflow["harness"],
                            workload=workflow["workload"],
                            parallelism=workflow["parallelism"],
                            benchmark_source=workflow.get("benchmark_source"),
                        )
                    )
                    _link_evaluation_benchmark(workflow, workflow, evaluation)
                    evaluation_id = evaluation["id"]
                    workflow.update(status="benchmarking", evaluation_run_id=evaluation_id)
                    _save(workflow)
                while True:
                    evaluated = _get("benchmark", evaluation_id)
                    if evaluated is None:
                        raise ValueError("evaluation run is no longer available")
                    if evaluated and evaluated["status"] in _TERMINAL_EVALUATE_STATUSES:
                        workflow.update(
                            status=evaluated["status"],
                            metrics=evaluated.get("metrics", {}),
                            error=evaluated.get("error"),
                            finished_at=_now(),
                        )
                        return
                    await asyncio.sleep(3)
            if deployment.status.value in {"failed", "cancelled", "cleaned"}:
                raise ValueError(f"deployment ended with status {deployment.status.value}")
            await asyncio.sleep(3)
    except asyncio.CancelledError:
        workflow.update(
            status="cancelling" if workflow.get("status") == "cancelling" else "cancelled", finished_at=_now()
        )
        raise
    except Exception as error:
        workflow.update(status="failed", error=str(error), finished_at=_now())
    finally:
        _save(workflow)
        _workflow_tasks.pop(workflow_id, None)
        _active_workflows.pop(workflow_id, None)


@router.post(
    "/runs",
    status_code=202,
    summary="Start a benchmark run against a ready deployment execution or a published Model Service.",
    description=(
        "Start a benchmark using the same runner, monitoring, KV collection and analysis as deployment workflows. "
        "Set exactly one of deployment_execution_id (a specific deployment) or model_service_group_id (a published "
        "Model Service; resolves to one of its currently healthy, authorized members and routes through the "
        "cluster's shared Gateway using its published name and api_key, the same path real clients use -- "
        "404 if missing, 409 if no member is currently healthy/authorized). "
        "Requires a ready execution and its active cluster session (404 if missing; 409 if not ready or the session "
        "is invalid). Reuses healthy Prometheus targets, otherwise attempts monitor/RBAC setup with a 30-second total "
        "preparation deadline; missing monitoring does not fail the benchmark. Reads existing KV instrumentation "
        "when supported; never installs probes or redeploys the model. Persists deployment facts and completion-time "
        "resources without taking lifecycle ownership of an existing deployment. Returns the queued run with HTTP 202."
    ),
    operation_id="create_evaluate_run",
    responses=evaluation_problem_responses(401, 403, 404, 409, 422),
    response_model=BenchmarkRunResponse,
    response_model_exclude_unset=True,
)
async def create_run(request: EvaluateRunRequest, http_request: Request = None) -> dict:
    model_service_published_name: str | None = None
    deployment_execution_id = request.deployment_execution_id
    if request.model_service_group_id:
        deployment_execution_id, model_service_published_name = resolve_model_service_target(
            request.model_service_group_id, http_request
        )
    execution = _store.get_execution(deployment_execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="deployment execution not found")
    if execution.status != DeploymentStatus.READY or execution.endpoint is None:
        raise HTTPException(status_code=409, detail="deployment execution must be ready before evaluation")
    session_id = execution.provenance.get("cluster_session_id")
    if request.cluster_session_id and request.cluster_session_id != session_id:
        raise HTTPException(status_code=409, detail="evaluation session must match the deployment session")
    try:
        if session_id:
            deployment_runtime_overrides(session_id)
            benchmark_runtime = _cluster_benchmark_runtime(session_id)
        else:
            raise ValueError("benchmark requires an explicit active cluster session")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    run = {
        "kind": "benchmark",
        "id": str(uuid4()),
        "status": "queued",
        "created_at": _now(),
        **request.model_dump(),
        "deployment_execution_id": deployment_execution_id,
        "model_service_published_name": model_service_published_name,
        "cluster_session_id": session_id,
        "deployment_ownership": "existing-endpoint",
        "cluster_id": execution.provenance.get("cluster_server_id") or sessions.get_session(session_id).server_id,
        "owner_user_id": owner_for_create(current_principal(http_request)),
        # The request's repository/revision is retained as user intent for
        # compatibility, but execution always uses the cluster-owned checkout.
        "benchmark_runtime": benchmark_runtime,
    }
    run["benchmark"] = request.model_dump(include=set(BenchmarkSpec.model_fields))
    if request.api_key:
        # Run-only, in-memory: never part of the persisted run dict.
        _run_api_keys[run["id"]] = request.api_key
    _hydrate_benchmark_context(run)
    if "specification_file" not in request.model_fields_set:
        guide = run.get("guide")
        run["specification_file"] = os.environ.get("LLM_D_BENCH_SPECIFICATION_FILE") or (
            f"guides/{guide}" if guide and guide != "baseline-vllm" else "guides/optimized-baseline"
        )
    run["benchmark"]["specification_file"] = run["specification_file"]
    _save(run)
    _tasks[run["id"]] = asyncio.create_task(_execute(run["id"]))
    return run


@router.post(
    "/workflow-runs",
    status_code=202,
    summary=(
        "Start a simple deployment-plus-benchmark workflow that first creates an optimized baseline deployment, "
        "then benchmarks it."
    ),
    description=(
        "Start a simple deployment-plus-benchmark workflow that first creates an optimized baseline deployment, "
        "then benchmarks it. Use this for quick end-to-end workflow tests."
    ),
    operation_id="create_evaluate_workflow_run",
    responses=evaluation_problem_responses(401, 403, 409, 422),
    response_model=EvaluationWorkflowResponse,
    response_model_exclude_unset=True,
)
async def create_workflow_run(request: EvaluateWorkflowRequest, http_request: Request = None) -> dict:
    try:
        deployment_runtime_overrides(request.cluster_session_id)
        benchmark_runtime = _cluster_benchmark_runtime(request.cluster_session_id)
        content = {
            "model": {"name": request.model},
            "decode": {"replicaCount": request.replicas, "tensorParallelSize": request.tensor_parallel_size},
            "runtime": {**({"image": request.image} if request.image else {})},
        }
        configuration = DeployableConfiguration(
            type="optimized-baseline",
            provider_ref="optimized-baseline",
            format="helm",
            content=content,
            checksum=configuration_checksum(content),
        )
        deployment = await deployment_run_manager.start_run(
            DeploymentRunCreateRequest(
                configurations=[configuration],
                provenance={"cluster_session_id": request.cluster_session_id, "evaluate_workflow": True},
            ),
            owner_user_id=owner_for_create(current_principal(http_request)),
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    workflow = {
        "kind": "workflow",
        "deployment_ownership": "evaluation",
        "id": str(uuid4()),
        "status": "deploying",
        "created_at": _now(),
        "deployment_run_id": deployment.id,
        "cluster_id": getattr(require_active_session(request.cluster_session_id), "server_id", None),
        "owner_user_id": owner_for_create(current_principal(http_request)),
        "model": request.model,
        "harness": request.harness,
        "workload": request.workload,
        "parallelism": request.parallelism,
        "benchmark_runtime": benchmark_runtime,
    }
    _save(workflow)
    _workflow_tasks[workflow["id"]] = asyncio.create_task(_execute_workflow(workflow["id"]))
    return workflow


@router.post(
    "/evaluations",
    status_code=202,
    summary=(
        "Start a multi-configuration evaluation workflow that can deploy, benchmark, compare, and optionally "
        "baseline several published configuration artifacts."
    ),
    description=(
        "Queue deployment, benchmarking and comparison of published configuration artifacts. Independently deployed "
        "PD baselines default to the total prefill plus decode GPU budget divided by the effective baseline TP "
        "(source decode TP unless overridden). Explicit baseline replicas take precedence, including unequal budgets; "
        "same-pod Kubernetes Service comparisons do not create replicas. Returns 409 before queueing if an automatic "
        "PD budget cannot be represented exactly with 1-32 replicas; existing evaluation snapshots are unchanged."
    ),
    operation_id="create_evaluation",
    responses=evaluation_problem_responses(401, 403, 409, 422, 503),
    response_model=EvaluationWorkflowResponse,
    response_model_exclude_unset=True,
)
async def create_evaluation(request: EvaluationCreateRequest, http_request: Request = None) -> dict:
    try:
        deployment_runtime_overrides(request.cluster_session_id)
        session = require_active_session(request.cluster_session_id)
        deployment_source = cluster_deployment_source(session)
        benchmark_runtime = _cluster_benchmark_runtime(request.cluster_session_id)
        artifacts = {}
        for plan in request.benchmark_plans:
            artifact_id = plan.configuration_artifact_id
            artifact = get_configuration_artifact(artifact_id)
            if artifact is None:
                raise ValueError(f"configuration artifact is not available for benchmark plan {plan.id}")
            _validate_evaluation_capacity(artifact.deployable_configuration)
            if "kubernetes-service" in plan.baseline_types and not provider_supports(
                artifact.deployable_configuration.provider_ref, "supports_kubernetes_service_baseline"
            ):
                raise ValueError(
                    f"kubernetes-service baseline is not supported for "
                    f"{artifact.deployable_configuration.provider_ref} configurations"
                )
            artifacts[artifact_id] = artifact
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    workflow = {
        "kind": "workflow",
        "deployment_ownership": "evaluation",
        "id": str(uuid4()),
        "name": request.name,
        "status": "queued",
        "created_at": _now(),
        "cluster_session_id": request.cluster_session_id,
        "cluster_id": getattr(session, "server_id", None),
        "owner_user_id": owner_for_create(current_principal(http_request)),
        "benchmark_runtime": benchmark_runtime,
        "deployment_source": deployment_source,
        "runtime": request.runtime.model_dump(mode="json"),
        "configuration_artifacts": {
            artifact_id: artifact.deployable_configuration.checksum for artifact_id, artifact in artifacts.items()
        },
        "benchmark_plans": [plan.model_dump(mode="json") for plan in request.benchmark_plans],
        "compare_configurations": request.compare_configurations,
        "cases": _evaluation_cases(request, artifacts),
        "report": None,
    }
    for case in workflow["cases"]:
        if case["kind"] != "baseline":
            continue
        if case.get("baseline_type") == "kubernetes-service":
            # Reuses its sibling Guide case's own deployment execution instead of
            # provisioning a second stack -- see _execute_shared_pods_baseline_case.
            continue
        source_plan = next(
            plan
            for plan in request.benchmark_plans
            if plan.id == case.get("benchmark_plan_id") or plan.id in case["workload_ids"]
        )
        source_artifact_id = source_plan.configuration_artifact_id
        source_artifact = artifacts[source_artifact_id]
        try:
            baseline = _baseline_configuration(
                source_artifact.deployable_configuration,
                case.get("baseline_type", "direct-vllm"),
                case["workload_ids"],
                source_artifact_id,
                case.get("baseline_parameters"),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        case["deployment_configuration"] = baseline.model_dump(mode="json")
    try:
        _save(workflow)
        _workflow_tasks[workflow["id"]] = asyncio.create_task(_execute_evaluation(workflow["id"]))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise HTTPException(status_code=503, detail=f"evaluation could not be queued: {error}") from error
    return workflow


@router.post(
    "/workflow-runs/{workflow_id}/retry",
    status_code=202,
    summary="Retry a failed or cancelled multi-workload evaluation workflow.",
    description=(
        "Retry a failed or cancelled multi-workload evaluation workflow. "
        "Use this after fixing a transient cluster or runtime problem."
    ),
    operation_id="retry_evaluation_workflow",
    responses=evaluation_problem_responses(401, 403, 404, 409, 422),
    response_model=EvaluationWorkflowResponse,
    response_model_exclude_unset=True,
)
async def retry_workflow_run(workflow_id: str) -> dict:
    workflow = _get("workflow", workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="evaluate workflow run not found")
    if workflow.get("status") not in {"failed", "cancelled"} or "cases" not in workflow:
        raise HTTPException(
            status_code=409, detail="only failed or cancelled multi-workload evaluations can be retried"
        )
    try:
        _rebind_retry_session(workflow)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=f"Evaluation cannot be retried: {error}") from error
    for case in workflow["cases"]:
        case.pop("cancel_requested", None)
    # A failed same-pod baseline can only resume without rerunning its candidate when that
    # candidate's execution was explicitly preserved and is still Ready. Otherwise reset both
    # cases so retry recreates the pods, reruns the routed candidate, then performs the genuine
    # Kubernetes Service round-robin comparison on that new identical execution.
    for baseline in (
        item
        for item in workflow["cases"]
        if item.get("baseline_type") == "kubernetes-service" and item.get("status") != "succeeded"
    ):
        guide = next(
            (item for item in workflow["cases"] if item["id"] == baseline.get("dependent_guide_case_id")),
            None,
        )
        execution = _store.get_execution(guide.get("execution_id")) if guide and guide.get("execution_id") else None
        if guide and (execution is None or execution.status != DeploymentStatus.READY):
            guide.update(
                status="queued",
                cancel_requested=False,
                error=None,
                deployment_run_id=None,
                evaluation_run_id=None,
                deployment_case_id=None,
                execution_id=None,
                namespace=None,
                endpoint=None,
            )
    for case in workflow["cases"]:
        if case["status"] == "succeeded":
            continue
        if case["kind"] == "baseline" and case.get("baseline_type") == "kubernetes-service":
            # Reuses its sibling Guide case's deployment execution -- nothing to resolve here;
            # _execute_shared_pods_baseline_case re-derives it fresh on the next run.
            case.update(
                status="queued",
                error=None,
                evaluation_run_id=None,
                execution_id=None,
                namespace=None,
                endpoint=None,
            )
            continue
        deployment, ready_case = _ready_deployment_case(workflow_id, case)
        if ready_case is not None:
            case.update(
                status="benchmarking",
                error=None,
                evaluation_run_id=None,
                deployment_run_id=deployment.id,
                deployment_case_id=ready_case.id,
                execution_id=ready_case.execution_id,
            )
        else:
            try:
                _case_configuration(workflow, case)
            except ValueError as error:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Case {case['id']} cannot be retried: {error}. "
                        "Publish the intended configuration again and create a new evaluation; "
                        "the historical run has not been changed."
                    ),
                ) from error
            case.update(
                status="queued",
                cancel_requested=False,
                error=None,
                deployment_run_id=None,
                evaluation_run_id=None,
                deployment_case_id=None,
                execution_id=None,
                namespace=None,
                endpoint=None,
            )
    workflow.update(status="queued", error=None, report=None, finished_at=None)
    _save(workflow)
    _workflow_tasks[workflow_id] = asyncio.create_task(_execute_evaluation(workflow_id))
    return workflow


_cancellation_locks: dict[str, asyncio.Lock] = {}


def _deployment_owned(workflow: dict, case: dict) -> bool:
    # Standalone /runs always borrow endpoints, even if that service was originally
    # created by some other evaluation. Only this workflow's recorded links own it.
    return (
        workflow.get("deployment_ownership", "evaluation") == "evaluation"
        and case.get("deployment_ownership", "evaluation") == "evaluation"
    )


def _cancel_targets(workflow: dict, case_id: str | None) -> list[dict]:
    cases = workflow.get("cases") or [workflow]
    if case_id is None:
        return cases
    selected = next((case for case in cases if case["id"] == case_id), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="evaluation case not found")
    targets = [selected]
    # Same-pod baselines and suite scenarios cannot keep using a removed service.
    while True:
        ids = {case["id"] for case in targets}
        deployments = {case.get("deployment_run_id") for case in targets if _deployment_owned(workflow, case)} - {None}
        linked = [
            case
            for case in cases
            if case not in targets
            and (
                case.get("dependent_guide_case_id") in ids
                or case["id"] in {item.get("dependent_guide_case_id") for item in targets}
                or (_deployment_owned(workflow, case) and case.get("deployment_run_id") in deployments)
            )
        ]
        if not linked:
            return targets
        targets.extend(linked)


async def _cancel_evaluation(workflow: dict, case_id: str | None = None) -> dict:
    async with _cancellation_locks.setdefault(workflow["id"], asyncio.Lock()):
        workflow = _active_workflows.get(workflow["id"]) or _get("workflow", workflow["id"]) or workflow
        _restore_deployment_links(workflow)
        targets = _cancel_targets(workflow, case_id)
        parent = _workflow_tasks.get(workflow["id"])
        interrupt_parent = (
            case_id is None
            or workflow.get("active_case_id") in {case["id"] for case in targets}
            or (workflow.get("status") == "cancelling" and workflow.get("cancellation_scope") == case_id)
        )
        if interrupt_parent:
            workflow["cancellation_scope"] = case_id
        was_running = parent is not None and not parent.done()
        workflow["status"] = "cancelling" if interrupt_parent else workflow["status"]
        tasks = []
        for case in targets:
            case["cancel_requested"] = True
            if case.get("status") not in _TERMINAL_EVALUATE_STATUSES:
                case["status"] = "cancelling"
            run_id = case.get("evaluation_run_id")
            run = _get("benchmark", run_id) if run_id else None
            if run and run.get("status") not in _TERMINAL_EVALUATE_STATUSES:
                run["status"] = "cancelling"
                _save(run)
            task = _tasks.get(run_id)
            if task and not task.done():
                task.cancel()
                tasks.append(task)
        if interrupt_parent and was_running:
            parent.cancel()
            tasks.append(parent)
        _save(workflow)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _restore_deployment_links(workflow)
        targets = _cancel_targets(workflow, case_id)
        for case in targets:
            case["cancel_requested"] = True
        # Re-read links after awaiting provisioning cancellation: creation can finish
        # while the cancellation request is arriving.
        deployment_ids = {case.get("deployment_run_id") for case in targets if _deployment_owned(workflow, case)} - {
            None
        }
        errors = []
        for deployment_id in deployment_ids:
            try:
                await deployment_run_manager.worker_for_run(deployment_id).cancel_evaluation_deployment(deployment_id)
            except Exception as error:
                errors.append(f"{deployment_id}: {error}")
        for case in targets:
            run_id = case.get("evaluation_run_id")
            run = _get("benchmark", run_id) if run_id else None
            if run and run.get("status") not in _TERMINAL_EVALUATE_STATUSES:
                run.update(status="cancelled", finished_at=_now())
                _save(run)
            if case.get("status") not in _TERMINAL_EVALUATE_STATUSES:
                case.update(status="cancelling" if errors else "cancelled", finished_at=None if errors else _now())
        if errors:
            workflow["cancellation_error"] = "; ".join(errors)
            _save(workflow)
            raise HTTPException(
                status_code=409,
                detail=f"Benchmark stopped; deployment cleanup must be retried: {workflow['cancellation_error']}",
            )
        workflow.pop("cancellation_error", None)
        workflow.pop("cancellation_scope", None)
        if interrupt_parent:
            workflow.update(status="cancelled", active_case_id=None, finished_at=_now())
            if case_id is not None and any(
                case.get("status") not in _TERMINAL_EVALUATE_STATUSES for case in workflow.get("cases", [])
            ):
                workflow.update(status="running", finished_at=None)
                _workflow_tasks[workflow["id"]] = asyncio.create_task(_execute_evaluation(workflow["id"]))
        _save(workflow)
        return {"id": case_id or workflow["id"], "status": "cancelled"}


@router.post(
    "/workflow-runs/{workflow_id}/cases/{case_id}/cancel",
    summary="Cancel an evaluation case and clean up its owned deployments.",
    description=(
        "Stop the selected case and linked cases that depend on or share its owned deployment, "
        "cancel their benchmarks, "
        "and clean up evaluation-owned deployments. Borrowed endpoints are preserved. Unrelated pending cases can "
        "continue. A 409 response means deployment cleanup needs to be retried."
    ),
    operation_id="cancel_evaluation_case",
    responses=evaluation_problem_responses(401, 403, 404, 409, 422),
    response_model=EvaluationCancelResponse,
    response_model_exclude_unset=True,
)
async def cancel_evaluation_case(workflow_id: str, case_id: str) -> dict:
    workflow = _get("workflow", workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="evaluate workflow run not found")
    return await _cancel_evaluation(workflow, case_id)


@router.post(
    "/workflow-runs/{workflow_id}/cancel",
    summary="Cancel an evaluation workflow and clean up its owned deployments.",
    description=(
        "Cancel the workflow and its child benchmarks, then clean up evaluation-owned deployments while preserving "
        "borrowed endpoints. Returns cancelled after successful cleanup; a 409 response means cleanup must be retried."
    ),
    operation_id="cancel_evaluation_workflow",
    responses=evaluation_problem_responses(401, 403, 404, 409, 422),
    response_model=EvaluationCancelResponse,
    response_model_exclude_unset=True,
)
async def cancel_workflow_run(workflow_id: str) -> dict:
    workflow = _get("workflow", workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="evaluate workflow run not found")
    return await _cancel_evaluation(workflow)


@router.delete(
    "/workflow-runs/{workflow_id}",
    status_code=204,
    summary="Delete a terminal evaluation workflow and any retained child benchmark records it owns.",
    description=(
        "Delete a terminal evaluation workflow and any retained child benchmark records it owns. "
        "Use this after the workflow is finished and its history is no longer needed."
    ),
    operation_id="delete_evaluation_workflow",
    responses=evaluation_problem_responses(401, 403, 404, 409, 422),
)
async def delete_workflow_run(workflow_id: str, request: Request = None) -> None:
    workflow = _get("workflow", workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="evaluate workflow run not found")
    _require_evaluate_record(
        current_principal(request),
        workflow,
        permission="evaluate:run:delete",
        resource_type="evaluate_workflow",
        detail="evaluate workflow run is not accessible",
    )
    if workflow.get("status") not in _TERMINAL_EVALUATE_STATUSES:
        raise HTTPException(status_code=409, detail="active evaluation must be cancelled before deletion")
    deployment_run_ids = {
        deployment_run_id
        for deployment_run_id in [
            workflow.get("deployment_run_id"),
            *(case.get("deployment_run_id") for case in workflow.get("cases", [])),
        ]
        if deployment_run_id
    }
    for deployment_run_id in deployment_run_ids:
        if _store.get_run(deployment_run_id) is None:
            continue
        try:
            await deployment_run_manager.worker_for_run(deployment_run_id).delete_run(deployment_run_id)
        except ValueError as error:
            raise HTTPException(
                status_code=409,
                detail=f"deployment {deployment_run_id} could not be deleted: {error}",
            ) from error
    evaluation_ids = _linked_benchmark_ids(workflow)
    # Retried attempts also belong to the task, even after its case points to a newer run.
    evaluation_ids.update(
        run["id"] for run in _records("benchmark") if run.get("evaluation_workflow_id") == workflow_id
    )
    for evaluation_id in evaluation_ids:
        _delete_benchmark_history(evaluation_id)
    _delete("workflow", workflow_id)


@router.on_event("startup")
async def reconcile_evaluate_runs() -> None:
    """Reconcile persisted work whose in-memory task was lost on restart."""
    active_statuses = {"queued", "running", "cancelling", "deploying", "benchmarking"}
    benchmark_records = _records("benchmark")
    for record in benchmark_records:
        if record.get("status") not in active_statuses:
            continue
        record.update(
            status="cancelled" if record.get("status") == "cancelling" else "failed",
            error="evaluation process was interrupted by a service restart",
            process_logs_incomplete=True,
            finished_at=_now(),
        )
        _save(record)
    for workflow in _records("workflow"):
        if workflow.get("status") == "cancelling":
            # The persisted cancellation error remains visible and retryable.
            with contextlib.suppress(HTTPException):
                await _cancel_evaluation(workflow, workflow.get("cancellation_scope"))
            continue
        if "cases" in workflow:
            if workflow.get("status") in {"queued", "running", "deploying", "benchmarking"}:
                _workflow_tasks[workflow["id"]] = asyncio.create_task(_execute_evaluation(workflow["id"]))
            continue
        if workflow.get("status") not in {"deploying", "benchmarking"}:
            continue
        deployment = _store.get_run(workflow["deployment_run_id"])
        if deployment is None:
            workflow.update(
                status="failed", error="deployment run was unavailable after service restart", finished_at=_now()
            )
            _save(workflow)
            continue
        if deployment.status.value == "queued":
            deployment_run_manager.resume_run(deployment.id)
        elif deployment.status.value == "running" and not any(case.execution_id for case in deployment.cases):
            workflow.update(
                status="failed",
                error="deployment was interrupted before creating an execution",
                finished_at=_now(),
            )
            _save(workflow)
            continue
        _workflow_tasks[workflow["id"]] = asyncio.create_task(_execute_workflow(workflow["id"]))


@router.get(
    "/runs",
    summary="List one-off benchmark runs Lens Evaluate has recorded.",
    description="List one-off benchmark runs Lens Evaluate has recorded.",
    operation_id="list_evaluate_runs",
    responses=evaluation_problem_responses(401, 403, 404, 422),
    response_model=BenchmarkRunListResponse,
    response_model_exclude_unset=True,
)
async def list_runs(request: Request = None) -> dict:
    principal = current_principal(request)
    records = [
        run
        for run in _records("benchmark")
        if _evaluate_record_readable(principal, run, permission="evaluate:run:read", resource_type="evaluate_run")
    ]
    items = []
    for run in sorted(records, key=lambda run: run["created_at"], reverse=True):
        execution = (
            _store.get_execution(run.get("deployment_execution_id")) if run.get("deployment_execution_id") else None
        )
        enriched = dict(run)
        if execution:
            enriched.setdefault(
                "model",
                getattr(execution, "model", None)
                or getattr(execution, "model_name", None)
                or (getattr(execution, "provenance", {}) or {}).get("model"),
            )
            enriched.setdefault("endpoint", execution.forwarded_endpoint or execution.endpoint)
            enriched.setdefault(
                "deployment",
                {
                    "name": run.get("deployment_execution_id"),
                    "namespace": execution.namespace,
                    "endpoint": execution.forwarded_endpoint or execution.endpoint,
                },
            )
        items.append(enriched)
    return {"items": items}


def _linked_benchmark_ids(workflow: dict) -> set[str]:
    return {
        run_id
        for run_id in [
            workflow.get("evaluation_run_id"),
            *(case.get("evaluation_run_id") for case in workflow.get("cases", [])),
        ]
        if run_id
    }


def _delete_benchmark_history(run_id: str) -> None:
    # Delete only the run-owned directory, never a path supplied by an artifact.
    output = _results_root / _identifier(run_id)
    if output.is_symlink():
        output.unlink()
    elif output.exists():
        shutil.rmtree(output)
    _delete("benchmark", run_id)


@router.delete(
    "/runs/{run_id}",
    status_code=204,
    summary="Delete a terminal standalone benchmark and its saved results.",
    description=(
        "Delete a completed, failed, or cancelled standalone benchmark and its saved results directory. Active runs "
        "and runs linked to an evaluation workflow return 409; delete the owning workflow to remove its benchmark "
        "history."
    ),
    operation_id="delete_evaluate_run",
    responses=evaluation_problem_responses(401, 403, 404, 409, 422),
)
async def delete_benchmark_run(run_id: str, request: Request = None) -> None:
    try:
        run = _get("benchmark", run_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if run is None:
        raise HTTPException(status_code=404, detail="evaluate run not found")
    _require_evaluate_record(
        current_principal(request),
        run,
        permission="evaluate:run:delete",
        resource_type="evaluate_run",
        detail="evaluate run is not accessible",
    )
    if run.get("status") not in _TERMINAL_EVALUATE_STATUSES:
        raise HTTPException(status_code=409, detail="active benchmark must be cancelled before deletion")
    if any(
        run_id in _linked_benchmark_ids(workflow) or run.get("evaluation_workflow_id") == workflow["id"]
        for workflow in _records("workflow")
    ):
        raise HTTPException(
            status_code=409, detail="benchmark history belongs to an evaluation task; delete the task instead"
        )
    _delete_benchmark_history(run_id)


@router.get(
    "/runs/{run_id}",
    summary="Get benchmark results and read-only deployment evidence.",
    description=(
        "Return the benchmark record with its existing top-level fields plus saved configuration/model/namespace and "
        "deployment_cases for read-only live links. Terminal results use the same client-metric, request-evidence, "
        "capacity, mechanism and diagnosis enrichment as workflow cases. Resource snapshots are captured at benchmark "
        "completion, not reconstructed from live resources on GET. Legacy runs can recover configuration and derived "
        "results when source evidence still exists, but missing historical telemetry or KV traces are not fabricated. "
        "Invalid identifiers return 422 and missing runs return 404. This endpoint does not grant deployment lifecycle "
        "ownership."
    ),
    operation_id="get_evaluate_run",
    responses=evaluation_problem_responses(401, 403, 404, 422),
    response_model=BenchmarkRunResponse,
    response_model_exclude_unset=True,
)
async def get_run(run_id: str, request: Request = None) -> dict:
    try:
        run = _get("benchmark", run_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if run is None:
        raise HTTPException(status_code=404, detail="evaluate run not found")
    _require_evaluate_record(
        current_principal(request),
        run,
        permission="evaluate:run:read",
        resource_type="evaluate_run",
        detail="evaluate run is not accessible",
    )
    changed = _hydrate_benchmark_context(run)
    if run.get("status") in _TERMINAL_EVALUATE_STATUSES:
        changed = _enrich_benchmark_result(run) or changed
    if changed:
        _save(run)
    return _benchmark_response(run)


@router.get(
    "/workflow-runs",
    summary=(
        "List evaluation workflow runs Lens has recorded, including multi-configuration evaluations and simple "
        "deployment-plus-benchmark workflows."
    ),
    description=(
        "List evaluation workflow runs Lens has recorded, including multi-configuration evaluations and simple "
        "deployment-plus-benchmark workflows."
    ),
    operation_id="list_evaluation_workflows",
    responses=evaluation_problem_responses(401, 403, 404, 422),
    response_model=EvaluationWorkflowListResponse,
    response_model_exclude_unset=True,
)
async def list_workflow_runs(request: Request = None) -> dict:
    principal = current_principal(request)
    allowed = [
        run
        for run in _records("workflow")
        if _evaluate_record_readable(principal, run, permission="evaluate:run:read", resource_type="evaluate_workflow")
    ]
    records = sorted(allowed, key=lambda run: run["created_at"], reverse=True)
    items = []
    for workflow in records:
        response = dict(workflow)
        if "cases" in workflow:
            response["cases"] = [{**case, "configuration": _configuration_facts(case)} for case in workflow["cases"]]
        items.append(response)
    return {"items": items}


@router.get(
    "/workflow-runs/{workflow_id}",
    summary="Get the current top-level record for one evaluation workflow run.",
    description="Get the current top-level record for one evaluation workflow run.",
    operation_id="get_evaluation_workflow",
    responses=evaluation_problem_responses(401, 403, 404, 422),
    response_model=EvaluationWorkflowResponse,
    response_model_exclude_unset=True,
)
async def get_workflow_run(workflow_id: str, request: Request = None) -> dict:
    try:
        workflow = _get("workflow", workflow_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if workflow is None:
        raise HTTPException(status_code=404, detail="evaluate workflow run not found")
    _require_evaluate_record(
        current_principal(request),
        workflow,
        permission="evaluate:run:read",
        resource_type="evaluate_workflow",
        detail="evaluate workflow run is not accessible",
    )
    return workflow


@router.get(
    "/workflow-runs/{workflow_id}/details",
    summary="Get expanded evaluation workflow results and deployment links.",
    description=(
        "Return workflow cases, linked benchmark records, deployment evidence and comparison reports. Benchmark "
        "records and case results use the same client-metric, request-evidence, capacity, mechanism and diagnosis "
        "pipeline as standalone runs. Case resource snapshots are saved benchmark-completion evidence; "
        "deployment_cases "
        "may additionally contain current Kubernetes snapshots. Missing historical telemetry is not reconstructed from "
        "current samples. Missing workflows return 404; malformed identifiers return 422."
    ),
    operation_id="get_evaluation_workflow_details",
    responses=evaluation_problem_responses(401, 403, 404, 422),
    response_model=EvaluationDetailsResponse,
    response_model_exclude_unset=True,
)
async def get_workflow_run_details(workflow_id: str, request: Request = None) -> dict:
    try:
        workflow = _get("workflow", workflow_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if workflow is None:
        raise HTTPException(status_code=404, detail="evaluate workflow run not found")
    _require_evaluate_record(
        current_principal(request),
        workflow,
        permission="evaluate:run:read",
        resource_type="evaluate_workflow",
        detail="evaluate workflow run is not accessible",
    )
    if "cases" in workflow:
        workflow_terminal = workflow.get("status") in _TERMINAL_EVALUATE_STATUSES
        metrics_changed = False
        for case in workflow["cases"]:
            metrics_changed = _enrich_benchmark_result(case) or metrics_changed
        if metrics_changed or _comparison_report_needs_refresh(workflow.get("report")):
            workflow["report"] = _comparison_report(workflow)
            _save(workflow)
        if not workflow_terminal and _restore_deployment_links(workflow):
            _save(workflow)
        for evaluation_case in workflow["cases"] if not workflow_terminal else []:
            if evaluation_case.get("error") != "The linked deployment record was deleted":
                continue
            for dependent_case in workflow["cases"]:
                if dependent_case is evaluation_case or dependent_case.get("status") not in {"queued", "deploying"}:
                    continue
                dependent_case.update(
                    status="cancelled",
                    error="Not started because a required deployment record was deleted",
                )
                _save(workflow)
        case_details = []
        for evaluation_case in workflow["cases"]:
            response_case = dict(evaluation_case)
            if "spec" not in evaluation_case:
                response_case["configuration"] = _configuration_facts(evaluation_case)
            deployment = (
                _store.get_run(evaluation_case["deployment_run_id"])
                if evaluation_case.get("deployment_run_id")
                else None
            )
            deployment_warning = None
            if evaluation_case.get("deployment_run_id") and deployment is None:
                deployment_warning = (
                    "The linked deployment record is no longer available; the recorded evaluation result is unchanged."
                )
            deployment_cases = []
            if deployment:
                for case in deployment.cases:
                    execution = _store.get_execution(case.execution_id) if case.execution_id else None
                    resource_snapshot = await _kubernetes_resource_snapshot(
                        workflow.get("cluster_session_id"),
                        execution.namespace if execution else None,
                    )
                    resource_snapshot = resource_snapshot or evaluation_case.get("resource_snapshot")
                    deployment_cases.append(
                        {
                            "id": case.id,
                            "status": case.status,
                            "guide": case.provider_ref,
                            "attempt": case.attempt,
                            "failure": case.failure,
                            "execution_id": case.execution_id,
                            "namespace": execution.namespace if execution else None,
                            "endpoint": execution.endpoint.url if execution and execution.endpoint else None,
                            "execution_status": execution.status if execution else None,
                            "monitoring_setup": execution.monitoring_setup if execution else None,
                            "diagnostics": execution.diagnostics if execution else None,
                            "resource_snapshot": resource_snapshot,
                        }
                    )
            benchmark = (
                _get("benchmark", evaluation_case["evaluation_run_id"])
                if evaluation_case.get("evaluation_run_id")
                else None
            )
            if benchmark:
                benchmark = await get_run(benchmark["id"])
            case_details.append(
                {
                    "case": response_case,
                    "deployment": deployment,
                    "deployment_cases": deployment_cases,
                    "deployment_warning": deployment_warning,
                    "evaluation": benchmark,
                }
            )
        return {"workflow": workflow, "cases": case_details, "report": workflow.get("report")}
    deployment = _store.get_run(workflow["deployment_run_id"])
    deployment_cases = []
    if deployment is not None:
        for case in deployment.cases:
            execution = _store.get_execution(case.execution_id) if case.execution_id else None
            resource_snapshot = await _kubernetes_resource_snapshot(
                workflow.get("cluster_session_id"),
                execution.namespace if execution else None,
            )
            resource_snapshot = resource_snapshot or workflow.get("resource_snapshot")
            deployment_cases.append(
                {
                    "id": case.id,
                    "status": case.status,
                    "guide": case.provider_ref,
                    "attempt": case.attempt,
                    "failure": case.failure,
                    "execution_id": case.execution_id,
                    "namespace": execution.namespace if execution else None,
                    "endpoint": execution.endpoint.url if execution and execution.endpoint else None,
                    "execution_status": execution.status if execution else None,
                    "monitoring_setup": execution.monitoring_setup if execution else None,
                    "diagnostics": execution.diagnostics if execution else None,
                    "resource_snapshot": resource_snapshot,
                }
            )
    evaluation = _get("benchmark", workflow["evaluation_run_id"]) if workflow.get("evaluation_run_id") else None
    if evaluation:
        evaluation = await get_run(evaluation["id"])
    return {
        "workflow": workflow,
        "deployment": {
            "id": deployment.id,
            "status": deployment.status,
            "created_at": deployment.created_at,
            "started_at": deployment.started_at,
            "finished_at": deployment.finished_at,
            "cases": deployment_cases,
        }
        if deployment
        else None,
        "evaluation": evaluation,
    }


@router.post(
    "/runs/{run_id}/cancel",
    summary="Cancel an active one-off benchmark run.",
    description=(
        "Cancel an active one-off benchmark run. Use this when a benchmark should stop before its normal completion."
    ),
    operation_id="cancel_evaluate_run",
    responses=evaluation_problem_responses(401, 403, 404, 409, 422),
    response_model=EvaluationCancelResponse,
    response_model_exclude_unset=True,
)
async def cancel_run(run_id: str) -> dict:
    task = _tasks.get(run_id)
    try:
        run = _get("benchmark", run_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if run is None:
        raise HTTPException(status_code=404, detail="evaluate run not found")
    for workflow in _records("workflow"):
        for case in workflow.get("cases") or [workflow]:
            if case.get("evaluation_run_id") == run_id:
                return await _cancel_evaluation(workflow, case["id"] if "cases" in workflow else None)
    async with _cancellation_locks.setdefault(f"benchmark:{run_id}", asyncio.Lock()):
        run = _get("benchmark", run_id) or run
        task = _tasks.get(run_id)
        if run.get("status") == "cancelled":
            return {"id": run_id, "status": "cancelled"}
        if task is None or task.done():
            raise HTTPException(status_code=409, detail="evaluate run is not active")
        run["status"] = "cancelling"
        _save(run)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        run = _get("benchmark", run_id) or run
        run.update(status="cancelled", finished_at=_now())
        _save(run)
        return {"id": run_id, "status": "cancelled"}
