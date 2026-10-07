"""Offline, non-destructive legacy migration and artifact retention inventory.

Run: python -m llm_d_bench.utils.storage_admin migrate [--apply]
Stop writers before --apply. Sources remain untouched; conflicting destinations
are reported, never replaced. Cache migration is optional (normally rebuild it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .paths import storage_path

_PATH_FIELDS = frozenset(
    {
        "task_dir",
        "output",
        "output_dir",
        "output_path",
        "summary_path",
        "manifest_ref",
        "resolved_repository",
        "llm_d_repo_path",
        "llm_d_benchmark_repo_path",
        "path",
        "work_dir",
        "kubeconfig_path",
        "stdout_path",
        "stderr_path",
        "rendered_overlay",
        "overlay_path",
    }
)


def legacy_mappings(repository: Path, include_cache: bool = False) -> list[tuple[Path, Path]]:
    home = Path.home()
    cache = home / ".cache/llm-d-bench"
    old = home / ".llm-d-bench"

    def data(*parts):
        return storage_path("data", *parts)

    mappings = [
        (
            cache / "configurations/.artifacts",
            storage_path("data", "metadata", "configurations"),
        ),
        (cache / "configurations", storage_path("data", "artifacts", "configurations")),
        (
            cache / "deployment-manifests",
            storage_path("data", "artifacts", "deployment-manifests"),
        ),
        (
            cache / "prism-evaluate-results",
            storage_path("data", "artifacts", "evaluations"),
        ),
        (cache / "simulations", storage_path("data", "artifacts", "simulations")),
        (cache / "datasets", storage_path("data", "datasets")),
        (cache / "data/cluster/bootstrap", data("credentials", "bootstrap")),
        (cache / "data/cluster", storage_path("data", "credentials", "clusters")),
        (cache / "ai-providers", data("credentials", "ai-providers")),
        (cache / "storage", data("metadata", "storage")),
        (old / "deploy/runs", data("metadata", "deployments", "runs")),
        (old / "deploy/executions", data("metadata", "deployments", "executions")),
        (old / "model-cache", data("metadata", "model-cache")),
        (old / "agentic", data("metadata", "agentic")),
        (
            old / "monitoring/operations",
            storage_path("data", "metadata", "monitoring", "operations"),
        ),
        (
            home / ".llm_d_bench/run_store/evaluate",
            storage_path("data", "metadata", "evaluations"),
        ),
        (
            repository / "private/run_store/evaluate",
            storage_path("data", "metadata", "evaluations"),
        ),
        (repository / ".llm_d_bench/deploy_store/runs", data("metadata", "deployments", "runs")),
        (repository / ".llm_d_bench/deploy_store/executions", data("metadata", "deployments", "executions")),
        (repository / ".dev-logs", storage_path("log", "dev")),
        (repository / ".prism-logs", storage_path("log", "service")),
    ]
    # Old log owner layout differs from the new parent/logs layout.
    logs = old / "deploy/logs"
    if logs.is_dir():
        mappings.extend(
            (p, data("artifacts", "deployments", p.name, "logs"))
            for p in sorted(logs.iterdir())
            if p.is_dir() and not p.is_symlink()
        )
    if include_cache:
        mappings.extend(
            (cache / name, storage_path("cache", name))
            for name in ("repos", "backends", "tokenizers", "kubespray", "llm-d")
        )
    return mappings


def _rebase(value, mappings, key=""):
    if isinstance(value, dict):
        immutable_configuration = key in {
            "deployable_configuration",
            "deployment_configuration",
            "source_configurations",
        } or ("checksum" in value and "provider_ref" in value)
        return {
            k: v if k == "content" and immutable_configuration else _rebase(v, mappings, k) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_rebase(v, mappings, key) for v in value]
    if isinstance(value, str) and key in _PATH_FIELDS and value.startswith("/"):
        path = Path(value)
        for source, destination in sorted(mappings, key=lambda pair: len(str(pair[0])), reverse=True):
            if path.is_relative_to(source):
                return str(destination / path.relative_to(source))
    return value


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def migrate_tree(source: Path, target: Path, *, mappings=(), apply=False, excluded=()) -> dict:
    source, target = Path(source).absolute(), Path(target).absolute()
    report = {
        "source": str(source),
        "target": str(target),
        "copied": 0,
        "pending": 0,
        "unchanged": 0,
        "skipped": 0,
        "conflicts": [],
    }
    if not source.exists() or source == target:
        return report
    if source.is_symlink() or target.is_symlink() or target.is_relative_to(source) or source.is_relative_to(target):
        report["conflicts"].append("Source and destination overlap or use symlink roots")
        return report
    for folder, dirs, names in os.walk(source, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if not (Path(folder) / d).is_symlink() and not any((Path(folder) / d).is_relative_to(e) for e in excluded)
        )
        for name in sorted(names):
            original = Path(folder) / name
            if original.is_symlink() or not original.is_file() or name.endswith((".tmp", ".lock")):
                report["skipped"] += 1
                continue
            relative = original.relative_to(source)
            destination = target / relative
            rewritten = None
            if original.suffix == ".json":
                try:
                    payload = json.loads(original.read_text())
                    rebased = _rebase(payload, mappings)
                    # Earlier evaluation versions used a flat store.
                    if (
                        relative.parent == Path(".")
                        and isinstance(payload, dict)
                        and source.name == "evaluate"
                        and (
                            payload.get("kind") in {"benchmark", "workflow", "evaluation"}
                            or payload.get("deployment_run_id")
                            or payload.get("id")
                        )
                    ):
                        kind = payload.get("kind") or ("workflow" if payload.get("deployment_run_id") else "benchmark")
                        rebased["kind"] = "workflow" if kind == "evaluation" else kind
                        destination = target / ("runs" if kind == "benchmark" else "workflows") / name
                    if rebased != payload:
                        rewritten = (json.dumps(rebased, ensure_ascii=False, indent=2) + "\n").encode()
                except (ValueError, UnicodeError):
                    pass  # Raw third-party JSON is copied verbatim.
            if any(p.is_symlink() for p in [destination, *destination.parents]):
                report["conflicts"].append(str(relative))
                continue
            if destination.exists():
                same = destination.is_file() and (
                    _digest(destination)
                    == (hashlib.sha256(rewritten).hexdigest() if rewritten is not None else _digest(original))
                )
                if same:
                    report["unchanged"] += 1
                else:
                    report["conflicts"].append(str(relative))
                continue
            if not apply:
                report["pending"] += 1
                continue
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = destination.with_name(f".{destination.name}.{uuid4()}.tmp")
            try:
                with temporary.open("xb") as output:
                    os.chmod(temporary, 0o700 if original.stat().st_mode & 0o111 else 0o600)
                    if rewritten is not None:
                        output.write(rewritten)
                    else:
                        with original.open("rb") as stream:
                            before = os.fstat(stream.fileno())
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                output.write(chunk)
                            after = os.fstat(stream.fileno())
                            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                report["conflicts"].append(str(relative) + " (source changed)")
                                continue
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary, destination)  # Publish without replacing racing writers.
                    report["copied"] += 1
                except FileExistsError:
                    report["conflicts"].append(str(relative))
            finally:
                temporary.unlink(missing_ok=True)
    return report


def retention_inventory(root: Path, *, days: int = 30) -> list[dict]:
    """Report expiration candidates only. Domain deletion APIs own actual cleanup."""
    if days < 1:
        raise ValueError("Retention days must be positive")
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = []
    for path in sorted(Path(root).rglob("manifest.json")):
        if path.is_symlink():
            continue
        try:
            manifest = json.loads(path.read_text())
            if manifest.get("schema_version") != "artifact-manifest.v1":
                continue
            timestamp = datetime.fromisoformat(manifest["updated_at"])
            terminal = manifest.get("status") in {"complete", "completed", "succeeded", "failed", "cancelled"}
            eligible = terminal and manifest.get("retention_class") in {"diagnostic", "cache"} and timestamp < cutoff
            rows.append(
                {
                    "owner_id": manifest["owner_id"],
                    "uri": manifest.get("uri"),
                    "manifest": str(path),
                    "retention_class": manifest.get("retention_class"),
                    "eligible": eligible,
                    "size_bytes": sum(item.get("size_bytes", 0) for item in manifest.get("files", [])),
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            rows.append({"manifest": str(path), "eligible": False, "error": "invalid manifest"})
    return rows


def _require_unlinked(path: Path) -> None:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Storage symlinks are not supported")


def _read_record(path: Path) -> dict:
    _require_unlinked(path)
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("Storage record must be a JSON object")
    return payload


def _write_record(path: Path, payload: dict) -> None:
    _require_unlinked(path)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            temporary.chmod(0o600)
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def reindex_artifacts(*, apply: bool = False) -> dict:
    """Backfill copied historical evidence without inventing missing provenance.

    Existing manifests are preserved. Configuration bytes come from the saved
    immutable snapshot, never from an old same-name YAML that may be overwritten.
    """
    from .artifact_store import artifact_uri, register_artifacts

    report = {"registered": 0, "pending": 0, "unchanged": 0, "errors": []}
    configs = storage_path("data", "metadata", "configurations")
    output = storage_path("data", "artifacts", "configurations")
    for path in sorted(configs.glob("*.json")):
        try:
            _require_unlinked(path)
            record = _read_record(path)
            owner = record["artifact_id"]
            artifact_uri("configuration", owner)
            folder = output / owner
            file = record["configuration_file"]
            name = file["file_name"]
            if Path(name).name != name or name in {".", "..", "manifest.json"}:
                raise ValueError("Invalid configuration filename")
            guide = record["deployable_configuration"]["content"]["officialGuide"]
            content = guide["renderedManifest"].encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            if file.get("sha256") and file["sha256"].removeprefix("sha256:") != digest:
                raise ValueError("Configuration snapshot checksum conflict")
            if guide.get("manifestChecksum") and guide["manifestChecksum"] != f"sha256:{digest}":
                raise ValueError("Configuration manifest checksum conflict")
            destination = folder / name
            manifest_path = folder / "manifest.json"
            _require_unlinked(destination)
            _require_unlinked(manifest_path)
            if destination.exists() and destination.read_bytes() != content:
                raise ValueError("Configuration payload conflict")
            manifest = _read_record(manifest_path) if manifest_path.exists() else None
            if manifest is not None:
                entry = next((item for item in manifest.get("files", []) if item.get("path") == name), {})
                if (
                    (manifest.get("owner_type"), manifest.get("owner_id")) != ("configuration", owner)
                    or entry.get("sha256") != digest
                    or not destination.is_file()
                ):
                    raise ValueError("Configuration artifact inventory conflict")
            new_file = {
                **file,
                "path": f"/configs/{owner}/{name}",
                "uri": artifact_uri("configuration", owner, name),
                "size": len(content),
                "sha256": digest,
            }
            if manifest is not None and new_file == file:
                report["unchanged"] += 1
                continue
            if not apply:
                report["pending"] += 1
                continue
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not destination.exists():
                temporary = folder / f".{name}.{uuid4()}.tmp"
                try:
                    with temporary.open("xb") as stream:
                        temporary.chmod(0o600)
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            if manifest is None:
                register_artifacts(
                    folder,
                    owner_type="configuration",
                    owner_id=owner,
                    source_version=(guide.get("source") or {}).get("commit"),
                    configuration_ids=[owner],
                    retention_class="configuration",
                    files={name: {}},
                )
            record["configuration_file"] = new_file
            _write_record(path, record)
            report["registered"] += 1
        except (OSError, ValueError, KeyError, TypeError) as error:
            report["errors"].append({"record": str(path), "error": str(error)})

    domains = [
        (
            "evaluation",
            storage_path("data", "artifacts", "evaluations"),
            "evidence",
        ),
        ("simulation", storage_path("data", "artifacts", "simulations"), "evidence"),
        ("deployment", storage_path("data", "artifacts", "deployments"), "diagnostic"),
        (
            "deployment-manifest",
            storage_path("data", "artifacts", "deployment-manifests"),
            "configuration",
        ),
    ]
    terminal = {"completed", "succeeded", "failed", "cancelled", "stopped", "deleted"}
    for domain, root, retention in domains:
        for folder in sorted(root.iterdir()) if root.is_dir() else []:
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                _require_unlinked(folder)
                manifest_path = folder / "manifest.json"
                _require_unlinked(manifest_path)
                manifest = _read_record(manifest_path) if manifest_path.exists() else None
                if manifest is not None and (manifest.get("owner_type"), manifest.get("owner_id")) != (
                    domain,
                    folder.name,
                ):
                    raise ValueError("Artifact inventory owner conflict")
                record = {}
                if domain == "simulation":
                    record = _read_record(folder / "task.json")
                elif domain == "evaluation":
                    record_path = storage_path("data", "metadata", "evaluations") / "runs" / f"{folder.name}.json"
                    if record_path.exists() or record_path.is_symlink():
                        record = _read_record(record_path)
                elif domain == "deployment":
                    record_path = storage_path("data", "metadata", "deployments", "executions") / f"{folder.name}.json"
                    if record_path.exists() or record_path.is_symlink():
                        record = _read_record(record_path)
                status = record.get("status", "unknown")
                if record and status not in terminal:
                    continue  # Active work is never reclassified or mutated.
                if manifest is not None:
                    if (
                        domain == "evaluation"
                        and record
                        and (
                            record.get("artifact_ref") != manifest["uri"] or record.get("artifact_manifest") != manifest
                        )
                    ):
                        if apply:
                            record.update(artifact_ref=manifest["uri"], artifact_manifest=manifest)
                            _write_record(record_path, record)
                            report["registered"] += 1
                        else:
                            report["pending"] += 1
                    else:
                        report["unchanged"] += 1
                    continue
                snapshot_path = folder / "evaluation-record.json"
                if domain == "evaluation" and record:
                    snapshot = {k: v for k, v in record.items() if k not in {"artifact_ref", "artifact_manifest"}}
                    _require_unlinked(snapshot_path)
                    if snapshot_path.exists() and _read_record(snapshot_path) != snapshot:
                        raise ValueError("Evaluation snapshot conflict")
                if not apply:
                    report["pending"] += 1
                    continue
                if domain == "evaluation" and record and not snapshot_path.exists():
                    _write_record(snapshot_path, snapshot)
                artifact = record.get("artifact") or {}
                ids = (
                    record.get("configuration_ids")
                    or record.get("configuration_artifact_ids")
                    or artifact.get("configuration_artifact_ids")
                    or []
                )
                if record.get("configuration_artifact_id"):
                    ids = [*ids, record["configuration_artifact_id"]]
                source = artifact.get("source_ref") if domain == "deployment" else None
                if domain == "evaluation":
                    runtime = record.get("benchmark_runtime") or {}
                    source = {
                        "benchmark": {key: runtime.get(key) or "unknown" for key in ("ref", "commit", "resolved_from")},
                        "lens": "unknown",
                    }
                    configuration_id = (record.get("configuration") or {}).get("configuration_artifact_id")
                    if configuration_id:
                        ids = [*ids, configuration_id]
                manifest = register_artifacts(
                    folder,
                    owner_type=domain,
                    owner_id=folder.name,
                    source_version=source,
                    configuration_ids=ids,
                    retention_class=retention,
                    status=status,
                    truncated=domain != "deployment-manifest",
                )
                if domain == "evaluation" and record:
                    record["artifact_ref"] = manifest["uri"]
                    record["artifact_manifest"] = manifest
                    _write_record(record_path, record)
                report["registered"] += 1
            except (OSError, ValueError, KeyError, TypeError) as error:
                report["errors"].append({"record": str(folder), "error": str(error)})
    datasets = storage_path("data", "datasets")
    for metadata_path in sorted(datasets.glob("*.metadata.json")):
        try:
            _require_unlinked(metadata_path)
            metadata = _read_record(metadata_path)
            owner = metadata["dataset"]
            artifact_uri("dataset", owner)
            payload = metadata_path.with_name(metadata_path.name.removesuffix(".metadata.json"))
            _require_unlinked(payload)
            if not payload.is_file():
                raise ValueError("Dataset payload is missing")
            manifest_path = payload.with_name(f"{payload.name}.manifest.json")
            _require_unlinked(manifest_path)
            if manifest_path.exists():
                manifest = _read_record(manifest_path)
                if (manifest.get("owner_type"), manifest.get("owner_id")) != ("dataset", owner):
                    raise ValueError("Dataset artifact owner conflict")
                report["unchanged"] += 1
                continue
            files = {payload.name: {"kind": "dataset"}, metadata_path.name: {"kind": "metadata"}}
            for timeline in datasets.iterdir():
                if timeline.name.startswith(f"{payload.name}.timeline.v") and timeline.name.endswith(".json"):
                    _require_unlinked(timeline)
                    if timeline.is_file():
                        files[timeline.name] = {"kind": "timeline-index"}
            if not apply:
                report["pending"] += 1
                continue
            register_artifacts(
                datasets,
                owner_type="dataset",
                owner_id=owner,
                manifest_name=manifest_path.name,
                files=files,
                source_version={
                    "source": metadata.get("source_url") or "unknown",
                    "repository": metadata.get("source_repository") or "unknown",
                    "revision": metadata.get("revision") or "unknown",
                    "download_sha256": metadata.get("sha256") or "unknown",
                },
            )
            report["registered"] += 1
        except (OSError, ValueError, KeyError, TypeError) as error:
            report["errors"].append({"record": str(metadata_path), "error": str(error)})
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    migration = sub.add_parser("migrate")
    migration.add_argument("--apply", action="store_true", help="Copy files; stop application writers first")
    migration.add_argument("--include-cache", action="store_true")
    migration.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    reindex = sub.add_parser("reindex")
    reindex.add_argument("--apply", action="store_true")
    retention = sub.add_parser("retention")
    retention.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    if args.command == "reindex":
        report = reindex_artifacts(apply=args.apply)
        print(json.dumps(report, indent=2))
        return 2 if report["errors"] else 0
    if args.command == "retention":
        print(json.dumps(retention_inventory(storage_path("data", "artifacts"), days=args.days), indent=2))
        return 0
    mappings = legacy_mappings(args.repository.resolve(), args.include_cache)
    reports = [
        migrate_tree(
            source,
            target,
            mappings=mappings,
            apply=args.apply,
            excluded=[other for other, _ in mappings if other != source and other.is_relative_to(source)],
        )
        for source, target in mappings
    ]
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "reports": reports}, indent=2))
    return 2 if any(row["conflicts"] for row in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
