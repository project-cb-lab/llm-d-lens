"""Persistent asynchronous install operations for the cluster monitoring stack.

Backed by ``MonitoringClusterStackOperationDao`` (SQLAlchemy) -- see
design doc section 5.4.14. ``root`` is accepted but ignored so existing
call sites/tests that construct ``OperationStore(tmp_path)`` keep working
unchanged; isolation now comes from the per-test database engine instead
of a per-instance directory.
"""

from __future__ import annotations

from llm_d_bench.monitoring.operation_runtime import MonitoringOperationManager, installation_environment, run_install_command

import os
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from llm_d_bench.db.dao.monitoring_cluster_stack_operation import (
    MonitoringClusterStackOperationDao,
)
from llm_d_bench.utils.kubernetes import kubeconfig_environment
from llm_d_bench.utils.shell import shell

from .models import (
    ClusterStackInstallRequest,
    ClusterStackOperationResponse,
    ClusterStackStatusResponse,
    OperationError,
    OperationLogEntry,
)

Verifier = Callable[[str], Awaitable[ClusterStackStatusResponse]]


class OperationStore:
    def __init__(self, root: Path | None = None) -> None:
        del root  # unused -- see module docstring
        self._dao = MonitoringClusterStackOperationDao()

    def save(self, operation: ClusterStackOperationResponse, cluster_id: str | None = None) -> None:
        self._dao.save(operation, cluster_id=cluster_id)

    def load(self, operation_id: str) -> ClusterStackOperationResponse | None:
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            return None
        return self._dao.load(operation_id)

    def recover_interrupted(self) -> list[ClusterStackOperationResponse]:
        return self._dao.recover_interrupted()


class ClusterStackOperationManager(MonitoringOperationManager):
    def __init__(self, store: OperationStore | None = None) -> None:
        super().__init__(store or OperationStore(), OperationLogEntry)

    async def create(
        self,
        request: ClusterStackInstallRequest,
        *,
        context: str | None,
        script: Path,
        verifier: Verifier,
        idempotency_key: str | None,
        cluster_id: str | None = None,
    ) -> ClusterStackOperationResponse:
        key = (context or "", request.namespace)
        return await self.queue_operation(
            key=key, idempotency_key=idempotency_key, cluster_id=cluster_id,
            create=lambda: ClusterStackOperationResponse(
                operation_id=uuid.uuid4().hex,
                status="queued",
                phase="preflight",
                namespace=request.namespace,
            ),
            run=lambda operation: self._run(operation, request, key, script, verifier, cluster_id),
        )

    async def _run(
        self,
        operation: ClusterStackOperationResponse,
        request: ClusterStackInstallRequest,
        key: tuple[str, str],
        script: Path,
        verifier: Verifier,
        cluster_id: str | None = None,
    ) -> None:
        operation.status = "running"
        operation.started_at = datetime.now(UTC)
        self.store.save(operation, cluster_id=cluster_id)

        install_args = ["--namespace", request.namespace]
        if request.mode == "individual":
            install_args.append("--individual")
        if request.enable_tls:
            install_args.append("--enable-tls")

        commands: list[tuple[list[str], str]] = []
        if request.reinstall:
            commands.append((["--namespace", request.namespace, "--uninstall"], "uninstalling"))
            self._append(operation, "Reinstalling: removing the existing stack and its metrics data", "warning")
        commands.append((install_args, "invoking_installer"))

        try:
            for args, phase in commands:
                operation.phase = phase
                self.store.save(operation, cluster_id=cluster_id)
                operation.exit_code = await self._run_command(operation, args, script, cluster_id)
                if operation.exit_code != 0:
                    raise RuntimeError(f"Installer exited with code {operation.exit_code}")
            operation.phase = "verifying"
            self.store.save(operation, cluster_id=cluster_id)
            snapshot = await verifier(request.namespace)
            if snapshot.status != "ready":
                raise RuntimeError(f"Installation completed but stack status is {snapshot.status}")
            operation.status = "succeeded"
            operation.phase = "completed"
            self._append(operation, "Monitoring stack installation verified")
        except TimeoutError:
            operation.status = "failed"
            operation.error = OperationError(
                code="COMMAND_TIMEOUT",
                message="Monitoring installation timed out",
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

    async def _run_command(
        self,
        operation: ClusterStackOperationResponse,
        args: list[str],
        script: Path,
        cluster_id: str | None = None,
    ) -> int:
        allowed_env = installation_environment(cluster_id, kubeconfig_environment)
        timeout = float(os.getenv("MONITORING_INSTALL_TIMEOUT_SECONDS", "900"))
        def on_line(line):
            lowered = line.lower()
            if "uninstall" in lowered or "deleting" in lowered:
                operation.phase = "uninstalling"
            elif "dashboard" in lowered:
                operation.phase = "loading_dashboards"
            elif "waiting for" in lowered:
                operation.phase = "waiting_for_components"
            elif "crd" in lowered:
                operation.phase = "installing_crds"
            elif "helm" in lowered or "prometheus stack" in lowered:
                operation.phase = "installing_chart"
            self._append(operation, line)
            self.store.save(operation, cluster_id=cluster_id)

        return await run_install_command(
            [str(script), *args],
            spawn=lambda argv, **options: shell.spawn_with_executable(script, argv[1:], **options),
            env=allowed_env,
            timeout=timeout,
            on_line=on_line,
            cwd=script.parent,
        )
