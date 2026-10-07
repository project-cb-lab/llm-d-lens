"""One-time CLI to import pre-existing on-disk JSON records into the new DB tables.

.. deprecated::
    The SQLAlchemy/PostgreSQL migration has shipped: every record store now
    reads and writes the database directly, so this importer is no longer part
    of a supported upgrade path and is **not maintained**. Its legacy default
    paths and on-disk formats are frozen as of the migration and are
    deliberately left unchanged (see the design doc's stale "Original Storage Locations" table);
    do not assume they match the storage-scheme layout. It is retained only as
    a best-effort historical tool for a database that still needs pre-DB JSON
    imported; new installs never need to run it.

See design doc ``docs/design/sqlalchemy-data-access-layer-design.md`` section 8
("Rollout plan", step 3) and section 12 (Appendix A, legacy path list).

Usage::

    python -m llm_d_bench.db.legacy_import --module storage
    python -m llm_d_bench.db.legacy_import --module all
    python -m llm_d_bench.db.legacy_import --module all --dry-run

Each module is imported independently: a failure importing one record is
logged and skipped (not fatal to the whole run), so a module can be re-run
safely -- ``save()``/``create()`` calls that hit an already-imported id are
treated as "already migrated" and skipped, making the whole script safely
re-runnable (idempotent). Only after a module's source directory is processed
with **no failures** is it renamed to ``<name>.migrated`` (kept, per the design
doc's rollback window, rather than deleted). A directory with any failed file
is left in place so the import can be retried after the problem is fixed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ImportStats:
    module: str
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def record_error(self, identifier: str, error: Exception) -> None:
        self.failed += 1
        self.errors.append(f"{identifier}: {error}")
        logger.warning("Failed to import %s record %s: %s", self.module, identifier, error)

    def __str__(self) -> str:
        return f"{self.module}: imported={self.imported} skipped={self.skipped} failed={self.failed}"


def _iter_json_files(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def _finish_directory(directory: Path, stats: ImportStats, *, dry_run: bool) -> None:
    """Archive ``directory`` to ``<name>.migrated`` only when nothing failed.

    A directory with failed files is left untouched so a retry can still find
    them; renaming it would hide the failures behind a path the next run no
    longer scans.
    """
    if dry_run or not directory.is_dir() or stats.failed:
        return
    migrated = directory.with_name(f"{directory.name}.migrated")
    if migrated.exists():
        logger.info("%s already has a .migrated sibling; leaving %s in place", stats.module, directory)
        return
    directory.rename(migrated)
    logger.info("Renamed %s -> %s", directory, migrated)


def import_storage_volumes(*, dry_run: bool = False) -> ImportStats:
    from llm_d_bench.db.dao.storage_volume import StorageVolumeDao
    from llm_d_bench.storage.contracts import StorageVolume

    stats = ImportStats("storage_volumes")
    directory = (
        Path(os.getenv("STORAGE_VOLUME_DIR", str(Path.home() / ".cache" / "llm-d-bench" / "storage"))) / "volumes"
    )
    dao = StorageVolumeDao()
    for path in _iter_json_files(directory):
        try:
            volume = StorageVolume.model_validate_json(path.read_text(encoding="utf-8"))
            if dao.get(volume.id) is not None:
                stats.skipped += 1
                continue
            if not dry_run:
                dao.create(volume)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001 - best-effort, continue with remaining files
            stats.record_error(path.stem, error)
    _finish_directory(directory, stats, dry_run=dry_run)
    return stats


def import_ai_providers(*, dry_run: bool = False) -> ImportStats:
    from llm_d_bench.ai_providers.contracts import AIProvider
    from llm_d_bench.db.dao.ai_provider import AIProviderDao

    stats = ImportStats("ai_providers")
    directory = Path(os.getenv("AI_PROVIDER_DIR", str(Path.home() / ".cache" / "llm-d-bench" / "ai-providers")))
    dao = AIProviderDao()
    for path in _iter_json_files(directory):
        try:
            provider = AIProvider.model_validate_json(path.read_text(encoding="utf-8"))
            if dao.get(provider.id) is not None:
                stats.skipped += 1
                continue
            if not dry_run:
                dao.create(provider)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(path.stem, error)
    _finish_directory(directory, stats, dry_run=dry_run)
    return stats


def import_configuration_artifacts(*, dry_run: bool = False) -> ImportStats:
    from llm_d_bench.configuration.models import ConfigurationArtifactRecord
    from llm_d_bench.db.dao.configuration_artifact import ConfigurationArtifactDao

    stats = ImportStats("configuration_artifacts")
    output_dir = Path(
        os.getenv("CONFIGURATION_OUTPUT_DIR", str(Path.home() / ".cache" / "llm-d-bench" / "configurations"))
    )
    directory = Path(os.getenv("CONFIGURATION_ARTIFACT_DIR", str(output_dir / ".artifacts")))
    dao = ConfigurationArtifactDao()
    for path in _iter_json_files(directory):
        try:
            artifact = ConfigurationArtifactRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if dao.get(artifact.artifact_id) is not None:
                stats.skipped += 1
                continue
            if not dry_run:
                dao.create(artifact)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(path.stem, error)
    _finish_directory(directory, stats, dry_run=dry_run)
    return stats


def import_deploy(*, dry_run: bool = False) -> ImportStats:
    from llm_d_bench.db.dao.deployment_batch import DeploymentBatchDao, DeploymentEvidenceDao
    from llm_d_bench.deploy.contracts import DeploymentExecution, DeploymentRun

    stats = ImportStats("deploy")
    base = Path(os.getenv("DEPLOY_RUN_STORE_DIR", str(Path.home() / ".llm-d-bench" / "deploy")))
    runs_dir = base / "runs"
    executions_dir = base / "executions"

    batch_dao = DeploymentBatchDao()
    for path in _iter_json_files(runs_dir):
        try:
            run = DeploymentRun.model_validate_json(path.read_text(encoding="utf-8"))
            if batch_dao.get(run.id) is not None:
                stats.skipped += 1
                continue
            if not dry_run:
                batch_dao.create(run)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(path.stem, error)
    _finish_directory(runs_dir, stats, dry_run=dry_run)

    evidence_dao = DeploymentEvidenceDao()
    for path in _iter_json_files(executions_dir):
        try:
            execution = DeploymentExecution.model_validate_json(path.read_text(encoding="utf-8"))
            if evidence_dao.get(execution.execution_id) is not None:
                stats.skipped += 1
                continue
            if not dry_run:
                evidence_dao.save(execution)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(path.stem, error)
    _finish_directory(executions_dir, stats, dry_run=dry_run)
    return stats


def import_model_cache(*, dry_run: bool = False) -> ImportStats:
    from llm_d_bench.db.dao.model_cache_entry import ModelCacheEntryDao
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    stats = ImportStats("model_cache_entries")
    directory = Path(os.getenv("MODEL_CACHE_DIR", str(Path.home() / ".llm-d-bench" / "model-cache"))) / "entries"
    dao = ModelCacheEntryDao()
    for path in _iter_json_files(directory):
        try:
            entry = ModelCacheEntry.model_validate_json(path.read_text(encoding="utf-8"))
            if dao.get(entry.id) is not None:
                stats.skipped += 1
                continue
            if not dry_run:
                dao.create(entry)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(path.stem, error)
    _finish_directory(directory, stats, dry_run=dry_run)
    return stats


def import_agentic_benchmark_records(*, dry_run: bool = False) -> ImportStats:
    from llm_d_bench.agentic.benchmark_evidence import BenchmarkRecord
    from llm_d_bench.db.dao.agentic_benchmark_record import BenchmarkRecordDao

    stats = ImportStats("agentic_benchmark_records")
    directory = Path(os.getenv("AGENTIC_BENCHMARK_DIR", str(Path.home() / ".llm-d-bench" / "agentic" / "benchmarks")))
    dao = BenchmarkRecordDao()
    existing = {record.benchmark_id for record in dao.list()}
    for path in _iter_json_files(directory):
        try:
            record = BenchmarkRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if record.benchmark_id in existing:
                stats.skipped += 1
                continue
            if not dry_run:
                dao.save(record)
            existing.add(record.benchmark_id)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(path.stem, error)
    _finish_directory(directory, stats, dry_run=dry_run)
    return stats


def import_monitoring_operations(*, dry_run: bool = False) -> ImportStats:
    from llm_d_bench.db.dao.monitoring_accelerator_operation import (
        MonitoringAcceleratorOperationDao,
    )
    from llm_d_bench.db.dao.monitoring_cluster_stack_operation import (
        MonitoringClusterStackOperationDao,
    )
    from llm_d_bench.monitoring.accelerator.models import AcceleratorOperationResponse
    from llm_d_bench.monitoring.cluster_stack.models import ClusterStackOperationResponse

    stats = ImportStats("monitoring_operations")
    directory = Path(
        os.getenv("MONITORING_OPERATION_ROOT", str(Path.home() / ".llm-d-bench" / "monitoring" / "operations"))
    )
    accelerator_dao = MonitoringAcceleratorOperationDao()
    cluster_stack_dao = MonitoringClusterStackOperationDao()
    for path in _iter_json_files(directory):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            # ``AcceleratorOperationResponse`` is the only one of the two DTOs
            # carrying an "accelerator" field -- used here purely to route
            # each legacy file to the right table, no field is renamed/lost.
            if "accelerator" in payload:
                accelerator_operation = AcceleratorOperationResponse.model_validate(payload)
                if accelerator_dao.load(accelerator_operation.operation_id) is not None:
                    stats.skipped += 1
                    continue
                if not dry_run:
                    accelerator_dao.save(accelerator_operation)
            else:
                cluster_stack_operation = ClusterStackOperationResponse.model_validate(payload)
                if cluster_stack_dao.load(cluster_stack_operation.operation_id) is not None:
                    stats.skipped += 1
                    continue
                if not dry_run:
                    cluster_stack_dao.save(cluster_stack_operation)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(path.stem, error)
    _finish_directory(directory, stats, dry_run=dry_run)
    return stats


def import_evaluate(*, dry_run: bool = False) -> ImportStats:
    from llm_d_bench.db.dao.evaluate_run import EvaluateRunDao
    from llm_d_bench.db.dao.evaluate_workflow import EvaluateWorkflowDao
    from llm_d_bench.db.evaluate_persistence_models import EvaluateRunRecord, EvaluateWorkflowRecord

    stats = ImportStats("evaluate")
    base = Path(os.getenv("LLM_D_BENCH_EVALUATE_STORE", str(Path.home() / ".llm_d_bench" / "run_store" / "evaluate")))
    runs_dir = base / "runs"
    workflows_dir = base / "workflows"

    run_dao = EvaluateRunDao()
    for path in _iter_json_files(runs_dir):
        try:
            payload = dict(json.loads(path.read_text(encoding="utf-8")), kind="benchmark")
            record = EvaluateRunRecord.model_validate(payload)
            if run_dao.get(record.id) is not None:
                stats.skipped += 1
                continue
            if not dry_run:
                run_dao.create(record)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(path.stem, error)
    _finish_directory(runs_dir, stats, dry_run=dry_run)

    workflow_dao = EvaluateWorkflowDao()
    for path in _iter_json_files(workflows_dir):
        try:
            payload = dict(json.loads(path.read_text(encoding="utf-8")), kind="workflow")
            record = EvaluateWorkflowRecord.model_validate(payload)
            if workflow_dao.get(record.id) is not None:
                stats.skipped += 1
                continue
            if not dry_run:
                workflow_dao.create(record)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(path.stem, error)
    _finish_directory(workflows_dir, stats, dry_run=dry_run)
    return stats


def import_simulation_tasks(*, dry_run: bool = False) -> ImportStats:
    from llm_d_bench.db.dao.simulation_task import SimulationTaskDao
    from llm_d_bench.simulation.models import SimulationTask

    stats = ImportStats("simulation_tasks")
    directory = Path(os.getenv("SIMULATION_TASK_ROOT", str(Path.home() / ".cache" / "llm-d-bench" / "simulations")))
    dao = SimulationTaskDao()
    if not directory.is_dir():
        return stats
    for task_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        path = task_dir / "task.json"
        if not path.is_file():
            continue
        try:
            task = SimulationTask.model_validate_json(path.read_text(encoding="utf-8"))
            if dao.get(task.id) is not None:
                stats.skipped += 1
                continue
            if not dry_run:
                dao.save(task, upsert=False)
            stats.imported += 1
        except Exception as error:  # noqa: BLE001
            stats.record_error(task_dir.name, error)
    # Simulation task directories also hold per-request/artifact files that
    # must stay on disk (see design doc §9) -- only rename the whole root
    # once every subdirectory's task.json has been imported, never delete it.
    _finish_directory(directory, stats, dry_run=dry_run)
    return stats


IMPORTERS: dict[str, Callable[..., ImportStats]] = {
    "storage": import_storage_volumes,
    "ai_providers": import_ai_providers,
    "configuration": import_configuration_artifacts,
    "deploy": import_deploy,
    "model_cache": import_model_cache,
    "agentic": import_agentic_benchmark_records,
    "monitoring": import_monitoring_operations,
    "evaluate": import_evaluate,
    "simulation": import_simulation_tasks,
}


def run_import(module: str, *, dry_run: bool = False) -> list[ImportStats]:
    if module == "all":
        return [importer(dry_run=dry_run) for importer in IMPORTERS.values()]
    return [IMPORTERS[module](dry_run=dry_run)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", choices=[*IMPORTERS.keys(), "all"], required=True, help="Which module to import.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported without writing to the database or renaming directories.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.warning(
        "legacy_import is DEPRECATED: the database migration has shipped and all record stores "
        "now use the database directly. This one-time JSON importer is no longer maintained "
        "(its legacy paths/formats are frozen). Run it only if this database still needs "
        "pre-DB JSON imported."
    )
    all_stats = run_import(args.module, dry_run=args.dry_run)

    exit_code = 0
    for stats in all_stats:
        print(stats)
        for error in stats.errors:
            print(f"  ERROR: {error}")
        if stats.failed:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
