"""Persistent asynchronous install operations for accelerator observability.

Backed by ``MonitoringAcceleratorOperationDao`` (SQLAlchemy) -- see
design doc section 5.4.13. ``root`` is accepted but ignored so existing
call sites/tests that construct ``AcceleratorOperationStore(tmp_path)`` keep
working unchanged; isolation now comes from the per-test database engine
instead of a per-instance directory.
"""

from __future__ import annotations

from llm_d_bench.monitoring.operation_runtime import MonitoringOperationManager, installation_environment, run_install_command

import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from llm_d_bench.db.dao.monitoring_accelerator_operation import (
    MonitoringAcceleratorOperationDao,
)
from llm_d_bench.utils.kubernetes import kubeconfig_environment
from llm_d_bench.utils.shell import shell

from .models import (
    AcceleratorInstallRequest,
    AcceleratorOperationResponse,
    AcceleratorStatusResponse,
    OperationError,
    OperationLogEntry,
)

Verifier = Callable[[str], Awaitable[AcceleratorStatusResponse]]
PostInstall = Callable[[], Awaitable[None]]

# After ``helm upgrade`` returns, pods are typically still starting, so a single
# immediate status read reports "installing"/"progressing". Poll the verifier
# (bounded) so a healthy rollout is not reported as a failed install.
_VERIFY_TIMEOUT_SECONDS = float(os.getenv("MONITORING_VERIFY_TIMEOUT_SECONDS", "300"))
_VERIFY_POLL_INTERVAL_SECONDS = 5.0
_PENDING_STATUSES = {"installing", "progressing"}


class AcceleratorOperationStore:
    def __init__(self, root: Path | None = None) -> None:
        del root  # unused -- see module docstring
        self._dao = MonitoringAcceleratorOperationDao()

    def save(self, operation: AcceleratorOperationResponse, cluster_id: str | None = None) -> None:
        self._dao.save(operation, cluster_id=cluster_id)

    def load(self, operation_id: str) -> AcceleratorOperationResponse | None:
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            return None
        return self._dao.load(operation_id)

    def recover_interrupted(self) -> list[AcceleratorOperationResponse]:
        return self._dao.recover_interrupted()


class AcceleratorOperationManager(MonitoringOperationManager):
    def __init__(self, store: AcceleratorOperationStore | None = None) -> None:
        super().__init__(store or AcceleratorOperationStore(), OperationLogEntry)

    async def create(
        self,
        request: AcceleratorInstallRequest,
        *,
        context: str | None,
        argv: list[str],
        verifier: Verifier,
        idempotency_key: str | None,
        cluster_id: str | None = None,
        post_install: PostInstall | None = None,
    ) -> AcceleratorOperationResponse:
        key = (context or "", request.namespace)
        return await self.queue_operation(
            key=key, idempotency_key=idempotency_key, cluster_id=cluster_id,
            create=lambda: AcceleratorOperationResponse(
                operation_id=uuid.uuid4().hex,
                status="queued",
                phase="preflight",
                accelerator=request.accelerator,
                access_mode=request.access_mode,
                namespace=request.namespace,
            ),
            run=lambda operation: self._run(operation, request, key, argv, verifier, post_install, cluster_id),
        )

    async def _run(
        self,
        operation: AcceleratorOperationResponse,
        request: AcceleratorInstallRequest,
        key: tuple[str, str],
        argv: list[str],
        verifier: Verifier,
        post_install: PostInstall | None,
        cluster_id: str | None = None,
    ) -> None:
        operation.status = "running"
        operation.phase = "invoking_helm"
        operation.started_at = datetime.now(UTC)
        self.store.save(operation, cluster_id=cluster_id)
        allowed_env = installation_environment(cluster_id, kubeconfig_environment)
        timeout = float(os.getenv("MONITORING_INSTALL_TIMEOUT_SECONDS", "900"))
        try:
            def on_line(line):
                lowered = line.lower()
                if "waiting for" in lowered or "pending" in lowered:
                    operation.phase = "waiting_for_pods"
                elif "servicemonitor" in lowered or "grafana" in lowered:
                    operation.phase = "configuring_monitoring"
                elif "helm" in lowered or "chart" in lowered:
                    operation.phase = "installing_chart"
                self._append(operation, line)
                self.store.save(operation, cluster_id=cluster_id)

            operation.exit_code = await run_install_command(
                argv, spawn=shell.spawn, env=allowed_env, timeout=timeout, on_line=on_line,
            )
            if operation.exit_code != 0:
                raise RuntimeError(f"helm install exited with code {operation.exit_code}")
            if post_install is not None:
                operation.phase = "configuring_monitoring"
                self.store.save(operation, cluster_id=cluster_id)
                await post_install()
            operation.phase = "verifying"
            self.store.save(operation, cluster_id=cluster_id)
            verify_deadline = time.monotonic() + _VERIFY_TIMEOUT_SECONDS
            snapshot = await verifier(request.namespace)
            while snapshot.status in _PENDING_STATUSES and time.monotonic() < verify_deadline:
                self._append(
                    operation,
                    f"Waiting for observability components to become ready (status: {snapshot.status})…",
                )
                self.store.save(operation, cluster_id=cluster_id)
                await asyncio.sleep(_VERIFY_POLL_INTERVAL_SECONDS)
                snapshot = await verifier(request.namespace)
            if snapshot.status != "ready":
                raise RuntimeError(f"Installation completed but accelerator status is {snapshot.status}")
            operation.status = "succeeded"
            operation.phase = "completed"
            self._append(operation, "Intel GPU observability installation verified")
        except TimeoutError:
            operation.status = "failed"
            operation.error = OperationError(
                code="COMMAND_TIMEOUT",
                message="Intel GPU observability installation timed out",
                retryable=True,
            )
            self._append(operation, operation.error.message, "error")
        except Exception as error:
            operation.status = "failed"
            code = "INSTALL_VERIFICATION_FAILED" if operation.phase == "verifying" else "INSTALL_FAILED"
            operation.error = OperationError(code=code, message=str(error), retryable=True)
            self._append(operation, str(error), "error")
        finally:
            operation.finished_at = datetime.now(UTC)
            self._active.pop(key, None)
            self.store.save(operation, cluster_id=cluster_id)
