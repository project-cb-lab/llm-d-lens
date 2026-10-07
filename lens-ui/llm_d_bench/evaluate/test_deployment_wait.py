"""A shared-store readiness refresh must not abort a live deployment."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

router = importlib.import_module("llm_d_bench.evaluate.router")


def deployment(status, failure=None):
    case = SimpleNamespace(status=SimpleNamespace(value=status), execution_id="execution", failure=failure)
    return SimpleNamespace(status=SimpleNamespace(value=status), cases=[case])


@pytest.mark.asyncio
async def test_wait_ignores_failed_snapshot_while_owner_still_deploying(monkeypatch):
    failed, ready = deployment("failed"), deployment("ready")
    store = Mock()
    store.get_run.side_effect = [failed, ready]
    manager = SimpleNamespace(is_run_active=lambda _: True)
    monkeypatch.setattr(router, "_store", store)
    monkeypatch.setattr(router, "deployment_run_manager", manager)
    sleep = asyncio.sleep

    async def tick(_):
        await sleep(0)

    monkeypatch.setattr(router.asyncio, "sleep", tick)
    result, case = await router._wait_for_deployment("run")
    assert result is ready
    assert case is ready.cases[0]


@pytest.mark.asyncio
async def test_wait_reports_failure_once_owner_has_finished(monkeypatch):
    failed = deployment("failed", SimpleNamespace(detail="manifest apply rejected"))
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_run=lambda _: failed))
    monkeypatch.setattr(router, "deployment_run_manager", SimpleNamespace(is_run_active=lambda _: False))
    with pytest.raises(ValueError, match="manifest apply rejected"):
        await router._wait_for_deployment("run")


def test_retry_reuses_direct_deployment_link_without_legacy_provenance(monkeypatch):
    from llm_d_bench.deploy.contracts import DeploymentStatus

    case = SimpleNamespace(status=SimpleNamespace(value="ready"), execution_id="execution", attempt=0)
    run = SimpleNamespace(cases=[case])
    store = SimpleNamespace(
        list_runs=lambda: [],
        get_run=lambda identifier: run if identifier == "linked-run" else None,
        get_execution=lambda _: SimpleNamespace(status=DeploymentStatus.READY),
    )
    monkeypatch.setattr(router, "_store", store)
    assert router._ready_deployment_case("workflow", {"id": "guide-1", "deployment_run_id": "linked-run"}) == (
        run,
        case,
    )
