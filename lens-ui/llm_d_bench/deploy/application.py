"""Application service for creating and operating deployment runs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from llm_d_bench.cluster import deployment_runtime_overrides, require_active_session
from llm_d_bench.cluster import registry as cluster_registry
from llm_d_bench.cluster.deployment_source import resolve_cluster_deployment_source
from llm_d_bench.deploy.contracts import DeploymentCaseStatus, DeploymentRunCreateRequest, DeploymentRunStatus
from llm_d_bench.deploy.plan_service import DeploymentPlanService
from llm_d_bench.deploy.run_store import JsonDeploymentRunStore
from llm_d_bench.deploy.runtime.composition import (
    RuntimeConfigurationError,
    build_deployment_service_from_environment,
)
from llm_d_bench.deploy.worker import DeploymentRunWorker


def cluster_deployment_source(session, requested: dict | None = None) -> dict:
    """Resolve source settings from the selected cluster; ignore request-supplied paths."""
    cluster = cluster_registry.require_cluster(session.server_id)
    source = dict(requested or {})
    for key in ("repository", "branch", "warning"):
        source.pop(key, None)
    source.update(resolve_cluster_deployment_source(cluster))
    return source



_ACCELERATOR_KEYS = ("accelerator", "accelerator_variant", "upstream_variant")


def _deploy_accelerator(run) -> str | None:
    """The accelerator/vendor a run targets, from its provenance/configurations.

    The Deploy runtime renders a vendor-specific overlay (Intel XPU vs NVIDIA
    GPU); without this it defaults to Intel, so an NVIDIA cluster would render
    gpu.intel.com claims. Returns None when nothing recorded it (keep default).
    """
    if run is None:
        return None

    def first(mapping: object) -> str | None:
        if not isinstance(mapping, dict):
            return None
        for key in _ACCELERATOR_KEYS:
            value = mapping.get(key)
            if value:
                return str(value)
        return None

    recorded = first(getattr(run, "provenance", None))
    if recorded:
        return recorded
    for configuration in getattr(run, "source_configurations", None) or []:
        content = getattr(configuration, "content", None)
        recorded = first(getattr(configuration, "provenance", None)) or first(content)
        if recorded:
            return recorded
        guide = content.get("officialGuide") if isinstance(content, dict) else None
        recorded = first(guide) or first(guide.get("source") if isinstance(guide, dict) else None)
        if recorded:
            return recorded
    return None


class DeploymentRunManager:
    """Compose Deploy storage, runtime selection, and background scheduling."""

    def __init__(self, store: JsonDeploymentRunStore | None = None) -> None:
        self.store = store or JsonDeploymentRunStore()
        self._default_worker: DeploymentRunWorker | None = None
        self._run_workers: dict[str, DeploymentRunWorker] = {}
        self._run_tasks: dict[str, asyncio.Task[None]] = {}

    def _schedule_run(self, run_id: str, worker: DeploymentRunWorker) -> None:
        existing = self._run_tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(worker.execute_run(run_id))
        self._run_tasks[run_id] = task
        task.add_done_callback(
            lambda completed, scheduled_run_id=run_id: (
                self._run_tasks.pop(scheduled_run_id, None)
                if self._run_tasks.get(scheduled_run_id) is completed
                else None
            )
        )

    def worker_for_run(self, run_id: str) -> DeploymentRunWorker:
        cached = self._run_workers.get(run_id)
        if cached is not None:
            return cached
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        worker = self._worker(run)
        self._run_workers[run_id] = worker
        return worker

    def is_run_active(self, run_id: str) -> bool:
        """Whether this process is still executing the deployment operation."""
        task = self._run_tasks.get(run_id)
        return task is not None and not task.done()

    async def start_run(self, request: DeploymentRunCreateRequest, *, owner_user_id: str | None = None):
        session_id = request.provenance.get("cluster_session_id")
        session = None
        if session_id:
            session = require_active_session(session_id)
            requested_server_id = request.provenance.get("cluster_server_id")
            if requested_server_id and requested_server_id != session.server_id:
                raise ValueError("cluster_server_id does not match the active cluster session")
            request.provenance["cluster_server_id"] = session.server_id
            for configuration in request.configurations:
                if configuration.format != "manifest":
                    continue
                cluster_ref = configuration.provenance.get("cluster_ref")
                if not isinstance(cluster_ref, dict) or not cluster_ref.get("id"):
                    raise ValueError("manifest configuration requires a target cluster binding")
                if cluster_ref["id"] != session.server_id:
                    raise ValueError(
                        "saved YAML belongs to a different cluster; select its original cluster before deploying"
                    )
        else:
            raise ValueError("deployment requires an explicit active cluster session")
        deployment_source = request.provenance.get("deployment_source")
        if deployment_source is not None and not isinstance(deployment_source, dict):
            raise ValueError("provenance.deployment_source must be an object")
        # A connected cluster is the authority for the llm-d checkout used by
        # every deployment path. Do not clone a second copy from an artifact's
        # guide_source or reconstruct a cache path.
        request.provenance["deployment_source"] = cluster_deployment_source(session, deployment_source)
        run = DeploymentPlanService(self.store).create_run(request)
        if owner_user_id:
            from llm_d_bench.db.dao.deployment_batch import DeploymentBatchDao

            DeploymentBatchDao().set_owner(run.id, owner_user_id=owner_user_id)
        worker = self._worker(run)
        self._run_workers[run.id] = worker
        self._schedule_run(run.id, worker)
        return run

    def rebind_cluster_session(self, run_id: str, session_id: str) -> None:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        session = require_active_session(session_id)
        bound_server_id = run.provenance.get("cluster_server_id")
        if bound_server_id and bound_server_id != session.server_id:
            raise ValueError("active cluster session is not connected to this deployment's original cluster")
        run.provenance["cluster_session_id"] = session.id
        run.provenance["cluster_server_id"] = session.server_id
        self.store.save_run(run)
        self._run_workers.pop(run_id, None)

    def resume_run(self, run_id: str) -> None:
        """Reschedule a persisted run interrupted before it created an execution."""
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        interrupted = [
            case for case in run.cases if case.status == DeploymentCaseStatus.RENDERING and case.execution_id is None
        ]
        if run.status == DeploymentRunStatus.RUNNING and interrupted:
            for case in interrupted:
                case.status = DeploymentCaseStatus.QUEUED
            run.status = DeploymentRunStatus.QUEUED
            self.store.save_run(run)
        if run.status != DeploymentRunStatus.QUEUED:
            return
        worker = self.worker_for_run(run_id)
        self._schedule_run(run_id, worker)

    def reconcile_interrupted_cancellation(self, run_id: str) -> None:
        """Finish cancellation work that was interrupted before an execution existed."""
        run = self.store.get_run(run_id)
        if run is None or run.status != DeploymentRunStatus.CANCELLING:
            return
        for case in run.cases:
            if case.execution_id is None and case.status in {
                DeploymentCaseStatus.QUEUED,
                DeploymentCaseStatus.RENDERING,
                DeploymentCaseStatus.CANCELLING,
            }:
                case.status = DeploymentCaseStatus.CANCELLED
        active = {
            DeploymentCaseStatus.QUEUED,
            DeploymentCaseStatus.RENDERING,
            DeploymentCaseStatus.DEPLOYING,
            DeploymentCaseStatus.CANCELLING,
        }
        if not any(case.status in active for case in run.cases):
            run.status = DeploymentRunStatus.CANCELLED
            run.finished_at = datetime.now(UTC)
        self.store.save_run(run)

    @staticmethod
    def has_available_source(run) -> bool:
        """Return whether a persisted run's runtime can still be reconstructed."""
        source = run.provenance.get("deployment_source")
        repository = source.get("resolved_repository") if isinstance(source, dict) else None
        return not repository or Path(str(repository)).is_dir()

    def _worker(self, run=None) -> DeploymentRunWorker:
        source = run.provenance.get("deployment_source") if run else None
        resolved_repository = source.get("resolved_repository") if isinstance(source, dict) else None
        session_id = run.provenance.get("cluster_session_id") if run else None
        if run is not None and not resolved_repository:
            cluster_server_id = run.provenance.get("cluster_server_id")
            if cluster_server_id:
                resolved_repository = cluster_deployment_source(
                    SimpleNamespace(server_id=cluster_server_id),
                    source if isinstance(source, dict) else None,
                )["resolved_repository"]
        lifecycle_error = None
        try:
            runtime_overrides = deployment_runtime_overrides(session_id)
        except ValueError as error:
            if not session_id or str(error) != "cluster session is no longer active":
                raise
            lifecycle_error = (
                "cluster session is no longer active; reconnect the original cluster before changing this deployment"
            )
            runtime_overrides = {"KUBECONFIG": str(Path("/dev/null"))}
        accelerator = _deploy_accelerator(run)
        accelerator_env = {"PRISM_DEPLOY_ACCELERATOR": accelerator} if accelerator else {}
        # The cluster's saved proxy configuration is authoritative for anything
        # this run deploys; resolve_proxy_env already falls back to this backend
        # process's environment for auto/unknown clusters.
        proxy_env: dict[str, str] = {}
        cluster_server_id = run.provenance.get("cluster_server_id") if run else None
        if cluster_server_id:
            try:
                from llm_d_bench.cluster.service import resolve_proxy_env

                proxy_env = resolve_proxy_env(str(cluster_server_id))
            except Exception:  # pragma: no cover - a bad proxy lookup must not block deploys
                proxy_env = {}
        if not resolved_repository:
            if runtime_overrides:
                return self._build_worker({**runtime_overrides, **accelerator_env, **proxy_env}, lifecycle_error)
            if self._default_worker is None:
                self._default_worker = self._build_worker({**accelerator_env, **proxy_env} or None)
            return self._default_worker
        return self._build_worker(
            {"LLM_D_ROOT": str(resolved_repository), **runtime_overrides, **accelerator_env, **proxy_env},
            lifecycle_error,
        )

    def _build_worker(
        self, environment: dict[str, str] | None, lifecycle_error: str | None = None
    ) -> DeploymentRunWorker:
        configured = build_deployment_service_from_environment(environment)
        if configured is None:
            raise RuntimeConfigurationError("local Deploy runtime is disabled")
        service, _catalog = configured
        return DeploymentRunWorker(self.store, service, lifecycle_error)


deployment_run_manager = DeploymentRunManager()
