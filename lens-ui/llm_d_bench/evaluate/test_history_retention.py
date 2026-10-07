"""History belongs to the benchmark task, independently of serving resources."""

import asyncio
import importlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

router = importlib.import_module("llm_d_bench.evaluate.router")


@pytest.fixture
def records(monkeypatch, tmp_path):
    for name in ["_benchmark_directory", "_workflow_directory", "_results_root"]:
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setattr(router, name, path)
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_run=lambda _: None))


def save_run(parent_id=None):
    run = {
        "kind": "benchmark",
        "id": str(uuid4()),
        "status": "succeeded",
        "metrics": {"observability": {"window": {"start": "start", "end": "end"}, "per_pod": [{"pod": "decode-0"}]}},
    }
    if parent_id:
        run["evaluation_workflow_id"] = parent_id
    router._save(run)
    output = router._results_root / run["id"]
    output.mkdir(exist_ok=True)
    (output / "report.json").write_text('{"recorded": true}')
    return run, output


@pytest.mark.parametrize("legacy", [False, True])
def test_task_history_survives_restart_and_missing_deployment_until_task_deleted(records, legacy):
    parent_id = str(uuid4())
    run, output = save_run(parent_id)
    old_run, old_output = save_run(parent_id)
    unrelated, unrelated_output = save_run()
    workflow = {"kind": "workflow", "id": parent_id, "status": "succeeded", "deployment_run_id": "already-deleted"}
    if legacy:
        workflow["evaluation_run_id"] = run["id"]
    else:
        workflow["cases"] = [
            {"id": "case", "status": "succeeded", "evaluation_run_id": run["id"], "metrics": run["metrics"]}
        ]
    router._save(workflow)
    asyncio.run(router.reconcile_evaluate_runs())
    assert router._get("benchmark", run["id"])["metrics"] == run["metrics"]
    assert output.exists()
    with pytest.raises(router.HTTPException) as error:
        asyncio.run(router.delete_benchmark_run(run["id"]))
    assert error.value.status_code == 409
    # A superseded retry remains protected by its parent identity too.
    with pytest.raises(router.HTTPException):
        asyncio.run(router.delete_benchmark_run(old_run["id"]))
    asyncio.run(router.delete_workflow_run(parent_id))
    assert router._get("workflow", parent_id) is None
    assert router._get("benchmark", run["id"]) is None
    assert router._get("benchmark", old_run["id"]) is None
    assert not output.exists() and not old_output.exists()
    assert unrelated_output.exists()
    assert router._get("benchmark", unrelated["id"]) is not None


def test_deleting_history_never_follows_an_artifact_directory_symlink(records, tmp_path):
    run, output = save_run()
    for payload in output.iterdir():
        payload.unlink()
    output.rmdir()
    external = tmp_path / "unrelated"
    external.mkdir()
    (external / "keep").write_text("keep")
    output.symlink_to(external, target_is_directory=True)
    asyncio.run(router.delete_benchmark_run(run["id"]))
    assert (external / "keep").read_text() == "keep"
    assert not output.is_symlink()
