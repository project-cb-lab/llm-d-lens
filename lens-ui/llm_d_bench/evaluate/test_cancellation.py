"""Cancellation is scoped by evaluation ownership, never endpoint provenance."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

router = importlib.import_module("llm_d_bench.evaluate.router")


@pytest.fixture
def state(monkeypatch, tmp_path):
    for name in ["_benchmark_directory", "_workflow_directory", "_results_root"]:
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setattr(router, name, path)
    for name in ["_tasks", "_workflow_tasks", "_active_workflows", "_cancellation_locks"]:
        monkeypatch.setattr(router, name, {})
    monkeypatch.setattr(router, "_restore_deployment_links", lambda workflow: False)
    worker = SimpleNamespace(cancel_evaluation_deployment=AsyncMock())
    manager = SimpleNamespace(worker_for_run=Mock(return_value=worker))
    monkeypatch.setattr(router, "deployment_run_manager", manager)
    return manager, worker


def benchmark():
    run = {
        "kind": "benchmark",
        "id": str(uuid4()),
        "status": "running",
        "deployment_ownership": "existing-endpoint",
        "metrics": {"throughput_tps": 42},
    }
    router._save(run)
    return run


def workflow(cases=None, **extra):
    record = dict(kind="workflow", id=str(uuid4()), status="running", deployment_ownership="evaluation", **extra)
    if cases is not None:
        record["cases"] = cases
    router._save(record)
    return record


async def waiting():
    await asyncio.Event().wait()


def test_restart_resumes_task_and_preserves_interrupted_attempt(state, monkeypatch):
    run = benchmark()
    parent = workflow(
        [
            {"id": "done", "status": "succeeded", "metrics": {"throughput_tps": 42}},
            {"id": "active", "status": "benchmarking", "evaluation_run_id": run["id"]},
        ]
    )
    resume = AsyncMock()
    monkeypatch.setattr(router, "_execute_evaluation", resume)

    async def check():
        await router.reconcile_evaluate_runs()
        await asyncio.gather(*router._workflow_tasks.values())

    asyncio.run(check())
    resume.assert_awaited_once_with(parent["id"])
    saved = router._get("workflow", parent["id"])
    assert saved["status"] == "running"
    assert saved["cases"][0]["metrics"]["throughput_tps"] == 42
    interrupted = router._get("benchmark", run["id"])
    assert interrupted["status"] == "failed"
    assert interrupted["error"] == "evaluation process was interrupted by a service restart"


def test_existing_endpoint_cancel_never_touches_deployment(state):
    async def check():
        run = benchmark()
        router._tasks[run["id"]] = asyncio.create_task(waiting())
        result = await router.cancel_run(run["id"])
        assert result["status"] == "cancelled"
        assert router._get("benchmark", run["id"])["metrics"]["throughput_tps"] == 42
        state[0].worker_for_run.assert_not_called()
        assert await router.cancel_run(run["id"]) == result

    asyncio.run(check())


@pytest.mark.parametrize("entry", ["workflow", "child"])
def test_legacy_workflow_and_child_cancel_settle_benchmark_and_ready_deployment(state, entry):
    async def check():
        run = benchmark()
        parent = workflow(deployment_run_id="owned-ready", evaluation_run_id=run["id"])
        router._tasks[run["id"]] = asyncio.create_task(waiting())
        router._workflow_tasks[parent["id"]] = asyncio.create_task(waiting())
        if entry == "child":
            await router.cancel_run(run["id"])
        else:
            await router.cancel_workflow_run(parent["id"])
        state[1].cancel_evaluation_deployment.assert_awaited_once_with("owned-ready")
        assert router._get("benchmark", run["id"])["status"] == "cancelled"
        assert router._get("workflow", parent["id"])["status"] == "cancelled"

    asyncio.run(check())


def test_whole_task_cleans_preserved_services_once_and_retains_completed_results(state):
    parent = workflow(
        [
            {
                "id": "done",
                "status": "succeeded",
                "deployment_run_id": "shared",
                "preserve_deployment": True,
                "metrics": {"throughput_tps": 42},
            },
            {"id": "active", "status": "benchmarking", "deployment_run_id": "shared"},
            {"id": "pending", "status": "queued"},
            {
                "id": "borrowed",
                "status": "benchmarking",
                "deployment_run_id": "external",
                "deployment_ownership": "existing-endpoint",
            },
        ]
    )
    asyncio.run(router.cancel_workflow_run(parent["id"]))
    state[1].cancel_evaluation_deployment.assert_awaited_once_with("shared")
    saved = router._get("workflow", parent["id"])
    assert [c["status"] for c in saved["cases"]] == ["succeeded", "cancelled", "cancelled", "cancelled"]
    assert saved["cases"][0]["metrics"]["throughput_tps"] == 42


@pytest.mark.parametrize("entry", ["workflow", "case", "child"])
def test_borrowed_workflow_links_do_not_grant_ownership(state, entry):
    async def check():
        run = benchmark()
        parent = workflow(
            [
                {
                    "id": "borrowed",
                    "status": "benchmarking",
                    "deployment_run_id": "external",
                    "evaluation_run_id": run["id"],
                }
            ]
        )
        parent["deployment_ownership"] = "existing-endpoint"
        router._save(parent)
        router._tasks[run["id"]] = asyncio.create_task(waiting())
        if entry == "workflow":
            await router.cancel_workflow_run(parent["id"])
        elif entry == "case":
            await router.cancel_evaluation_case(parent["id"], "borrowed")
        else:
            await router.cancel_run(run["id"])
        state[0].worker_for_run.assert_not_called()
        assert router._get("benchmark", run["id"])["status"] == "cancelled"

    asyncio.run(check())


def test_cancel_active_shared_case_updates_live_state_and_resumes_unrelated_case(state, monkeypatch):
    async def check():
        run = benchmark()
        parent = workflow(
            [
                {
                    "id": "guide",
                    "status": "benchmarking",
                    "deployment_run_id": "shared",
                    "evaluation_run_id": run["id"],
                },
                {"id": "baseline", "status": "queued", "dependent_guide_case_id": "guide"},
                {"id": "next", "status": "queued"},
            ],
            active_case_id="guide",
        )
        router._active_workflows[parent["id"]] = parent
        router._tasks[run["id"]] = asyncio.create_task(waiting())
        router._workflow_tasks[parent["id"]] = asyncio.create_task(waiting())
        resume = AsyncMock()
        monkeypatch.setattr(router, "_execute_evaluation", resume)
        await router.cancel_evaluation_case(parent["id"], "guide")
        await asyncio.sleep(0)
        saved = router._get("workflow", parent["id"])
        assert [c["status"] for c in saved["cases"]] == ["cancelled", "cancelled", "queued"]
        assert saved["status"] == "running"
        assert parent["cases"][0]["cancel_requested"]
        resume.assert_awaited_once_with(parent["id"])
        state[1].cancel_evaluation_deployment.assert_awaited_once_with("shared")

    asyncio.run(check())


def test_cleanup_error_stays_cancelling_until_retry_succeeds(state):
    parent = workflow([{"id": "active", "status": "deploying", "deployment_run_id": "owned"}])
    state[1].cancel_evaluation_deployment.side_effect = ValueError("cluster unavailable")
    with pytest.raises(router.HTTPException, match="cleanup must be retried"):
        asyncio.run(router.cancel_workflow_run(parent["id"]))
    saved = router._get("workflow", parent["id"])
    assert saved["status"] == "cancelling"
    assert "cluster unavailable" in saved["cancellation_error"]
    state[1].cancel_evaluation_deployment.side_effect = None
    asyncio.run(router.cancel_workflow_run(parent["id"]))
    assert router._get("workflow", parent["id"])["status"] == "cancelled"


def test_case_cleanup_retry_resumes_remaining_cases(state, monkeypatch):
    async def check():
        parent = workflow(
            [
                {"id": "active", "status": "deploying", "deployment_run_id": "owned"},
                {"id": "next", "status": "queued"},
            ],
            active_case_id="active",
        )
        router._active_workflows[parent["id"]] = parent
        router._workflow_tasks[parent["id"]] = asyncio.create_task(waiting())
        resume = AsyncMock()
        monkeypatch.setattr(router, "_execute_evaluation", resume)
        state[1].cancel_evaluation_deployment.side_effect = ValueError("cluster disconnected")
        with pytest.raises(router.HTTPException):
            await router.cancel_evaluation_case(parent["id"], "active")
        parent["active_case_id"] = None
        state[1].cancel_evaluation_deployment.side_effect = None
        await router.cancel_evaluation_case(parent["id"], "active")
        await asyncio.sleep(0)
        resume.assert_awaited_once_with(parent["id"])
        assert router._get("workflow", parent["id"])["status"] == "running"

    asyncio.run(check())


def test_restart_finishes_cancellation_instead_of_redeploying(state, monkeypatch):
    parent = workflow([{"id": "active", "status": "cancelling", "deployment_run_id": "owned"}])
    parent["status"] = "cancelling"
    router._save(parent)
    resume = AsyncMock()
    monkeypatch.setattr(router, "_execute_evaluation", resume)
    asyncio.run(router.reconcile_evaluate_runs())
    state[1].cancel_evaluation_deployment.assert_awaited_once_with("owned")
    resume.assert_not_awaited()
    assert router._get("workflow", parent["id"])["status"] == "cancelled"


def test_simultaneous_existing_endpoint_cancels_wait_for_cleanup_and_keep_final_metrics(state):
    async def check():
        run = benchmark()
        entered = asyncio.Event()

        async def execute():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                final = router._get("benchmark", run["id"])
                final["metrics"]["final_sample"] = 99
                router._save(final)

        router._tasks[run["id"]] = asyncio.create_task(execute())
        await entered.wait()
        results = await asyncio.gather(router.cancel_run(run["id"]), router.cancel_run(run["id"]))
        assert all(result["status"] == "cancelled" for result in results)
        assert router._get("benchmark", run["id"])["metrics"]["final_sample"] == 99
        state[0].worker_for_run.assert_not_called()

    asyncio.run(check())
