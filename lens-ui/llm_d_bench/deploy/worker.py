"""Concurrent execution and cancellation of Deploy-owned deployment runs."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from llm_d_bench.auth.service import default_service
from llm_d_bench.deploy.contracts import (
    DeploymentCaseStatus,
    DeploymentKillRequest,
    DeploymentLogRequest,
    DeploymentRunStatus,
    DeploymentStatus,
    ProblemDetails,
)
from llm_d_bench.deploy.run_store import JsonDeploymentRunStore
from llm_d_bench.deploy.service import DeploymentCreationCancelled, DeploymentService

# A durable 'enabling' record without an active task is an interrupted attempt.
_monitoring_inflight: set[str] = set()


class DeploymentRunWorker:
    def __init__(
        self,
        store: JsonDeploymentRunStore,
        service: DeploymentService,
        lifecycle_error: str | None = None,
    ) -> None:
        self._store = store
        self._service = service
        self._lifecycle_error = lifecycle_error
        self._active_tasks: dict[str, set[asyncio.Task[object]]] = {}
        self._cancel_requested: set[str] = set()
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def request_cancel(self, run_id: str) -> None:
        run = self._store.get_run(run_id)
        if run is None or run.status not in {DeploymentRunStatus.QUEUED, DeploymentRunStatus.RUNNING}:
            return
        self._cancel_requested.add(run_id)
        run.status = DeploymentRunStatus.CANCELLING
        self._store.save_run(run)
        for task in self._active_tasks.get(run_id, set()):
            task.cancel()

    async def _cancel_and_wait(self, run_id: str, *, timeout: float = 5.0) -> None:
        """Cancel an in-progress run and block until it reaches a terminal state.

        Reuses ``request_cancel`` (which cancels any in-flight case tasks;
        each one runs its own cluster cleanup via ``kill`` when cancelled)
        and waits for ``_execute_run``'s loop to observe the cancellation and
        settle every case, so callers can safely proceed with deletion right
        after this returns instead of rejecting the delete outright.
        """
        self.request_cancel(run_id)
        tasks = self._active_tasks.get(run_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        active_statuses = {
            DeploymentRunStatus.QUEUED,
            DeploymentRunStatus.RUNNING,
            DeploymentRunStatus.CANCELLING,
        }
        run = self._store.get_run(run_id)
        if run is None or run.status not in active_statuses:
            return
        if not tasks:
            # Nothing was actually executing (e.g. a QUEUED run cancelled
            # before its task started iterating, or no execute_run loop is
            # running at all) -- there is nothing left to wait for.
            self._cancel_pending(run)
            return
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            run = self._store.get_run(run_id)
            if run is None or run.status not in active_statuses:
                return
            if loop.time() >= deadline:
                # The execute_run loop never got a chance to observe the
                # cancellation -- force a terminal state ourselves.
                self._cancel_pending(run)
                return
            await asyncio.sleep(0.05)

    async def cancel_evaluation_deployment(self, run_id: str) -> None:
        """Settle provisioning, then remove resources owned by a cancelled evaluation."""
        await self._cancel_and_wait(run_id)
        run = self._store.get_run(run_id)
        if run is None:
            return
        errors = []
        for case in list(run.cases):
            if not case.execution_id or case.status in {
                DeploymentCaseStatus.CLEANED,
                DeploymentCaseStatus.CLEANED_UP,
            }:
                continue
            execution = self._store.get_execution(case.execution_id)
            if execution is not None and execution.status in {DeploymentStatus.CLEANED, DeploymentStatus.CLEANED_UP}:
                latest_run = self._store.get_run(run_id)
                latest_case = next(item for item in latest_run.cases if item.id == case.id)
                self._apply_refreshed_status(latest_case, execution.status)
                self._store.save_run(latest_run)
                continue
            try:
                _case, cleaned = await self.clean_case(run_id, case.id)
                if cleaned.status not in {DeploymentStatus.CLEANED, DeploymentStatus.CLEANED_UP}:
                    raise ValueError("cleanup did not confirm resource removal")
            except Exception as error:
                errors.append(f"deployment case {case.id}: {error}")
        if errors:
            raise ValueError("; ".join(errors))
        run = self._store.get_run(run_id)
        if run is not None:
            run.status = DeploymentRunStatus.CANCELLED
            run.finished_at = datetime.now(UTC)
            self._store.save_run(run)

    async def get_case_logs(self, run_id: str, case_id: str, limit: int = 200):
        _run, case, execution = self._case_execution(run_id, case_id)
        if case.status not in {
            DeploymentCaseStatus.STOPPED,
            DeploymentCaseStatus.CLEANED,
            DeploymentCaseStatus.CLEANED_UP,
        } and execution.status not in {
            DeploymentStatus.ROLLED_BACK,
            DeploymentStatus.CLEANED,
            DeploymentStatus.CLEANED_UP,
        }:
            execution = await self._service.refresh_diagnostics(execution)
            self._store.save_execution(execution)
        self._service.restore_execution(execution)
        return await self._service.get_logs(DeploymentLogRequest(execution_id=execution.execution_id, limit=limit))

    async def execute_run(self, run_id: str) -> None:
        await self._execute_run(run_id)

    async def _enable_ready_monitoring(self, execution):
        """Start scraping once readiness is durable, independently of evaluation/UI."""
        execution = self._store.get_execution(execution.execution_id) or execution
        setup = execution.monitoring_setup
        if execution.status != DeploymentStatus.READY or (
            setup and (setup.get("status") != "enabling" or execution.execution_id in _monitoring_inflight)
        ):
            return execution
        # Lazy import: Monitoring resolves execution identity through Deploy's facade.
        from llm_d_bench.monitoring.deployment import service as monitoring

        started_at = datetime.now(UTC).isoformat()
        execution = execution.model_copy(
            update={
                "monitoring_setup": {
                    "status": "enabling",
                    "enabled": False,
                    "started_at": started_at,
                }
            }
        )
        self._store.save_execution(execution)
        _monitoring_inflight.add(execution.execution_id)
        try:
            async with asyncio.timeout(60):
                result = await monitoring.enable(execution.execution_id)
            setup = {**result, "started_at": started_at}
        except Exception as error:
            # Monitoring failure must not turn a working model deployment into a failure.
            setup = {
                "status": "unavailable",
                "enabled": False,
                "code": getattr(error, "code", "monitoring_enable_failed"),
                "message": str(error) or "Monitoring setup timed out",
                "started_at": started_at,
            }
        finally:
            _monitoring_inflight.discard(execution.execution_id)
        setup["finished_at"] = datetime.now(UTC).isoformat()
        # Setup may finish after a user has stopped/cleaned the deployment.
        # Only update the audit; never restore its old READY lifecycle state.
        latest = self._store.get_execution(execution.execution_id)
        if latest is None:
            return execution
        execution = latest.model_copy(update={"monitoring_setup": setup})
        self._store.save_execution(execution)
        return execution

    async def refresh_case_execution(self, run_id: str, case_id: str):
        run, case, execution = self._case_execution(run_id, case_id)
        if case.status == DeploymentCaseStatus.STOPPED or execution.status == DeploymentStatus.ROLLED_BACK:
            persisted_case = next((item for item in run.cases if item.id == case_id), None)
            if persisted_case is not None and persisted_case.status != DeploymentCaseStatus.STOPPED:
                persisted_case.status = DeploymentCaseStatus.STOPPED
                self._aggregate(run)
                self._store.save_run(run)
            return execution
        refreshed = await self._refresh_case_executions(run_id, [case_id])
        try:
            return refreshed[case_id]
        except KeyError as error:
            raise ValueError("deployment case refresh failed") from error

    async def refresh_run_executions(self, run_id: str, *, pending_only: bool = False) -> None:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        case_ids = [
            case.id
            for case in run.cases
            if case.execution_id is not None
            and (not pending_only or case.status == DeploymentCaseStatus.DEPLOYING)
            and case.status
            not in {
                DeploymentCaseStatus.STOPPED,
                DeploymentCaseStatus.CLEANED,
                DeploymentCaseStatus.CLEANED_UP,
                DeploymentCaseStatus.CANCELLED,
            }
        ]
        await self._refresh_case_executions(run_id, case_ids)

    async def _refresh_case_executions(self, run_id: str, case_ids: list[str]):
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        executions = []
        preserved = {}
        for case_id in case_ids:
            case = next((item for item in run.cases if item.id == case_id), None)
            if case is None:
                raise ValueError("deployment case not found")
            if case.execution_id:
                execution = self._store.get_execution(case.execution_id)
                if execution is not None and execution.status == DeploymentStatus.ROLLED_BACK:
                    case.status = DeploymentCaseStatus.STOPPED
                    preserved[case_id] = execution
                    continue
            if case.status in {DeploymentCaseStatus.CLEANED, DeploymentCaseStatus.CLEANED_UP}:
                execution = self._store.get_execution(case.execution_id) if case.execution_id else None
                if execution is None:
                    raise ValueError("deployment execution not found")
                preserved[case_id] = execution
                continue
            if case.execution_id is None:
                raise ValueError("deployment case has no execution yet")
            execution = self._store.get_execution(case.execution_id)
            if execution is None:
                raise ValueError("deployment execution not found")
            executions.append((case_id, execution))
        results = await asyncio.gather(
            *(self._service.refresh_diagnostics(execution) for _case_id, execution in executions),
            return_exceptions=True,
        )
        refreshed = {
            case_id: result
            for (case_id, _execution), result in zip(executions, results, strict=False)
            if not isinstance(result, Exception)
        }
        refreshed = {**preserved, **refreshed}
        async with self._refresh_locks.setdefault(run_id, asyncio.Lock()):
            current_run = self._store.get_run(run_id)
            if current_run is None:
                raise ValueError("deployment run not found")
            for case in current_run.cases:
                execution = refreshed.get(case.id)
                if execution is None:
                    continue
                latest_execution = self._store.get_execution(execution.execution_id)
                if (
                    latest_execution is not None
                    and latest_execution.status in {DeploymentStatus.CLEANED, DeploymentStatus.CLEANED_UP}
                    and execution.status not in {DeploymentStatus.CLEANED, DeploymentStatus.CLEANED_UP}
                ):
                    execution = latest_execution
                    refreshed[case.id] = execution
                self._store.save_execution(execution)
                execution = await self._enable_ready_monitoring(execution)
                refreshed[case.id] = execution
                self._apply_refreshed_status(case, execution.status)
                if case.status == DeploymentCaseStatus.FAILED and case.failure is None:
                    case.failure = ProblemDetails(
                        status=500,
                        title="deployment case failed",
                        detail=self._failure_detail(execution),
                        code="deployment_failed",
                    )
            self._aggregate(current_run)
            self._store.save_run(current_run)
        return refreshed

    @staticmethod
    def _apply_refreshed_status(case, status: DeploymentStatus) -> None:
        if status == DeploymentStatus.ROLLED_BACK:
            case.status = DeploymentCaseStatus.STOPPED
        elif status == DeploymentStatus.READY:
            case.status = DeploymentCaseStatus.READY
            case.failure = None
        elif status == DeploymentStatus.DEPLOYING:
            case.status = DeploymentCaseStatus.DEPLOYING
            case.failure = None
        elif status == DeploymentStatus.CLEANED:
            case.status = DeploymentCaseStatus.CLEANED
        elif status == DeploymentStatus.CLEANED_UP:
            case.status = DeploymentCaseStatus.CLEANED_UP
        elif case.status not in {
            DeploymentCaseStatus.STOPPED,
            DeploymentCaseStatus.CANCELLED,
            DeploymentCaseStatus.CLEANED,
            DeploymentCaseStatus.CLEANED_UP,
        }:
            case.status = DeploymentCaseStatus.FAILED

    async def stop_case(self, run_id: str, case_id: str):
        self._require_active_cluster_session()
        run, case, execution = self._case_execution(run_id, case_id)
        stopped = await self._service.stop(execution)
        self._store.save_execution(stopped)
        case.status = DeploymentCaseStatus.STOPPED
        self._store.save_run(run)
        return case, stopped

    async def clean_case(self, run_id: str, case_id: str, *, preserve_rendered_overlay: bool = False):
        self._require_active_cluster_session()
        run, case, execution = self._case_execution(run_id, case_id)
        if case.status in {DeploymentCaseStatus.CLEANED, DeploymentCaseStatus.CLEANED_UP}:
            raise ValueError("deployment case is already cleaned")
        cleaned = await self._service.clean(execution, preserve_rendered_overlay=preserve_rendered_overlay)
        self._store.save_execution(cleaned)
        if cleaned.status == DeploymentStatus.CLEANED:
            case.status = DeploymentCaseStatus.CLEANED
        elif cleaned.status == DeploymentStatus.CLEANED_UP:
            case.status = DeploymentCaseStatus.CLEANED_UP
        self._aggregate(run)
        self._store.save_run(run)
        return case, cleaned

    async def restart_case(self, run_id: str, case_id: str, *, model_token: str | None = None):
        from llm_d_bench.deploy.plan_service import DeploymentPlanService

        self._require_active_cluster_session()
        run, case, execution = self._case_execution(run_id, case_id)
        if case is None or case.status not in {DeploymentCaseStatus.STOPPED, DeploymentCaseStatus.FAILED}:
            raise ValueError("only stopped or failed deployment cases can be restarted")
        cleaned = await self._service.clean(execution)
        self._store.save_execution(cleaned)
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        case = next(item for item in run.cases if item.id == case_id)
        if cleaned.status in {DeploymentStatus.CLEANED, DeploymentStatus.CLEANED_UP}:
            case.status = DeploymentCaseStatus.CLEANED
        else:
            raise ValueError(
                "previous deployment namespace could not be cleaned; reconnect the original cluster session "
                "before retrying"
            )
        self._store.save_run(run)
        retry = DeploymentPlanService(self._store).retry_case(run_id, case_id)
        asyncio.create_task(self._execute_retry(run_id, retry.id, model_token))
        return retry

    async def _execute_retry(self, run_id: str, case_id: str, model_token: str | None) -> None:
        if model_token:
            run = self._store.get_run(run_id)
            case = next((item for item in run.cases if item.id == case_id), None) if run else None
            if case is None:
                return
            case.status = DeploymentCaseStatus.RENDERING
            self._store.save_run(run)
            await self._execute_case(run, case, model_token=model_token)
            self._aggregate(run)
            return
        await self.execute_run(run_id)

    @staticmethod
    def _revoke_execution_shares(execution_ids: list[str | None]) -> None:
        """Drop per-deployment shares once those deployment records are gone."""
        default_service().revoke_resource_shares(resource_type="deployment_execution", resource_ids=execution_ids)

    @staticmethod
    def _revoke_run_shares(run_id: str) -> None:
        """Drop run-level shares once the run record itself is deleted."""
        default_service().revoke_resource_shares(resource_type="deployment_run", resource_ids=[run_id])

    async def delete_run(self, run_id: str) -> None:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        if run.status in {DeploymentRunStatus.QUEUED, DeploymentRunStatus.RUNNING, DeploymentRunStatus.CANCELLING}:
            raise ValueError("active deployment run must be stopped or cleaned before deletion")
        execution_ids = [case.execution_id for case in run.cases]
        for case in run.cases:
            if case.execution_id is None or case.status in {
                DeploymentCaseStatus.CLEANED,
                DeploymentCaseStatus.CLEANED_UP,
            }:
                continue
            if self._store.get_execution(case.execution_id) is None:
                # The execution record is already gone (e.g. removed out of
                # band, or a previous cleanup succeeded but crashed before
                # recording it). There is nothing left to clean up on the
                # cluster; treat the case as cleaned instead of blocking the
                # whole run's deletion forever with a stale reference.
                case.status = DeploymentCaseStatus.CLEANED
                continue
            await self.clean_case(run_id, case.id)
        self._store.delete_run(run_id)
        self._revoke_execution_shares(execution_ids)
        self._revoke_run_shares(run_id)

    async def delete_case_execution(self, run_id: str, case_id: str, *, delete_namespace: bool = True) -> None:
        """Optionally clean one deployment's cluster resources, then drop its records.

        When ``delete_namespace`` is true, cluster cleanup happens first: a
        persisted record is the only handle Prism keeps on a live namespace,
        so it is removed after the resources it points at are gone, never
        before. When false, the namespace and its workloads are left running
        and only Prism's own records are dropped.

        A deployment that's still being created (or already being cancelled)
        is cancelled first rather than rejected, so the user isn't forced to
        separately cancel and wait before they can delete it.
        """
        run = self._store.get_run(run_id)
        case = self._store.get_case(run_id, case_id)
        if run is None or case is None:
            raise ValueError("deployment case not found")
        if run.status in {
            DeploymentRunStatus.QUEUED,
            DeploymentRunStatus.RUNNING,
            DeploymentRunStatus.CANCELLING,
        }:
            await self._cancel_and_wait(run_id)
            run = self._store.get_run(run_id)
            case = self._store.get_case(run_id, case_id) if run is not None else None
            if run is None or case is None:
                raise ValueError("deployment case not found")
        cleaned_statuses = {DeploymentCaseStatus.CLEANED, DeploymentCaseStatus.CLEANED_UP}
        if delete_namespace and case.execution_id is not None and case.status not in cleaned_statuses:
            await self.clean_case(run_id, case_id)
            updated_case = self._store.get_case(run_id, case_id)
            if updated_case and updated_case.status not in cleaned_statuses:
                raise ValueError("failed to delete Kubernetes namespace; deployment record retained")
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        remaining = [item for item in run.cases if item.id != case_id]
        if remaining:
            run.cases = remaining
            self._aggregate(run)
            self._store.save_run(run)
            if case.execution_id is not None:
                self._store.delete_execution(case.execution_id)
                self._revoke_execution_shares([case.execution_id])
            return
        # The run only existed to own this deployment, so it goes with it.
        execution_ids = [item.execution_id for item in run.cases]
        self._store.delete_run(run_id)
        self._revoke_execution_shares(execution_ids)
        self._revoke_run_shares(run_id)

    def _require_active_cluster_session(self) -> None:
        if self._lifecycle_error:
            raise ValueError(self._lifecycle_error)

    def _case_execution(self, run_id: str, case_id: str):
        run = self._store.get_run(run_id)
        case = next((item for item in run.cases if item.id == case_id), None) if run is not None else None
        if run is None or case is None:
            raise ValueError("deployment case not found")
        if case.execution_id is None:
            raise ValueError("deployment case has no execution yet")
        execution = self._store.get_execution(case.execution_id)
        if execution is None:
            raise ValueError("deployment execution not found")
        return run, case, execution

    async def _execute_run(self, run_id: str) -> None:
        run = self._store.get_run(run_id)
        if run is None or run.status != DeploymentRunStatus.QUEUED:
            return
        run.status = DeploymentRunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        self._store.save_run(run)
        while True:
            run = self._store.get_run(run_id)
            if run is None:
                return
            if run.status == DeploymentRunStatus.CANCELLING or run_id in self._cancel_requested:
                self._cancel_pending(run)
                return
            cases = self._next_cases(run)
            if not cases:
                self._aggregate(run)
                return
            for case in cases:
                case.status = DeploymentCaseStatus.RENDERING
            self._store.save_run(run)
            tasks = {asyncio.create_task(self._execute_case(run, case)) for case in cases}
            self._active_tasks[run_id] = tasks
            await asyncio.gather(*tasks, return_exceptions=True)
            self._active_tasks.pop(run_id, None)
            if run_id in self._cancel_requested:
                self._cancel_pending(run)
                return
            self._store.save_run(run)
            if any(case.status == DeploymentCaseStatus.FAILED for case in cases) and run.failure_policy == "stop":
                self._cancel_pending(run)
                return

    @staticmethod
    def _next_cases(run):
        ready = {case.id for case in run.cases if case.status == DeploymentCaseStatus.READY}
        return [
            case
            for case in sorted(run.cases, key=lambda item: (item.ordinal, item.attempt))
            if case.status == DeploymentCaseStatus.QUEUED and set(case.depends_on).issubset(ready)
        ]

    async def _execute_case(self, run, case, *, model_token: str | None = None) -> None:
        try:

            def _on_initial_execution(initial_exec):
                self._store.save_execution(initial_exec)
                case.execution_id = initial_exec.execution_id
                case.status = DeploymentCaseStatus.DEPLOYING
                self._store.save_run(run)

            execution = await self._service.create(
                case.create_request,
                model_token=model_token,
                on_initial_execution=_on_initial_execution,
            )
            self._store.save_execution(execution)
            case.execution_id = execution.execution_id
            if execution.status == DeploymentStatus.READY:
                case.status = DeploymentCaseStatus.READY
                self._store.save_run(run)
                await self._enable_ready_monitoring(execution)
            elif execution.status == DeploymentStatus.DEPLOYING:
                case.status = DeploymentCaseStatus.DEPLOYING
            else:
                case.status = DeploymentCaseStatus.FAILED
                detail = self._failure_detail(execution)
                case.failure = ProblemDetails(
                    status=500,
                    title="deployment case failed",
                    detail=detail,
                    code="model_access_failed"
                    if detail.startswith("modelserver could not access model")
                    else "deployment_failed",
                )
        except DeploymentCreationCancelled as error:
            self._store.save_execution(error.execution)
            case.execution_id = error.execution.execution_id
            case.status = DeploymentCaseStatus.CANCELLED
        except asyncio.CancelledError:
            if case.execution_id is not None:
                execution = self._store.get_execution(case.execution_id)
                if execution is not None:
                    cleaned = await self._service.kill(
                        DeploymentKillRequest(execution_id=execution.execution_id, reason="deployment run cancelled"),
                        execution,
                    )
                    self._store.save_execution(cleaned)
            case.status = DeploymentCaseStatus.CANCELLED
            raise
        except Exception as error:
            case.status = DeploymentCaseStatus.FAILED
            case.failure = ProblemDetails(
                status=500,
                title="deployment case failed",
                detail=str(error),
                code="deployment_failed",
            )

    def _cancel_pending(self, run) -> None:
        self._cancel_requested.discard(run.id)
        for case in run.cases:
            if case.status in {
                DeploymentCaseStatus.QUEUED,
                DeploymentCaseStatus.RENDERING,
                DeploymentCaseStatus.DEPLOYING,
            }:
                case.status = DeploymentCaseStatus.CANCELLED
        run.status = DeploymentRunStatus.CANCELLED
        run.finished_at = datetime.now(UTC)
        self._store.save_run(run)

    def _aggregate(self, run) -> None:
        cases = self._latest_cases(run)
        if all(case.status in {DeploymentCaseStatus.CLEANED, DeploymentCaseStatus.CLEANED_UP} for case in cases):
            run.status = DeploymentRunStatus.CLEANED
        elif any(
            case.status in {DeploymentCaseStatus.QUEUED, DeploymentCaseStatus.RENDERING, DeploymentCaseStatus.DEPLOYING}
            for case in cases
        ):
            run.status = DeploymentRunStatus.RUNNING
            run.finished_at = None
            self._store.save_run(run)
            return
        elif all(case.status == DeploymentCaseStatus.READY for case in cases):
            run.status = DeploymentRunStatus.SUCCEEDED
        elif any(case.status == DeploymentCaseStatus.READY for case in cases):
            run.status = DeploymentRunStatus.PARTIALLY_SUCCEEDED
        else:
            run.status = DeploymentRunStatus.FAILED
        run.finished_at = datetime.now(UTC)
        self._store.save_run(run)

    @staticmethod
    def _latest_cases(run):
        roots = {case.parent_case_id or case.id for case in run.cases}
        return [
            max(
                (case for case in run.cases if (case.parent_case_id or case.id) == root),
                key=lambda item: item.attempt,
            )
            for root in roots
        ]

    @staticmethod
    def _failure_detail(execution) -> str:
        diagnostics = execution.diagnostics.value if execution.diagnostics else {}
        snapshot = diagnostics.get("snapshot") or {}
        modelserver_logs = str(snapshot.get("modelserver_logs") or "")
        invalid_model = re.search(
            r"(?m)^(?:\([^\n]+\)\s+)?(?:OSError:\s+)?"
            r"(?P<model>\S+) is not a local folder and is not a valid model identifier",
            modelserver_logs,
        )
        if invalid_model:
            model = invalid_model.group("model")
            return (
                f'modelserver could not access model "{model}": verify the model ID; if it is private or gated, '
                "enter a Hugging Face token below and retry"
            )
        repository_not_found = re.search(
            r"Repository Not Found for url:\s+https://huggingface\.co/(?P<model>[^/\s]+/[^/\s]+)/",
            modelserver_logs,
        )
        if repository_not_found:
            model = repository_not_found.group("model")
            return (
                f'modelserver could not access model "{model}": verify the model ID; if it is private or gated, '
                "enter a Hugging Face token below and retry"
            )
        if re.search(
            r"(?i)(cannot access gated repo|gated repo|"
            r"401 (?:client error: )?unauthorized|403 (?:client error: )?forbidden)",
            modelserver_logs,
        ):
            return (
                "modelserver could not access the Hugging Face model: if it is private or gated, "
                "enter a Hugging Face token below and retry"
            )
        return str(
            diagnostics.get("reason")
            or diagnostics.get("reasons")
            or diagnostics.get("readiness")
            or "deployment did not become ready"
        )
