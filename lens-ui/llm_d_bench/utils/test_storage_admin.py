import json

import pytest

from llm_d_bench.utils.storage_admin import migrate_tree, retention_inventory


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    for name in (
        "CONFIGURATION_ARTIFACT_DIR",
        "CONFIGURATION_OUTPUT_DIR",
        "LLM_D_BENCH_EVALUATE_RESULTS_DIR",
        "LLM_D_BENCH_EVALUATE_STORE",
        "SIMULATION_TASK_ROOT",
        "LLM_D_BENCH_DEPLOYMENT_MANIFEST_DIR",
        "TRACE_REPLAY_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_migration_dry_run_apply_conflict_and_idempotence(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    source.mkdir()
    (source / "record.json").write_text(json.dumps({"task_dir": str(source / "task"), "message": str(source / "task")}))
    mappings = [(source, target)]
    report = migrate_tree(source, target, mappings=mappings)
    assert report["copied"] == 0 and report["pending"] == 1
    assert not target.exists()
    report = migrate_tree(source, target, mappings=mappings, apply=True)
    assert report["copied"] == 1
    record = json.loads((target / "record.json").read_text())
    assert record["task_dir"] == str(target / "task")
    assert record["message"] == str(source / "task")
    assert migrate_tree(source, target, mappings=mappings, apply=True)["unchanged"] == 1
    (target / "record.json").write_text("different")
    assert migrate_tree(source, target, mappings=mappings, apply=True)["conflicts"]
    assert (target / "record.json").read_text() == "different"
    assert (source / "record.json").exists()


def test_migration_does_not_follow_links(tmp_path):
    source = tmp_path / "old"
    source.mkdir()
    (source / "secret").symlink_to("/etc/passwd")
    report = migrate_tree(source, tmp_path / "new", apply=True)
    assert report["skipped"] == 1
    assert not (tmp_path / "new/secret").exists()


def test_retention_inventory_protects_active_and_configurations(tmp_path):
    for name, status, retention in [
        ("a", "running", "diagnostic"),
        ("b", "complete", "configuration"),
        ("c", "failed", "diagnostic"),
    ]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "artifact-manifest.v1",
                    "status": status,
                    "retention_class": retention,
                    "updated_at": "2020-01-01T00:00:00+00:00",
                    "files": [],
                    "owner_id": name,
                }
            )
        )
    rows = retention_inventory(tmp_path, days=30)
    assert [row["owner_id"] for row in rows if row["eligible"]] == ["c"]
    assert all((tmp_path / name).exists() for name in "abc")


def test_flat_workflow_and_result_path_migration(tmp_path):
    old = tmp_path / "evaluate"
    old.mkdir()
    result_old, result_new = tmp_path / "old-results", tmp_path / "results"
    for filename, record in [("workflow", {"kind": "workflow"}), ("legacy", {"deployment_run_id": "dep"})]:
        (old / f"{filename}.json").write_text(json.dumps({**record, "output": str(result_old / "run")}))
    new = tmp_path / "new"
    migrate_tree(old, new, mappings=[(result_old, result_new)], apply=True)
    for filename in ["workflow", "legacy"]:
        record = json.loads((new / "workflows" / f"{filename}.json").read_text())
        assert record["kind"] == "workflow"
        assert record["output"] == str(result_new / "run")


def test_migrated_cache_executable_keeps_owner_execute(tmp_path):
    old = tmp_path / "cache"
    old.mkdir()
    script = old / "tool"
    script.write_text("#!/bin/sh")
    script.chmod(0o755)
    new = tmp_path / "new"
    migrate_tree(old, new, apply=True)
    assert (new / "tool").stat().st_mode & 0o777 == 0o700


def test_backfill_rebuilds_immutable_configuration_from_snapshot(monkeypatch, tmp_path):
    from llm_d_bench.utils.storage_admin import reindex_artifacts

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    records = tmp_path / "metadata/configurations"
    records.mkdir(parents=True)
    record = {
        "artifact_id": "config-one",
        "configuration_file": {"file_name": "same.yaml", "path": "/configs/same.yaml"},
        "deployable_configuration": {
            "content": {"officialGuide": {"renderedManifest": "kind: Pod\n", "source": {"commit": "abc"}}}
        },
    }
    (records / "config-one.json").write_text(json.dumps(record))
    assert reindex_artifacts()["registered"] == 0
    assert not (tmp_path / "artifacts").exists()
    assert reindex_artifacts(apply=True)["registered"] == 1
    folder = tmp_path / "artifacts/configurations/config-one"
    assert (folder / "same.yaml").read_text() == "kind: Pod\n"
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["configuration_ids"] == ["config-one"]
    assert manifest["source_version"] == "abc"
    migrated = json.loads((records / "config-one.json").read_text())
    assert migrated["configuration_file"]["path"] == "/configs/config-one/same.yaml"
    assert reindex_artifacts(apply=True)["unchanged"] == 1


