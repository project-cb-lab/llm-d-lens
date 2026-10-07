"""Deployment evidence paths and honest snapshot completeness."""

import json

import pytest

from llm_d_bench.deploy.service import ExecutionLogArchive


def test_default_deployment_logs_register_partial_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    archive = ExecutionLogArchive()
    archive.append_snapshot("execution-1", "ready", {"modelserver_logs": "tail only"})
    root = tmp_path / "artifacts/deployments/execution-1"
    assert (root / "logs/entries.jsonl").is_file()
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["truncated"] is True
    assert manifest["status"] == "running"
    assert manifest["files"][0]["path"] == "logs/entries.jsonl"
    assert archive.read_entries("execution-1")[0].message == "tail only"
    with pytest.raises(ValueError):
        archive.append_snapshot("../escape", "ready", {})


def test_deployment_archive_rejects_symlink_before_writing(tmp_path):
    archive = ExecutionLogArchive(tmp_path / "owned")
    root = tmp_path / "owned/logs/execution"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    (root / "entries.jsonl").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        archive.append_snapshot("execution", "ready", {"modelserver_logs": "tail"})
    assert outside.read_text() == "unchanged"


def test_archive_rejects_sibling_owner_symlink(tmp_path):
    archive = ExecutionLogArchive(tmp_path)
    archive.append_snapshot("sibling", "ready", {"modelserver_logs": "original"})
    (tmp_path / "logs/owner").symlink_to(tmp_path / "logs/sibling", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        archive.append_snapshot("owner", "ready", {"modelserver_logs": "injected"})
    assert [entry.message for entry in archive.read_entries("sibling")] == ["original"]
