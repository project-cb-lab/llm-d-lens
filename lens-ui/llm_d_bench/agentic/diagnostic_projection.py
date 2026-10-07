"""Build bounded, replayable OpenSearch documents from diagnostic evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .evidence_search_repository import DiagnosticEvidenceProjection, EvidenceSourceType

_MAX_EXCERPT_CHARS = 8_000
_REFERENCE_SOURCES = {"deployment_log", "profiling_trace", "artifact", "guide", "github", "paper"}


def project_diagnostic_excerpt(
    *,
    evidence_id: str,
    source_type: EvidenceSourceType,
    title: str,
    excerpt: str,
    tenant_id: str,
    acl: tuple[str, ...],
    source_revision: str | None = None,
    artifact_uri: str | None = None,
    observed_at: datetime | None = None,
    runtime_backend: str | None = None,
    accelerator: str | None = None,
    execution_id: str | None = None,
    plan_id: str | None = None,
) -> DiagnosticEvidenceProjection:
    """Create a bounded projection from an already authorized diagnostic summary."""
    if source_type not in _REFERENCE_SOURCES:
        raise ValueError("planning snapshots must use project_planning_snapshot")
    excerpt = excerpt[:_MAX_EXCERPT_CHARS]
    return DiagnosticEvidenceProjection(
        evidence_id=evidence_id,
        source_type=source_type,
        title=title,
        excerpt=excerpt,
        content_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        tenant_id=tenant_id,
        acl=acl,
        source_revision=source_revision,
        artifact_uri=artifact_uri,
        observed_at=observed_at,
        runtime_backend=runtime_backend,
        accelerator=accelerator,
        execution_id=execution_id,
        plan_id=plan_id,
    )


def project_planning_snapshot(
    path: Path,
    *,
    tenant_id: str,
    acl: tuple[str, ...],
) -> DiagnosticEvidenceProjection:
    """Produce a metadata-only diagnostic projection from a batch-5 JSON snapshot."""
    raw = Path(path).read_bytes()
    try:
        snapshot = json.loads(raw)
        snapshot_id = _required_string(snapshot, "snapshot_id")
        run_id = _required_string(snapshot, "run_id")
        created_at = datetime.fromisoformat(_required_string(snapshot, "created_at"))
        event = _required_string(snapshot, "event")
        selected = snapshot["selected_candidate"]
        decision = snapshot.get("decision") or {}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("invalid planning snapshot") from error
    summary = {
        "event": event,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "selected_candidate": {
            "id": selected.get("id"),
            "provider_ref": selected.get("provider_ref"),
            "replicas": selected.get("replicas"),
            "tensor_parallel_size": selected.get("tensor_parallel_size"),
        },
        "decision": {
            "action": decision.get("action"),
            "candidate_id": decision.get("candidate_id"),
        },
        "candidate_count": len(snapshot.get("candidate_catalog") or ()),
        "planner": (snapshot.get("planner") or {}).get("name"),
        "generator": (snapshot.get("generator") or {}).get("name"),
    }
    return DiagnosticEvidenceProjection(
        evidence_id=f"planning-snapshot:{snapshot_id}",
        source_type="planning_snapshot",
        title=f"Agentic planning {event}: {run_id}",
        excerpt=json.dumps(summary, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        content_hash=hashlib.sha256(raw).hexdigest(),
        tenant_id=tenant_id,
        acl=acl,
        source_revision=f"agentic-planning-snapshot.v{snapshot.get('schema_version', 'unknown')}",
        artifact_uri=f"agentic-planning://{run_id}/{Path(path).name}",
        observed_at=created_at,
        plan_id=run_id,
    )


def projection_index_document(projection: DiagnosticEvidenceProjection) -> dict[str, object]:
    """Return the stable OpenSearch document body for an idempotent projection."""
    document = projection.model_dump(mode="json")
    if projection.runtime_backend is not None:
        document["runtime"] = {"backend": projection.runtime_backend}
    if projection.accelerator is not None:
        document["hardware"] = {"accelerator": projection.accelerator}
    document.pop("runtime_backend")
    document.pop("accelerator")
    return document


def _required_string(value: dict[str, object], key: str) -> str:
    result = value[key]
    if not isinstance(result, str) or not result:
        raise ValueError(f"planning snapshot {key} is missing")
    return result