def _legacy_configuration(tmp_path):
    records = tmp_path / "metadata/configurations"
    records.mkdir(parents=True)
    path = records / "config-one.json"
    path.write_text(
        json.dumps(
            {
                "artifact_id": "config-one",
                "configuration_file": {"file_name": "same.yaml", "path": "/configs/same.yaml"},
                "deployable_configuration": {"content": {"officialGuide": {"renderedManifest": "kind: Pod\n"}}},
            }
        )
    )
    return path


def test_reindex_repairs_metadata_after_manifest_publish_failure(monkeypatch, tmp_path):
    from llm_d_bench.utils import storage_admin as admin

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    path = _legacy_configuration(tmp_path)
    write_record = admin._write_record
    monkeypatch.setattr(admin, "_write_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("interrupted")))
    assert admin.reindex_artifacts(apply=True)["errors"]
    manifest = tmp_path / "artifacts/configurations/config-one/manifest.json"
    before = manifest.read_bytes()
    monkeypatch.setattr(admin, "_write_record", write_record)
    assert not admin.reindex_artifacts(apply=True)["errors"]
    assert json.loads(path.read_text())["configuration_file"]["path"] == "/configs/config-one/same.yaml"
    assert manifest.read_bytes() == before


def test_reindex_rejects_symlink_configuration_metadata(monkeypatch, tmp_path):
    from llm_d_bench.utils.storage_admin import reindex_artifacts

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    path = _legacy_configuration(tmp_path)
    external = tmp_path / "external.json"
    path.replace(external)
    path.symlink_to(external)
    before = external.read_bytes()
    assert reindex_artifacts(apply=True)["errors"]
    assert external.read_bytes() == before
    assert not (tmp_path / "artifacts/configurations/config-one").exists()


def test_reindex_dataset_sidecars_preserves_native_payload(monkeypatch, tmp_path):
    from llm_d_bench.utils.storage_admin import reindex_artifacts

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    root = tmp_path / "datasets"
    root.mkdir()
    (root / "trace.csv").write_text("timestamp,input_length,output_length\n1,2,3\n")
    (root / "trace.csv.metadata.json").write_text(
        json.dumps({"dataset": "trace-one", "source_url": "https://example.test/data", "sha256": "upstream"})
    )
    (root / "trace.csv.timeline.v2.json").write_text("{}")
    assert reindex_artifacts()["pending"] == 1
    assert not (root / "trace.csv.manifest.json").exists()
    before = (root / "trace.csv").read_bytes()
    assert reindex_artifacts(apply=True)["registered"] == 1
    manifest_path = root / "trace.csv.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["owner_id"] == "trace-one"
    assert {file["path"] for file in manifest["files"]} == {
        "trace.csv",
        "trace.csv.metadata.json",
        "trace.csv.timeline.v2.json",
    }
    assert manifest["source_version"]["download_sha256"] == "upstream"
    assert (root / "trace.csv").read_bytes() == before
    assert reindex_artifacts(apply=True)["unchanged"] == 1


def test_reindex_preserves_conflicting_evaluation_snapshot(monkeypatch, tmp_path):
    from llm_d_bench.utils.storage_admin import reindex_artifacts

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    records = tmp_path / "metadata/evaluations/runs"
    records.mkdir(parents=True)
    (records / "run-one.json").write_text(json.dumps({"id": "run-one", "status": "failed"}))
    folder = tmp_path / "artifacts/evaluations/run-one"
    folder.mkdir(parents=True)
    snapshot = folder / "evaluation-record.json"
    snapshot.write_text('{"original": true}')
    assert reindex_artifacts(apply=True)["errors"]
    assert snapshot.read_text() == '{"original": true}'
    assert not (folder / "manifest.json").exists()


def test_reindex_repairs_evaluation_reference_after_interruption(monkeypatch, tmp_path):
    from llm_d_bench.utils import storage_admin as admin
    from llm_d_bench.utils.artifact_store import register_artifacts

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    records = tmp_path / "metadata/evaluations/runs"
    records.mkdir(parents=True)
    record_path = records / "run-one.json"
    record_path.write_text(json.dumps({"id": "run-one", "status": "failed"}))
    folder = tmp_path / "artifacts/evaluations/run-one"
    folder.mkdir(parents=True)
    (folder / "evaluation-record.json").write_bytes(record_path.read_bytes())
    manifest = register_artifacts(folder, owner_type="evaluation", owner_id="run-one", status="failed")
    assert not admin.reindex_artifacts(apply=True)["errors"]
    assert json.loads(record_path.read_text())["artifact_ref"] == manifest["uri"]


def test_migration_preserves_checksum_owned_configuration_content(tmp_path):
    source = tmp_path / "old"
    source.mkdir()
    target = tmp_path / "new"
    immutable = {"volume": {"path": str(source / "model")}}
    payload = {
        "output": str(source / "results"),
        "deployable_configuration": {"content": immutable, "checksum": "original"},
    }
    (source / "record.json").write_text(json.dumps(payload))
    migrate_tree(source, target, mappings=[(source, target)], apply=True)
    migrated = json.loads((target / "record.json").read_text())
    assert migrated["output"] == str(target / "results")
    assert migrated["deployable_configuration"] == payload["deployable_configuration"]
