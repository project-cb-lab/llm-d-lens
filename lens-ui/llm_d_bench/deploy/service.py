"""Deploy service backed by registered legacy GuideAdapter providers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from llm_d_bench.common.hashing import stable_hash
from llm_d_bench.deploy.contracts import (
    ConfigurationArtifact,
    DeploymentArtifact,
    DeploymentCreateRequest,
    DeploymentEndpoint,
    DeploymentExecution,
    DeploymentKillRequest,
    DeploymentLogEntry,
    DeploymentLogPage,
    DeploymentLogRequest,
    DeploymentMetadata,
    DeploymentStatus,
    VersionedPayload,
)
from llm_d_bench.deploy.data_plane import resolve_data_plane
from llm_d_bench.deploy.providers.guide_adapter import GuideAdapter, GuideDeploymentArtifact
from llm_d_bench.deploy.providers.guide_catalog import GuideCatalog
from llm_d_bench.deploy.providers.hardware_profile import pin_runtime_image
from llm_d_bench.utils.artifact_store import register_artifacts
from llm_d_bench.utils.paths import storage_path

logger = logging.getLogger(__name__)

NamespaceFactory = Callable[[DeploymentCreateRequest], str]

# Workers build separate service instances for polling. Creation owns readiness
# until deploy and its initial readiness check finish; polling must not race it.
_creating_executions: dict[str, DeploymentExecution] = {}


def _initial_metadata(request: DeploymentCreateRequest) -> DeploymentMetadata:
    return DeploymentMetadata(description=str(request.provenance.get("description") or "").strip())


def _with_default_model_secret(
    deployment_policy: dict[str, Any] | None, model_token: str | None, cluster_id: object
) -> dict[str, Any] | None:
    """Fall back to the cluster's wizard-created HF_TOKEN secret when the
    configuration didn't request one explicitly (see the wizard's mandatory
    HF_TOKEN secret step in CreateClusterWizard.jsx and
    llm_d_bench.cluster.service.create_hf_token_secret). Without this, guides
    whose ``HF_TOKEN`` env is ``optional: true`` deploy successfully but
    later fail at inference/benchmark time for gated models -- see the
    "warm-up run failed ... llm-d-hf-token" class of bug this fixes.

    ``mode: "none"`` (or an empty/missing ``model_secret``) means the caller did
    not choose a token source, so the cluster's recorded secret still applies.
    Only a concrete source (``existing-secret`` / ``host``) or a per-request
    ``model_token`` is left untouched.
    """
    if not isinstance(deployment_policy, dict) or model_token:
        return deployment_policy
    existing = deployment_policy.get("model_secret")
    if existing and (not isinstance(existing, dict) or existing.get("mode") not in (None, "", "none")):
        return deployment_policy
    from llm_d_bench.cluster import registry

    cluster = registry.get_cluster(str(cluster_id)) if cluster_id else None
    if cluster is None or not cluster.hf_token_secret_namespace or not cluster.hf_token_secret_name:
        return deployment_policy
    return {
        **deployment_policy,
        "model_secret": {
            "mode": "existing-secret",
            "sourceNamespace": cluster.hf_token_secret_namespace,
            "sourceName": cluster.hf_token_secret_name,
        },
    }


class ExecutionLogArchive:
    """Append-only deployment diagnostics retained independently of Kubernetes."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._legacy_layout = base_dir is not None
        self._logs_dir = (
            Path(base_dir) / "logs" if base_dir is not None else storage_path("data", "artifacts", "deployments")
        )
        self._contexts: dict[str, dict[str, Any]] = {}
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append_snapshot(self, execution_id: str, phase: str, snapshot: dict[str, Any]) -> None:
        entries = {
            "workload": snapshot.get("modelserver_logs"),
            "events": snapshot.get("events"),
        }
        timestamp = datetime.now(UTC).isoformat()
        with self._lock:
            archive = self._archive_path(execution_id)
            archive.parent.mkdir(parents=True, exist_ok=True)
            existing = self._recorded_messages(archive)
            with archive.open("a", encoding="utf-8") as handle:
                for source, message in entries.items():
                    if not message:
                        continue
                    rendered_message = str(message)
                    if (source, rendered_message) in existing:
                        continue
                    record = {
                        "timestamp": timestamp,
                        "phase": phase,
                        "source": source,
                        "message": rendered_message,
                    }
                    handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._register(execution_id)

    def read_entries(self, execution_id: str) -> list[DeploymentLogEntry]:
        archive = self._archive_path(execution_id)
        if not archive.is_file():
            return []
        entries: list[DeploymentLogEntry] = []
        for line in archive.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                entries.append(
                    DeploymentLogEntry(
                        timestamp=datetime.fromisoformat(record["timestamp"]),
                        source=str(record["source"]),
                        message=str(record["message"]),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return entries

    def has_entries(self, execution_id: str) -> bool:
        archive = self._archive_path(execution_id)
        return archive.is_file() and archive.stat().st_size > 0

    @staticmethod
    def evidence_ref(execution_id: str) -> str:
        return f"deployment-log://{execution_id}/archive"

    def _archive_path(self, execution_id: str) -> Path:
        if not execution_id or Path(execution_id).name != execution_id or execution_id in {".", ".."}:
            raise ValueError("invalid deployment execution id")
        owner = self._logs_dir / execution_id
        if owner.is_symlink():
            raise ValueError("deployment archive owner symlinks are not supported")
        root = owner.resolve()
        if not root.is_relative_to(self._logs_dir.resolve()):
            raise ValueError("deployment logs escape archive root")
        path = root / "entries.jsonl" if self._legacy_layout else root / "logs" / "entries.jsonl"
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("deployment archive symlinks are not supported")
        return path

    def register_execution(self, execution: DeploymentExecution) -> None:
        """Attach lifecycle provenance to persisted, partial diagnostic snapshots."""
        with self._lock:
            self._contexts[execution.execution_id] = {
                "source_version": {
                    "source_ref": execution.artifact.source_ref,
                    "lens": os.environ.get("LENS_SOURCE_VERSION") or "unknown",
                },
                "configuration_ids": execution.artifact.configuration_artifact_ids,
                "status": execution.status.value,
            }
            archive = self._archive_path(execution.execution_id)
            root = archive.parent if self._legacy_layout else archive.parent.parent
            root.mkdir(parents=True, exist_ok=True)
            if execution.diagnostics:
                target = root / "diagnostics.json"
                temporary = target.with_suffix(".tmp")
                temporary.write_text(execution.diagnostics.model_dump_json(indent=2), encoding="utf-8")
                temporary.replace(target)
            self._register(execution.execution_id)

    def _register(self, execution_id: str) -> None:
        archive = self._archive_path(execution_id)
        root = archive.parent if self._legacy_layout else archive.parent.parent
        register_artifacts(
            root,
            owner_type="deployment",
            owner_id=execution_id,
            retention_class="diagnostic",
            truncated=True,
            **self._contexts.get(execution_id, {"status": "running"}),
        )

    @staticmethod
    def _recorded_messages(archive: Path) -> set[tuple[str, str]]:
        if not archive.is_file():
            return set()
        records = set()
        for line in archive.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                records.add((str(record["source"]), str(record["message"])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return records


def _readiness_is_transient(reasons: list[str], diagnostics: dict | None = None) -> bool:
    """Return true for errors that reflect a temporary inability to reach the
    cluster/control-plane rather than an actual workload failure, so a brief
    network blip doesn't permanently flag a healthy deployment as failed."""
    timeout_markers = (
        "kubectl command timed out",
        "helm command timed out",
        "endpoint smoke test timed out",
    )
    connectivity_markers = (
        "was refused",
        "connection refused",
        "connection reset",
        "no route to host",
        "i/o timeout",
        "dial tcp",
        "eof",
        "tls handshake timeout",
        "unable to connect to the server",
        "client.timeout exceeded",
        "context deadline exceeded",
    )
    if diagnostics:
        # Rollout timeouts hide scheduler reasons. Current Pending pods together
        # with allocation events distinguish resource waits from startup failures.
        pods = str(diagnostics.get("pods") or "")
        events = str(diagnostics.get("events") or "")
        if "pending" in pods.lower():
            reasons = [*reasons, pods, events]
    workload_failures = ("crashloopbackoff", "imagepullbackoff", "errimagepull", "oomkilled", "invalid image")
    if any(marker in reason.lower() for reason in reasons for marker in workload_failures):
        return False
    resource_wait = (
        "insufficient ",
        "waiting for resourceclaim",
        "waiting for resource claim",
        "resourceclaim is not allocated",
        "resource claim is not allocated",
    )
    markers = timeout_markers + connectivity_markers + resource_wait
    if any(any(marker in reason.lower() for marker in markers) for reason in reasons):
        return True
    # Kubernetes may acknowledge apply before a following read observes the
    # new Deployment.  Only that exact ``rollout status`` response is treated
    # as transient; other "not found" errors (Secret, PVC, ConfigMap in
    # events) are genuine configuration failures.
    return any(_deployment_not_found(reason) for reason in reasons)


def _deployment_not_found(message: str) -> bool:
    normalized = message.lower()
    return "deployments.apps" in normalized and "not found" in normalized


class DeploymentService(Protocol):
    """Public deploy command and observation boundary."""

    async def create(
        self,
        request: DeploymentCreateRequest,
        *,
        model_token: str | None = None,
        on_initial_execution: Any | None = None,
    ) -> DeploymentExecution: ...

    async def kill(
        self,
        request: DeploymentKillRequest,
        execution: DeploymentExecution,
    ) -> DeploymentExecution: ...

    async def get(self, execution_id: str) -> DeploymentExecution: ...

    def restore_execution(self, execution: DeploymentExecution) -> None: ...

    async def get_logs(self, request: DeploymentLogRequest) -> DeploymentLogPage: ...

    async def refresh_diagnostics(self, execution: DeploymentExecution) -> DeploymentExecution: ...

    async def stop(self, execution: DeploymentExecution) -> DeploymentExecution: ...

    async def clean(self, execution: DeploymentExecution) -> DeploymentExecution: ...


class DeploymentCreationCancelled(asyncio.CancelledError):
    """Cancellation that retains the deployment execution cleaned during create."""

    def __init__(self, execution: DeploymentExecution) -> None:
        super().__init__("deployment creation cancelled")
        self.execution = execution


class GuideAdapterDeploymentService:
    """Compatibility service that delegates deployment lifecycle to GuideAdapter."""

    def __init__(
        self,
        catalog: GuideCatalog,
        namespace_factory: NamespaceFactory,
        log_archive: ExecutionLogArchive | None = None,
    ) -> None:
        self._catalog = catalog
        self._namespace_factory = namespace_factory
        self._log_archive = log_archive or ExecutionLogArchive()
        self._executions: dict[str, DeploymentExecution] = {}
        self._provider_executions: dict[str, tuple[GuideAdapter, GuideDeploymentArtifact, dict[str, Any]]] = {}

    async def create(
        self,
        request: DeploymentCreateRequest,
        *,
        model_token: str | None = None,
        on_initial_execution: Any | None = None,
    ) -> DeploymentExecution:
        execution_id = str(uuid4())
        metadata = _initial_metadata(request)
        artifact_input = self._single_provider_artifact(request.configuration_artifacts)
        adapter = self._catalog.get_adapter(artifact_input.provider_ref)
        artifact = self._deployment_artifact(request, artifact_input)
        if adapter is None:
            return self._store(
                DeploymentExecution(
                    execution_id=execution_id,
                    request_id=request.request_id,
                    status=DeploymentStatus.FAILED,
                    artifact=artifact,
                    configuration_artifacts=request.configuration_artifacts,
                    provenance=request.provenance,
                    metadata=metadata,
                    diagnostics=VersionedPayload(
                        schema_version="deploy-diagnostics.v1",
                        value={"reason": "unsupported_deployment_provider"},
                    ),
                )
            )

        definition = adapter.discover()
        overrides = self._compatibility_overrides(artifact_input)
        # The llm-d model-server image always follows the pinned stack version,
        # whatever tag a stored configuration carries (custom images are left as-is).
        runtime_overrides = overrides.get("runtime")
        if isinstance(runtime_overrides, dict) and isinstance(runtime_overrides.get("image"), str):
            runtime_overrides["image"] = pin_runtime_image(runtime_overrides["image"])
        cluster_snapshot = request.cluster_snapshot.value if request.cluster_snapshot else {}
        validation = adapter.validate_inputs(definition, cluster_snapshot, overrides)
        if not validation.accepted:
            return self._store(
                DeploymentExecution(
                    execution_id=execution_id,
                    request_id=request.request_id,
                    status=DeploymentStatus.FAILED,
                    artifact=artifact,
                    configuration_artifacts=request.configuration_artifacts,
                    provenance=request.provenance,
                    metadata=metadata,
                    diagnostics=VersionedPayload(
                        schema_version="deploy-diagnostics.v1",
                        value={"reasons": validation.reasons},
                    ),
                )
            )

        provider_artifact: GuideDeploymentArtifact | None = None
        provider_execution: dict[str, Any] | None = None
        namespace: str | None = self._namespace_factory(request)
        try:
            overrides["_deployment_namespace"] = namespace
            overrides["_cluster_id"] = request.provenance.get("cluster_server_id")
            provider_artifact = await adapter.render(definition, overrides)
            # The framework resolves the data plane (shared Gateway vs the
            # deployment's own proxy) so providers do not inspect cluster state.
            data_plane, data_plane_warning = await resolve_data_plane(
                provider_artifact.deployment_contract.get("data_plane_kind") or "external",
                provenance=request.provenance,
                cluster_id=request.provenance.get("cluster_server_id"),
            )
            if data_plane_warning:
                logger.warning("Deployment %s: %s", execution_id, data_plane_warning)
            artifact = self._deployment_artifact(request, artifact_input, provider_artifact)
            initial_execution = DeploymentExecution(
                execution_id=execution_id,
                request_id=request.request_id,
                status=DeploymentStatus.DEPLOYING,
                artifact=artifact,
                namespace=namespace,
                configuration_artifacts=request.configuration_artifacts,
                provenance=request.provenance,
                metadata=metadata,
            )
            self._store(initial_execution)
            _creating_executions[execution_id] = initial_execution
            if callable(on_initial_execution):
                with contextlib.suppress(Exception):
                    on_initial_execution(initial_execution)
            provider_execution = await adapter.deploy(
                provider_artifact,
                {
                    "namespace": namespace,
                    "deployment_policy": _with_default_model_secret(
                        request.deployment_policy.value, model_token, request.provenance.get("cluster_server_id")
                    ),
                    "model_token": model_token,
                    "provenance": request.provenance,
                    "runtime": overrides.get("runtime") or {},
                    "data_plane": data_plane,
                    "data_plane_warning": data_plane_warning,
                },
            )
            provider_execution["_artifact"] = provider_artifact
            deployment_snapshot = await adapter.diagnostics(provider_execution)
            self._log_archive.append_snapshot(execution_id, "deployed", deployment_snapshot)
            readiness = await adapter.readiness(provider_execution)
            if not readiness.accepted:
                diagnostics = await adapter.diagnostics(provider_execution)
                self._log_archive.append_snapshot(execution_id, "readiness_failed", diagnostics)
                still_deploying = _readiness_is_transient(readiness.reasons, diagnostics)
                cleanup = None if still_deploying else await adapter.cleanup(provider_execution, provider_artifact)
                execution = DeploymentExecution(
                    execution_id=execution_id,
                    request_id=request.request_id,
                    status=DeploymentStatus.DEPLOYING if still_deploying else DeploymentStatus.FAILED,
                    artifact=artifact,
                    namespace=namespace,
                    configuration_artifacts=request.configuration_artifacts,
                    provenance=request.provenance,
                    data_plane=data_plane,
                    metadata=metadata,
                    evidence_refs=list(provider_execution.get("evidence_refs") or []),
                    diagnostics=VersionedPayload(
                        schema_version="deploy-diagnostics.v1",
                        value={
                            "reasons": readiness.reasons,
                            "snapshot": diagnostics,
                            "cleanup": cleanup,
                        },
                    ),
                )
            else:
                diagnostics = await adapter.diagnostics(provider_execution)
                self._log_archive.append_snapshot(execution_id, "ready", diagnostics)
                endpoint_url = str(provider_execution.get("endpoint_url") or "")
                if not endpoint_url:
                    raise RuntimeError("provider readiness completed without an endpoint")
                baseline_endpoint_url = str(provider_execution.get("baseline_endpoint_url") or "") or None
                execution = DeploymentExecution(
                    execution_id=execution_id,
                    request_id=request.request_id,
                    status=DeploymentStatus.READY,
                    artifact=artifact,
                    endpoint=DeploymentEndpoint(url=endpoint_url, baseline_url=baseline_endpoint_url),
                    namespace=namespace,
                    configuration_artifacts=request.configuration_artifacts,
                    provenance=request.provenance,
                    data_plane=data_plane,
                    metadata=metadata,
                    evidence_refs=list(provider_execution.get("evidence_refs") or []),
                    diagnostics=VersionedPayload(
                        schema_version="deploy-diagnostics.v1",
                        value={
                            "snapshot": diagnostics,
                            "data_plane": data_plane,
                            **({"data_plane_warning": data_plane_warning} if data_plane_warning else {}),
                        },
                    ),
                )
            execution = self._store(execution)
            self._provider_executions[execution.execution_id] = (adapter, provider_artifact, provider_execution)
            return execution
        except BaseException as error:
            diagnostics: dict[str, Any] | None = None
            cleanup: dict[str, Any] | None = None
            if provider_artifact is not None and provider_execution is not None:
                try:
                    diagnostics = await adapter.diagnostics(provider_execution)
                    self._log_archive.append_snapshot(execution_id, "create_failed", diagnostics)
                    cleanup = await adapter.cleanup(provider_execution, provider_artifact)
                except Exception as cleanup_error:
                    cleanup = {"cleaned_up": False, "error": str(cleanup_error)}
            failed = self._store(
                DeploymentExecution(
                    execution_id=execution_id,
                    request_id=request.request_id,
                    status=DeploymentStatus.FAILED,
                    artifact=artifact,
                    namespace=namespace,
                    configuration_artifacts=request.configuration_artifacts,
                    provenance=request.provenance,
                    metadata=metadata,
                    diagnostics=VersionedPayload(
                        schema_version="deploy-diagnostics.v1",
                        value={"reason": str(error), "snapshot": diagnostics, "cleanup": cleanup},
                    ),
                )
            )
            if isinstance(error, asyncio.CancelledError):
                raise DeploymentCreationCancelled(failed) from error
            return failed
        finally:
            _creating_executions.pop(execution_id, None)

    async def kill(
        self,
        request: DeploymentKillRequest,
        execution: DeploymentExecution,
    ) -> DeploymentExecution:
        if request.execution_id != execution.execution_id:
            raise ValueError("kill request does not match deployment execution")
        if request.expected_namespace is not None and request.expected_namespace != execution.namespace:
            raise ValueError("kill request namespace does not match deployment execution")
        provider_state = self._provider_executions.get(execution.execution_id)
        if provider_state is None:
            raise ValueError("deployment execution is not managed by this service")
        adapter, provider_artifact, provider_execution = provider_state
        diagnostics: dict[str, Any] | None = None
        try:
            diagnostics = await adapter.diagnostics(provider_execution)
            self._archive_snapshot(execution.execution_id, "killed", diagnostics)
            cleanup = await adapter.cleanup(provider_execution, provider_artifact)
        except Exception as error:
            return self._store(
                execution.model_copy(
                    update={
                        "status": DeploymentStatus.FAILED,
                        "diagnostics": VersionedPayload(
                            schema_version="deploy-diagnostics.v1",
                            value={
                                "snapshot": diagnostics,
                                "cleanup": {"cleaned_up": False, "error": str(error)},
                                "reason": request.reason,
                            },
                        ),
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
        status = DeploymentStatus.CLEANED_UP if cleanup.get("cleaned_up", False) else DeploymentStatus.FAILED
        updated = execution.model_copy(
            update={
                "status": status,
                "diagnostics": VersionedPayload(
                    schema_version="deploy-diagnostics.v1",
                    value={"snapshot": diagnostics, "cleanup": cleanup, "reason": request.reason},
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        self._provider_executions.pop(execution.execution_id, None)
        return self._store(updated)

    @staticmethod
    def _has_provider_context(execution: DeploymentExecution) -> bool:
        """True when the execution has the state needed to reach cluster resources."""
        return execution.namespace is not None and bool(execution.configuration_artifacts)

    async def stop(self, execution: DeploymentExecution) -> DeploymentExecution:
        provider_state = self._provider_executions.get(execution.execution_id)
        if provider_state is None and not self._has_provider_context(execution):
            # The execution failed before any cluster resource was created.
            return self._store(
                execution.model_copy(
                    update={
                        "status": DeploymentStatus.ROLLED_BACK,
                        "diagnostics": VersionedPayload(
                            schema_version="deploy-diagnostics.v1",
                            value={"stop": {"stopped": True, "reason": "no_provider_context"}},
                        ),
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
        if provider_state is None:
            adapter, artifact, provider_execution = self._restored_provider_state(execution)
        else:
            adapter, artifact, provider_execution = provider_state
        diagnostics = await adapter.diagnostics(provider_execution)
        self._archive_snapshot(execution.execution_id, "stopped", diagnostics)
        stopped = await adapter.stop(provider_execution, artifact)
        return self._store(
            execution.model_copy(
                update={
                    "status": DeploymentStatus.ROLLED_BACK,
                    "diagnostics": VersionedPayload(
                        schema_version="deploy-diagnostics.v1",
                        value={"snapshot": diagnostics, "stop": stopped},
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
        )

    async def clean(
        self, execution: DeploymentExecution, *, preserve_rendered_overlay: bool = False
    ) -> DeploymentExecution:
        provider_state = self._provider_executions.get(execution.execution_id)
        if provider_state is None and not self._has_provider_context(execution):
            # The execution failed before any cluster resource was created, so
            # there is nothing to clean; just drop the record.
            self._provider_executions.pop(execution.execution_id, None)
            return self._store(
                execution.model_copy(
                    update={
                        "status": DeploymentStatus.CLEANED,
                        "diagnostics": VersionedPayload(
                            schema_version="deploy-diagnostics.v1",
                            value={"cleanup": {"cleaned_up": True, "reason": "no_provider_context"}},
                        ),
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
        if provider_state is None:
            adapter, artifact, provider_execution = self._restored_provider_state(execution)
        else:
            adapter, artifact, provider_execution = provider_state
        diagnostics = await adapter.diagnostics(provider_execution)
        self._archive_snapshot(execution.execution_id, "cleanup", diagnostics)
        cleanup = await adapter.cleanup(provider_execution, artifact, force=True)
        overlay_removed = False
        if cleanup.get("cleaned_up", False) and not preserve_rendered_overlay:
            remove_overlay = getattr(adapter, "remove_rendered_overlay", None)
            if callable(remove_overlay):
                overlay_removed = bool(remove_overlay(artifact))
        cleanup["rendered_overlay_preserved"] = preserve_rendered_overlay
        cleanup["rendered_overlay_removed"] = overlay_removed
        status = DeploymentStatus.CLEANED if cleanup.get("cleaned_up", False) else execution.status
        self._provider_executions.pop(execution.execution_id, None)
        return self._store(
            execution.model_copy(
                update={
                    "status": status,
                    "diagnostics": VersionedPayload(
                        schema_version="deploy-diagnostics.v1",
                        value={"snapshot": diagnostics, "cleanup": cleanup},
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
        )

    async def get(self, execution_id: str) -> DeploymentExecution:
        try:
            return self._executions[execution_id]
        except KeyError as error:
            raise KeyError(f"unknown deployment execution: {execution_id}") from error

    def restore_execution(self, execution: DeploymentExecution) -> None:
        """Restore durable execution state needed for post-restart log reads."""
        self._executions[execution.execution_id] = execution

    def _restored_provider_state(
        self, execution: DeploymentExecution
    ) -> tuple[GuideAdapter, GuideDeploymentArtifact, dict[str, Any]]:
        if execution.namespace is None or not execution.configuration_artifacts:
            raise ValueError("deployment execution has no persisted provider context")
        provider_ref = execution.configuration_artifacts[0].provider_ref
        adapter = self._catalog.get_adapter(provider_ref)
        if adapter is None:
            raise ValueError("deployment provider is no longer registered")
        register_namespace = getattr(adapter, "register_restored_namespace", None)
        if callable(register_namespace):
            register_namespace(execution.namespace)
        artifact = GuideDeploymentArtifact(
            guide_id=provider_ref,
            artifact_hash=execution.artifact.artifact_hash,
            manifest_ref=execution.artifact.manifest_ref,
            manifest_checksum=execution.artifact.manifest_checksum,
            source_ref=execution.artifact.source_ref,
            deployment_contract=dict(execution.artifact.rendered_payload.value.get("deployment_contract") or {}),
        )
        snapshot = (execution.diagnostics.value.get("snapshot") or {}) if execution.diagnostics else {}
        provider_context = {
            key: value
            for key, value in snapshot.items()
            if key.startswith(("router_", "calibration_")) or key in {"model", "calibrated_peak_prefill_throughput"}
        }
        if execution.endpoint:
            provider_context["endpoint_url"] = execution.endpoint.url
            provider_context["baseline_endpoint_url"] = execution.endpoint.baseline_url
        return adapter, artifact, {**provider_context, "namespace": execution.namespace, "_artifact": artifact}

    async def refresh_diagnostics(self, execution: DeploymentExecution) -> DeploymentExecution:
        if execution.execution_id in _creating_executions:
            return _creating_executions[execution.execution_id]
        provider_state = self._provider_executions.get(execution.execution_id)
        if provider_state is None:
            if execution.namespace is None or not execution.configuration_artifacts:
                return execution
            adapter, _artifact, provider_execution = self._restored_provider_state(execution)
        else:
            adapter, _artifact, provider_execution = provider_state
        readiness = await adapter.readiness(provider_execution)
        diagnostics = await adapter.diagnostics(provider_execution)
        self._archive_snapshot(execution.execution_id, "refreshed", diagnostics)
        endpoint = execution.endpoint
        status = execution.status
        if readiness.accepted:
            endpoint_url = str(provider_execution.get("endpoint_url") or "")
            if endpoint_url:
                baseline_endpoint_url = str(provider_execution.get("baseline_endpoint_url") or "") or (
                    execution.endpoint.baseline_url if execution.endpoint else None
                )
                endpoint = DeploymentEndpoint(
                    url=endpoint_url,
                    baseline_url=baseline_endpoint_url,
                )
                status = DeploymentStatus.READY
        elif self._namespace_was_deleted(readiness.reasons):
            status = DeploymentStatus.CLEANED
        elif _readiness_is_transient(readiness.reasons, diagnostics):
            # A transient blip (command timeout, or the control-plane being
            # briefly unreachable) says nothing about the workload itself --
            # leave the previously observed status (e.g. READY) untouched
            # instead of downgrading a healthy deployment.
            pass
        elif execution.status not in {DeploymentStatus.CLEANED, DeploymentStatus.CLEANED_UP}:
            status = DeploymentStatus.FAILED
        return self._store(
            execution.model_copy(
                update={
                    "status": status,
                    "endpoint": endpoint,
                    "diagnostics": VersionedPayload(
                        schema_version="deploy-diagnostics.v1",
                        value={"snapshot": diagnostics, "readiness": readiness.reasons},
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
        )

    async def get_logs(self, request: DeploymentLogRequest) -> DeploymentLogPage:
        execution = await self.get(request.execution_id)
        archived_entries = self._log_archive.read_entries(execution.execution_id)
        if archived_entries:
            return self._log_page(execution, request, archived_entries)
        diagnostics = execution.diagnostics.value if execution.diagnostics else {}
        snapshot = diagnostics.get("snapshot") if isinstance(diagnostics, dict) else None
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        if not snapshot:
            provider_state = self._provider_executions.get(execution.execution_id)
            if provider_state is None:
                adapter, _artifact, provider_execution = self._restored_provider_state(execution)
            else:
                adapter, _artifact, provider_execution = provider_state
            snapshot = await adapter.diagnostics(provider_execution)
            self._archive_snapshot(execution.execution_id, "queried", snapshot)
            execution = self._store(
                execution.model_copy(
                    update={
                        "diagnostics": VersionedPayload(
                            schema_version="deploy-diagnostics.v1",
                            value={"snapshot": snapshot},
                        ),
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
        source_map = {
            "workload": snapshot.get("modelserver_logs"),
            "events": snapshot.get("events"),
        }
        sources = request.sources or list(source_map)
        entries = [
            DeploymentLogEntry(
                timestamp=datetime.now(UTC),
                source=source,
                message=str(source_map[source]),
            )
            for source in sources
            if source_map[source]
        ]
        return self._log_page(execution, request, entries)

    def _log_page(
        self,
        execution: DeploymentExecution,
        request: DeploymentLogRequest,
        entries: list[DeploymentLogEntry],
    ) -> DeploymentLogPage:
        sources = request.sources or ["workload", "events"]
        unsupported = [source for source in sources if source not in {"workload", "events"}]
        if unsupported:
            raise ValueError(f"unsupported deployment log sources: {', '.join(unsupported)}")
        entries = [entry for entry in entries if entry.source in sources]
        offset = self._log_offset(request.cursor)
        page_entries = entries[offset : offset + request.limit]
        next_offset = offset + len(page_entries)
        next_cursor = str(next_offset) if next_offset < len(entries) else None
        evidence_refs = [ref for ref in execution.evidence_refs if ref.startswith("deployment-log://")]
        archive_ref = self._log_archive.evidence_ref(execution.execution_id)
        if self._log_archive.has_entries(execution.execution_id) and archive_ref not in evidence_refs:
            evidence_refs.append(archive_ref)
        return DeploymentLogPage(
            execution_id=execution.execution_id,
            complete=not request.follow,
            evidence_refs=evidence_refs,
            entries=page_entries,
            next_cursor=next_cursor,
        )

    def _archive_snapshot(self, execution_id: str, phase: str, snapshot: dict[str, Any] | None) -> None:
        if snapshot:
            self._log_archive.append_snapshot(execution_id, phase, snapshot)

    @staticmethod
    def _namespace_was_deleted(reasons: list[str]) -> bool:
        return any("namespaces" in reason.lower() and "not found" in reason.lower() for reason in reasons)

    @staticmethod
    def _log_offset(cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            offset = int(cursor)
        except ValueError as error:
            raise ValueError("deployment log cursor must be a non-negative integer") from error
        if offset < 0:
            raise ValueError("deployment log cursor must be a non-negative integer")
        return offset

    @staticmethod
    def _single_provider_artifact(artifacts: list[ConfigurationArtifact]) -> ConfigurationArtifact:
        if len(artifacts) != 1:
            raise ValueError("GuideAdapter compatibility requires exactly one configuration artifact")
        provider_refs = {artifact.provider_ref for artifact in artifacts}
        if len(provider_refs) != 1:
            raise ValueError("a deployment create request must use one deployment provider")
        return artifacts[0]

    @staticmethod
    def _compatibility_overrides(artifact: ConfigurationArtifact) -> dict[str, Any]:
        if artifact.media_type != "application/json" or artifact.content is None:
            raise ValueError("GuideAdapter compatibility requires inline JSON configuration")
        parsed = json.loads(artifact.content)
        if not isinstance(parsed, dict):
            raise ValueError("GuideAdapter compatibility configuration must be a JSON object")
        return parsed

    @staticmethod
    def _deployment_artifact(
        request: DeploymentCreateRequest,
        configuration_artifact: ConfigurationArtifact,
        provider_artifact: GuideDeploymentArtifact | None = None,
    ) -> DeploymentArtifact:
        return DeploymentArtifact(
            artifact_hash=provider_artifact.artifact_hash
            if provider_artifact
            else stable_hash({"request_id": request.request_id, "checksum": configuration_artifact.checksum}),
            configuration_artifact_ids=[item.artifact_id for item in request.configuration_artifacts],
            source_ref=provider_artifact.source_ref if provider_artifact else None,
            manifest_ref=provider_artifact.manifest_ref if provider_artifact else None,
            manifest_checksum=provider_artifact.manifest_checksum if provider_artifact else None,
            values_checksum=provider_artifact.values_checksum if provider_artifact else None,
            rendered_payload=VersionedPayload(
                schema_version="deploy-rendered-artifact.v1",
                value={
                    "provider_ref": configuration_artifact.provider_ref,
                    "deployment_contract": provider_artifact.deployment_contract if provider_artifact else {},
                },
            ),
        )

    def _store(self, execution: DeploymentExecution) -> DeploymentExecution:
        self._log_archive.register_execution(execution)
        self._executions[execution.execution_id] = execution
        return execution
