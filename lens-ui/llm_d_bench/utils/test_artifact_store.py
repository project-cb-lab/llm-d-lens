import hashlib
import json

import pytest

from llm_d_bench.utils.artifact_store import register_artifacts, resolve_artifact


def test_manifest_is_portable_and_records_evidence(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "stdout.log").write_text("output")
    manifest = register_artifacts(
        root,
        owner_type="evaluation-run",
        owner_id="run-1",
        source_version={"revision": "abc"},
        configuration_ids=["cfg-1"],
        files={"stdout.log": {"truncated": True, "kind": "stdout"}},
    )
    entry = manifest["files"][0]
    assert entry["sha256"] == hashlib.sha256(b"output").hexdigest()
    assert entry["uri"] == "lens-artifact://evaluation-run/run-1/stdout.log"
    assert entry["truncated"] is True
    assert manifest["truncated"] is True
    assert manifest["configuration_ids"] == ["cfg-1"]
    assert str(tmp_path) not in json.dumps(manifest)
    assert resolve_artifact(root, manifest, entry["uri"]) == root / "stdout.log"
    assert json.loads((root / "manifest.json").read_text()) == manifest


def test_reindex_preserves_creation_and_excludes_manifest(tmp_path):
    (tmp_path / "data.json").write_text("{}")
    first = register_artifacts(tmp_path, owner_type="simulation", owner_id="one")
    second = register_artifacts(tmp_path, owner_type="simulation", owner_id="one")
    assert second["created_at"] == first["created_at"]
    assert len(second["files"]) == 1
    assert second["source_version"] == {"status": "unknown"}


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd"])
def test_reject_escape(tmp_path, path):
    with pytest.raises(ValueError):
        register_artifacts(tmp_path, owner_type="run", owner_id="one", files={path: {}})


def test_reject_symlink_and_owner_conflict(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("private")
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        register_artifacts(tmp_path, owner_type="run", owner_id="one", files={"link": {}})
    (tmp_path / "link").unlink()
    register_artifacts(tmp_path, owner_type="run", owner_id="one")
    with pytest.raises(ValueError):
        register_artifacts(tmp_path, owner_type="run", owner_id="two")


def test_reject_symlink_owner_root_before_writing(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "payload").write_text("data")
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        register_artifacts(linked, owner_type="run", owner_id="one")
    assert not (actual / "manifest.json").exists()


def test_existing_unowned_manifest_is_not_overwritten(tmp_path):
    target = tmp_path / "manifest.json"
    target.write_text("{}")
    with pytest.raises(ValueError, match="owner|manifest"):
        register_artifacts(tmp_path, owner_type="run", owner_id="one")
    assert target.read_text() == "{}"


def test_manifest_uri_resolves_without_self_hashing(tmp_path):
    (tmp_path / "data.json").write_text("{}")
    manifest = register_artifacts(tmp_path, owner_type="dataset", owner_id="one", manifest_name="data.manifest.json")
    assert resolve_artifact(tmp_path, manifest, manifest["uri"]) == tmp_path / "data.manifest.json"
