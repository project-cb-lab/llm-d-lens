"""Smoke tests for ``llm_d_bench.db.legacy_import`` -- writes legacy-shaped JSON
files into a temp directory (matching each module's original on-disk layout),
runs the corresponding importer, and asserts the records land in the new
tables and the source directory is renamed to ``*.migrated`` only when every
file imported cleanly (a failed file keeps the directory for a retry).
"""

from __future__ import annotations

import json

from llm_d_bench.ai_providers.contracts import AIProvider
from llm_d_bench.db import legacy_import
from llm_d_bench.db.dao.ai_provider import AIProviderDao
from llm_d_bench.db.dao.storage_volume import StorageVolumeDao
from llm_d_bench.storage.contracts import LocalDiskSpec, StorageVolume, StorageVolumeKind, StorageVolumeStatus


def _volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "qwen-cache",
        "kind": StorageVolumeKind.LOCAL_DISK,
        "status": StorageVolumeStatus.READY,
        "capacity": "100Gi",
        "readOnly": True,
        "localDisk": LocalDiskSpec(hostPath="/data/models"),
        "pvcName": "prism-storage-abc",
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def test_import_storage_volumes_migrates_legacy_files(tmp_path, monkeypatch):
    volumes_dir = tmp_path / "storage" / "volumes"
    volumes_dir.mkdir(parents=True)
    volume = _volume()
    (volumes_dir / f"{volume.id}.json").write_text(volume.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("STORAGE_VOLUME_DIR", str(tmp_path / "storage"))

    stats = legacy_import.import_storage_volumes()

    assert stats.imported == 1
    assert stats.failed == 0
    assert StorageVolumeDao().get(volume.id) == volume
    assert not volumes_dir.exists()
    assert (tmp_path / "storage" / "volumes.migrated").is_dir()


def test_import_storage_volumes_is_idempotent(tmp_path, monkeypatch):
    volumes_dir = tmp_path / "storage" / "volumes"
    volumes_dir.mkdir(parents=True)
    volume = _volume()
    (volumes_dir / f"{volume.id}.json").write_text(volume.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("STORAGE_VOLUME_DIR", str(tmp_path / "storage"))

    first = legacy_import.import_storage_volumes()
    assert first.imported == 1

    # Re-run against the now-migrated directory: nothing left to scan, so a
    # second invocation (e.g. a retried rollout step) is a safe no-op.
    second = legacy_import.import_storage_volumes()
    assert second.imported == 0
    assert second.skipped == 0
    assert second.failed == 0


def test_import_storage_volumes_skips_already_migrated_ids(tmp_path, monkeypatch):
    volumes_dir = tmp_path / "storage" / "volumes"
    volumes_dir.mkdir(parents=True)
    volume = _volume()
    (volumes_dir / f"{volume.id}.json").write_text(volume.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("STORAGE_VOLUME_DIR", str(tmp_path / "storage"))
    StorageVolumeDao().create(volume)

    stats = legacy_import.import_storage_volumes()

    assert stats.imported == 0
    assert stats.skipped == 1
    assert stats.failed == 0


def test_import_storage_volumes_dry_run_does_not_write_or_rename(tmp_path, monkeypatch):
    volumes_dir = tmp_path / "storage" / "volumes"
    volumes_dir.mkdir(parents=True)
    volume = _volume()
    (volumes_dir / f"{volume.id}.json").write_text(volume.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("STORAGE_VOLUME_DIR", str(tmp_path / "storage"))

    stats = legacy_import.import_storage_volumes(dry_run=True)

    assert stats.imported == 1
    assert StorageVolumeDao().get(volume.id) is None
    assert volumes_dir.is_dir()


def test_import_storage_volumes_reports_and_continues_past_malformed_files(tmp_path, monkeypatch):
    volumes_dir = tmp_path / "storage" / "volumes"
    volumes_dir.mkdir(parents=True)
    volume = _volume()
    (volumes_dir / f"{volume.id}.json").write_text(volume.model_dump_json(), encoding="utf-8")
    (volumes_dir / "corrupt.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("STORAGE_VOLUME_DIR", str(tmp_path / "storage"))

    stats = legacy_import.import_storage_volumes()

    assert stats.imported == 1
    assert stats.failed == 1
    assert "corrupt" in stats.errors[0]
    # A partial failure leaves the directory in place so the import can be
    # retried after the bad file is fixed; only a fully clean run is archived
    # (renaming here would hide the failure behind a path the next run skips).
    assert volumes_dir.is_dir()
    assert not (tmp_path / "storage" / "volumes.migrated").exists()


def test_import_ai_providers_migrates_legacy_files(tmp_path, monkeypatch):
    providers_dir = tmp_path / "ai-providers"
    providers_dir.mkdir(parents=True)
    provider = AIProvider(name="Local vLLM", baseUrl="http://localhost:8000/v1", model="Qwen/Qwen3-8B")
    (providers_dir / f"{provider.id}.json").write_text(provider.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("AI_PROVIDER_DIR", str(providers_dir))

    stats = legacy_import.import_ai_providers()

    assert stats.imported == 1
    assert AIProviderDao().get(provider.id) == provider
    assert (tmp_path / "ai-providers.migrated").is_dir()


def test_main_all_module_runs_every_importer_and_reports_zero_when_no_files(tmp_path, monkeypatch, capsys):
    # No legacy directories exist under these env overrides, so every
    # importer should report all-zero stats without raising.
    monkeypatch.setenv("STORAGE_VOLUME_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("AI_PROVIDER_DIR", str(tmp_path / "ai-providers"))
    monkeypatch.setenv("CONFIGURATION_OUTPUT_DIR", str(tmp_path / "configurations"))
    monkeypatch.setenv("DEPLOY_RUN_STORE_DIR", str(tmp_path / "deploy"))
    monkeypatch.setenv("MODEL_CACHE_DIR", str(tmp_path / "model-cache"))
    monkeypatch.setenv("AGENTIC_BENCHMARK_DIR", str(tmp_path / "agentic"))
    monkeypatch.setenv("MONITORING_OPERATION_ROOT", str(tmp_path / "monitoring"))
    monkeypatch.setenv("LLM_D_BENCH_EVALUATE_STORE", str(tmp_path / "evaluate"))
    monkeypatch.setenv("SIMULATION_TASK_ROOT", str(tmp_path / "simulations"))

    exit_code = legacy_import.main(["--module", "all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    for module in (
        "storage_volumes",
        "ai_providers",
        "configuration_artifacts",
        "deploy",
        "model_cache_entries",
        "agentic_benchmark_records",
        "monitoring_operations",
        "evaluate",
        "simulation_tasks",
    ):
        assert f"{module}: imported=0 skipped=0 failed=0" in output


def test_import_monitoring_operations_routes_by_accelerator_field(tmp_path, monkeypatch):
    from llm_d_bench.db.dao.monitoring_accelerator_operation import (
        MonitoringAcceleratorOperationDao,
    )
    from llm_d_bench.db.dao.monitoring_cluster_stack_operation import (
        MonitoringClusterStackOperationDao,
    )

    operations_dir = tmp_path / "operations"
    operations_dir.mkdir(parents=True)
    accelerator_payload = {
        "operation_id": "a" * 32,
        "status": "succeeded",
        "phase": "completed",
        "accelerator": "intel_gpu",
        "namespace": "intel-xpumd",
    }
    cluster_stack_payload = {
        "operation_id": "b" * 32,
        "status": "succeeded",
        "phase": "completed",
        "namespace": "llm-d-monitoring",
    }
    (operations_dir / "a.json").write_text(json.dumps(accelerator_payload), encoding="utf-8")
    (operations_dir / "b.json").write_text(json.dumps(cluster_stack_payload), encoding="utf-8")
    monkeypatch.setenv("MONITORING_OPERATION_ROOT", str(operations_dir))

    stats = legacy_import.import_monitoring_operations()

    assert stats.imported == 2
    assert stats.failed == 0
    assert MonitoringAcceleratorOperationDao().load("a" * 32) is not None
    assert MonitoringClusterStackOperationDao().load("b" * 32) is not None
