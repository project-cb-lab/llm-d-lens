"""Evaluation evidence registration preserves native results and bounded-log truth."""

import asyncio
import importlib
import json
import sys
from uuid import uuid4

import pytest

router = importlib.import_module("llm_d_bench.evaluate.router")


@pytest.fixture
def evidence(monkeypatch, tmp_path):
    for name in ("_benchmark_directory", "_workflow_directory", "_results_root"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setattr(router, name, directory)
    return router._results_root


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_terminal_save_registers_real_nested_results(evidence, status):
    run_id = str(uuid4())
    output = evidence / run_id
    reports = output / "point-1" / "reports"
    reports.mkdir(parents=True)
    (reports / "stage_0_lifecycle_metrics.json").write_text('{"actual": 42}')
    run = {
        "kind": "benchmark",
        "id": run_id,
        "status": status,
        "benchmark_runtime": {"ref": "v1", "resolved_from": "cluster-registry"},
        "configuration": {"configuration_artifact_id": "config-123"},
        "stdout": "tail",
        "stdout_truncated": True,
    }
    router._save(run)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["owner_type"] == "evaluation"
    assert manifest["status"] == status
    assert manifest["configuration_ids"] == ["config-123"]
    assert manifest["source_version"]["benchmark"]["ref"] == "v1"
    assert manifest["source_version"]["benchmark"]["commit"] == "unknown"
    files = {item["path"]: item for item in manifest["files"]}
    assert "point-1/reports/stage_0_lifecycle_metrics.json" in files
    assert files["evaluation-record.json"]["truncated"] is True
    assert run["artifact_ref"] == f"lens-artifact://evaluation/{run_id}/manifest.json"
    assert router._get("benchmark", run_id)["artifact_manifest"] == manifest
    assert "artifact_manifest" not in json.loads((output / "evaluation-record.json").read_text())


@pytest.mark.asyncio
async def test_full_process_logs_survive_bounded_stdout(evidence, monkeypatch):
    monkeypatch.setattr(router, "_save", lambda run: None)
    run_id = str(uuid4())
    (evidence / run_id).mkdir()
    run = {"kind": "benchmark", "id": run_id, "status": "running"}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        'import sys; sys.stdout.write("x" * 50000)',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, _ = await router._stream_benchmark_process(process, run)
    assert len(stdout) == 32768
    assert run["stdout_truncated"] is True
    assert (evidence / run_id / "process.stdout.log").read_bytes() == b"x" * 50000


def test_registration_does_not_follow_result_root_symlinks(evidence, tmp_path):
    run_id = str(uuid4())
    external = tmp_path / "external"
    external.mkdir()
    (evidence / run_id).symlink_to(external, target_is_directory=True)
    run = {"kind": "benchmark", "id": run_id, "status": "failed"}
    with pytest.raises(ValueError, match="outside|symlink"):
        router._save(run)
    assert not (external / "evaluation-record.json").exists()


@pytest.mark.asyncio
async def test_setup_failure_still_registers_result_snapshot(evidence, monkeypatch):
    from types import SimpleNamespace

    run_id = str(uuid4())
    run = {"kind": "benchmark", "id": run_id, "status": "queued", "deployment_execution_id": "missing"}
    router._save(run)
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_execution=lambda _: None))
    monkeypatch.setattr(router, "_enrich_benchmark_result", lambda _: None)
    await router._execute(run_id)
    saved = router._get("benchmark", run_id)
    assert saved["status"] == "failed"
    assert saved["artifact_manifest"]["status"] == "failed"
    snapshot = json.loads((evidence / run_id / "evaluation-record.json").read_text())
    assert snapshot["error"] == "deployment execution is not ready for evaluation"


@pytest.mark.asyncio
async def test_timeout_marks_persisted_process_logs_incomplete(evidence, monkeypatch):
    monkeypatch.setattr(router, "_save", lambda run: None)
    run_id = str(uuid4())
    (evidence / run_id).mkdir()
    run = {"kind": "benchmark", "id": run_id, "status": "running", "wait_timeout_seconds": 0.2}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        'import time; print("started"); time.sleep(30)',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    with pytest.raises(RuntimeError, match="execution timeout"):
        await router._stream_benchmark_process(process, run)
    run["status"] = "failed"
    router._register_evaluation_artifacts(run)
    files = {item["path"]: item for item in run["artifact_manifest"]["files"]}
    assert files["process.stdout.log"]["truncated"] is True


def test_cancelled_before_start_has_durable_snapshot(evidence):
    run_id = str(uuid4())
    run = {"kind": "benchmark", "id": run_id, "status": "cancelled"}
    router._save(run)
    assert (evidence / run_id / "evaluation-record.json").is_file()
    assert run["artifact_manifest"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_restart_marks_abandoned_logs_incomplete(evidence):
    run_id = str(uuid4())
    output = evidence / run_id
    output.mkdir()
    (output / "process.stdout.log").write_text("before restart")
    router._save({"kind": "benchmark", "id": run_id, "status": "running"})
    await router.reconcile_evaluate_runs()
    saved = router._get("benchmark", run_id)
    files = {item["path"]: item for item in saved["artifact_manifest"]["files"]}
    assert saved["status"] == "failed"
    assert files["process.stdout.log"]["truncated"] is True
