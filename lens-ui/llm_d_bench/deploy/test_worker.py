"""Deployment worker failure reporting tests."""

from types import SimpleNamespace

from llm_d_bench.deploy.worker import DeploymentRunWorker


def test_failure_detail_reports_invalid_hugging_face_model() -> None:
    execution = SimpleNamespace(
        diagnostics=SimpleNamespace(
            value={
                "reasons": ['error: deployment "modelserver" exceeded its progress deadline'],
                "snapshot": {
                    "modelserver_logs": (
                        "(APIServer pid=1) OSError: Qwen/Qwen3-0.3B is not a local folder and is not a valid "
                        "model identifier listed on 'https://huggingface.co/models'"
                    ),
                },
            }
        )
    )

    assert DeploymentRunWorker._failure_detail(execution) == (
        'modelserver could not access model "Qwen/Qwen3-0.3B": verify the model ID; if it is private or gated, '
        "enter a Hugging Face token below and retry"
    )


def test_failure_detail_reports_gated_hugging_face_model() -> None:
    execution = SimpleNamespace(
        diagnostics=SimpleNamespace(
            value={
                "snapshot": {
                    "modelserver_logs": "OSError: You are trying to access a gated repo. Request access first."
                }
            }
        )
    )

    assert DeploymentRunWorker._failure_detail(execution) == (
        "modelserver could not access the Hugging Face model: if it is private or gated, "
        "enter a Hugging Face token below and retry"
    )


def test_failure_detail_falls_back_to_readiness_reason() -> None:
    execution = SimpleNamespace(
        diagnostics=SimpleNamespace(
            value={"reasons": ['error: deployment "modelserver" exceeded its progress deadline']}
        )
    )

    assert DeploymentRunWorker._failure_detail(execution) == (
        "['error: deployment \"modelserver\" exceeded its progress deadline']"
    )


def test_failure_detail_reports_refreshed_readiness_error() -> None:
    execution = SimpleNamespace(
        diagnostics=SimpleNamespace(
            value={
                "readiness": ['deployments.apps "modelserver" not found'],
            }
        )
    )
    assert "modelserver" in DeploymentRunWorker._failure_detail(execution)


def test_evaluation_cancel_cleans_ready_and_inflight_resources_and_updates_run():
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from llm_d_bench.deploy.contracts import DeploymentCaseStatus, DeploymentRunStatus, DeploymentStatus

    async def check():
        ready = SimpleNamespace(id="ready", execution_id="e1", status=DeploymentCaseStatus.READY)
        deploying = SimpleNamespace(id="deploying", execution_id="e2", status=DeploymentCaseStatus.DEPLOYING)
        cleaned = SimpleNamespace(id="cleaned", execution_id="e3", status=DeploymentCaseStatus.CLEANED)
        pending = SimpleNamespace(id="pending", execution_id=None, status=DeploymentCaseStatus.QUEUED)
        run = SimpleNamespace(id="run", status=DeploymentRunStatus.RUNNING, cases=[ready, deploying, cleaned, pending])
        store = SimpleNamespace(get_run=lambda _: run, get_execution=lambda _: None, save_run=Mock())
        worker = DeploymentRunWorker(store, SimpleNamespace())
        order = []

        async def settle(_):
            order.append("settled")
            pending.status = DeploymentCaseStatus.CANCELLED
            deploying.status = DeploymentCaseStatus.CANCELLED

        async def clean(_, case_id):
            assert order[0] == "settled"
            order.append(case_id)
            next(case for case in run.cases if case.id == case_id).status = DeploymentCaseStatus.CLEANED
            return None, SimpleNamespace(status=DeploymentStatus.CLEANED)

        worker._cancel_and_wait = AsyncMock(side_effect=settle)
        worker.clean_case = AsyncMock(side_effect=clean)
        await worker.cancel_evaluation_deployment("run")
        assert order == ["settled", "ready", "deploying"]
        assert run.status == DeploymentRunStatus.CANCELLED
        assert ready.status == deploying.status == DeploymentCaseStatus.CLEANED
        assert pending.status == DeploymentCaseStatus.CANCELLED
        assert run.finished_at is not None

    asyncio.run(check())


def test_evaluation_cancel_does_not_report_success_when_cleanup_returns_ready():
    import asyncio
    from unittest.mock import AsyncMock, Mock

    import pytest

    from llm_d_bench.deploy.contracts import DeploymentCaseStatus, DeploymentRunStatus, DeploymentStatus

    case = SimpleNamespace(id="ready", execution_id="e1", status=DeploymentCaseStatus.READY)
    run = SimpleNamespace(id="run", status=DeploymentRunStatus.SUCCEEDED, cases=[case])
    store = SimpleNamespace(get_run=lambda _: run, get_execution=lambda _: None, save_run=Mock())
    worker = DeploymentRunWorker(store, SimpleNamespace())
    worker._cancel_and_wait = AsyncMock()
    worker.clean_case = AsyncMock(return_value=(case, SimpleNamespace(status=DeploymentStatus.READY)))
    with pytest.raises(ValueError, match="did not confirm resource removal"):
        asyncio.run(worker.cancel_evaluation_deployment("run"))
    assert run.status == DeploymentRunStatus.SUCCEEDED


def test_cancel_is_not_overwritten_by_inflight_run_snapshot():
    import asyncio
    from copy import deepcopy

    from llm_d_bench.deploy.contracts import DeploymentCaseStatus, DeploymentRunStatus

    async def check():
        run = SimpleNamespace(
            id="run",
            status=DeploymentRunStatus.QUEUED,
            failure_policy="stop",
            cases=[
                SimpleNamespace(
                    id="one", execution_id=None, status=DeploymentCaseStatus.QUEUED, ordinal=0, attempt=1, depends_on=[]
                ),
            ],
        )
        saved = [deepcopy(run)]
        store = SimpleNamespace(
            get_run=lambda _: deepcopy(saved[0]), save_run=lambda value: saved.__setitem__(0, deepcopy(value))
        )
        worker = DeploymentRunWorker(store, SimpleNamespace())
        entered = asyncio.Event()

        async def execute(stale_run, case):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                case.status = DeploymentCaseStatus.CANCELLED
                store.save_run(stale_run)

        worker._execute_case = execute
        task = asyncio.create_task(worker.execute_run("run"))
        await entered.wait()
        worker.request_cancel("run")
        await task
        assert saved[0].status == DeploymentRunStatus.CANCELLED
        assert saved[0].cases[0].status == DeploymentCaseStatus.CANCELLED

    asyncio.run(check())
