"""Monitoring starts at deployment readiness, without requiring an evaluation."""

import asyncio
from types import SimpleNamespace

import pytest

from llm_d_bench.deploy.contracts import DeployableConfiguration, DeploymentCaseStatus, DeploymentRun, DeploymentStatus
from llm_d_bench.deploy.run_store import JsonDeploymentRunStore
from llm_d_bench.deploy.test_deployment_management import _case, _execution
from llm_d_bench.deploy.worker import DeploymentRunWorker
from llm_d_bench.monitoring.deployment import service as monitoring


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", [DeploymentStatus.READY, DeploymentStatus.DEPLOYING])
async def test_enables_after_ready_is_persisted_once(tmp_path, monkeypatch, initial_status):
    store = JsonDeploymentRunStore(tmp_path)
    execution = _execution("execution-1", initial_status, "serving")
    case = _case("run-1", 0, "execution-1", DeploymentCaseStatus.QUEUED)
    run = DeploymentRun(
        id="run-1",
        cases=[case],
        source_configurations=[
            DeployableConfiguration(
                type="optimized-baseline", format="helm", provider_ref="optimized-baseline", checksum="hash"
            )
        ],
    )
    store.create_run(run)
    calls = []

    async def enable(execution_id):
        assert store.get_execution(execution_id).status == DeploymentStatus.READY
        assert store.get_case("run-1", case.id).execution_id == execution_id
        calls.append(execution_id)
        return {"status": "enabled", "enabled": True, "podmonitors": ["modelserver"]}

    async def create(*args, **kwargs):
        return execution

    async def refresh(current):
        ready = _execution(current.execution_id, DeploymentStatus.READY, "serving")
        return current.model_copy(update={"status": ready.status, "endpoint": ready.endpoint})

    monkeypatch.setattr(monitoring, "enable", enable)
    worker = DeploymentRunWorker(store, SimpleNamespace(create=create, refresh_diagnostics=refresh))
    await worker._execute_case(run, case)
    if initial_status == DeploymentStatus.DEPLOYING:
        assert calls == []
        await worker.refresh_run_executions(run.id)
    assert calls == ["execution-1"]
    assert store.get_execution("execution-1").monitoring_setup["status"] == "enabled"
    # A new worker (or process restart) must not enable monitoring again.
    restarted = DeploymentRunWorker(store, worker._service)
    await restarted.refresh_run_executions(run.id)
    assert calls == ["execution-1"]


@pytest.mark.asyncio
async def test_monitoring_failure_does_not_fail_ready_deployment(tmp_path, monkeypatch):
    store = JsonDeploymentRunStore(tmp_path)
    execution = _execution("execution-1", DeploymentStatus.READY, "serving")
    case = _case("run-1", 0, "execution-1", DeploymentCaseStatus.QUEUED)
    run = DeploymentRun(
        id="run-1",
        cases=[case],
        source_configurations=[
            DeployableConfiguration(
                type="optimized-baseline", format="helm", provider_ref="optimized-baseline", checksum="hash"
            )
        ],
    )
    store.create_run(run)

    async def enable(execution_id):
        raise monitoring.DeploymentMonitoringError("stack_not_ready", "Prometheus is not ready", 409)

    async def create(*args, **kwargs):
        return execution

    monkeypatch.setattr(monitoring, "enable", enable)
    await DeploymentRunWorker(store, SimpleNamespace(create=create))._execute_case(run, case)
    saved = store.get_execution("execution-1")
    assert saved.status == DeploymentStatus.READY
    assert case.status == DeploymentCaseStatus.READY
    assert saved.monitoring_setup["status"] == "unavailable"
    assert saved.monitoring_setup["code"] == "stack_not_ready"
    assert saved.monitoring_setup["message"] == "Prometheus is not ready"


@pytest.mark.asyncio
async def test_stale_refresh_does_not_repeat_setup_or_resurrect_stopped_execution(tmp_path, monkeypatch):
    store = JsonDeploymentRunStore(tmp_path)
    execution = _execution("execution-1", DeploymentStatus.READY, "serving")
    store.save_execution(execution)
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def enable(execution_id):
        calls.append(execution_id)
        started.set()
        await finish.wait()
        return {"status": "enabled", "enabled": True}

    monkeypatch.setattr(monitoring, "enable", enable)
    worker = DeploymentRunWorker(store, SimpleNamespace())
    task = asyncio.create_task(worker._enable_ready_monitoring(execution))
    await started.wait()
    stale_enabling = store.get_execution("execution-1")
    # Provider refresh returns a snapshot taken before setup started.
    store.save_execution(execution)
    finish.set()
    await task
    store.save_execution(stale_enabling)
    await worker._enable_ready_monitoring(execution)
    assert calls == ["execution-1"]

    execution = _execution("execution-2", DeploymentStatus.READY, "serving-2")
    store.save_execution(execution)
    started.clear()
    finish.clear()
    task = asyncio.create_task(worker._enable_ready_monitoring(execution))
    await started.wait()
    store.save_execution(execution.model_copy(update={"status": DeploymentStatus.ROLLED_BACK, "endpoint": None}))
    finish.set()
    await task
    assert store.get_execution("execution-2").status == DeploymentStatus.ROLLED_BACK


@pytest.mark.asyncio
async def test_recovers_interrupted_monitoring_setup(tmp_path, monkeypatch):
    store = JsonDeploymentRunStore(tmp_path)
    execution = _execution("interrupted", DeploymentStatus.READY, "serving")
    execution.monitoring_setup = {"status": "enabling", "enabled": False}
    store.save_execution(execution)

    async def enable(execution_id):
        return {"status": "enabled", "enabled": True, "podmonitors": ["modelserver"]}

    monkeypatch.setattr(monitoring, "enable", enable)
    await DeploymentRunWorker(store, SimpleNamespace())._enable_ready_monitoring(execution)
    assert store.get_execution("interrupted").monitoring_setup["status"] == "enabled"


@pytest.mark.asyncio
async def test_fast_reconciliation_only_refreshes_pending_deployments(monkeypatch):
    import importlib

    router = importlib.import_module("llm_d_bench.deploy.router")
    from llm_d_bench.deploy.contracts import DeploymentRunStatus

    ready = SimpleNamespace(
        id="ready", status=DeploymentRunStatus.SUCCEEDED, cases=[SimpleNamespace(status=DeploymentCaseStatus.READY)]
    )
    pending = SimpleNamespace(
        id="pending", status=DeploymentRunStatus.RUNNING, cases=[SimpleNamespace(status=DeploymentCaseStatus.DEPLOYING)]
    )
    refreshed = []

    async def refresh(run, *, pending_only=False):
        refreshed.append(run.id)

    monkeypatch.setattr(router, "_store", SimpleNamespace(list_runs=lambda: [ready, pending]))
    monkeypatch.setattr(router, "_refresh_deployment_record", refresh)
    await router._refresh_all_deployment_records(pending_only=True)
    assert refreshed == ["pending"]
    refreshed.clear()
    await router._refresh_all_deployment_records()
    assert refreshed == ["ready", "pending"]
