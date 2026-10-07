"""Append-only JSON audit snapshots for Agentic planning decisions."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import AgenticDeploymentRun
from .planner import PlanningFacts


@dataclass(frozen=True)
class PlanningSnapshotReference:
    snapshot_id: str
    relative_path: str


class PlanningSnapshotStore:
    """Persist immutable, redacted planning audit snapshots outside the database."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        root = base_dir or os.getenv("AGENTIC_PLANNING_SNAPSHOT_DIR")
        self._base_dir = Path(root) if root else Path(__file__).parents[2] / "session-artifacts" / "agentic-planning"

    def save(self, run: AgenticDeploymentRun, facts: PlanningFacts, *, event: str) -> PlanningSnapshotReference:
        snapshot_id = str(uuid4())
        created_at = datetime.now(UTC)
        directory = self._base_dir / run.id
        filename = f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{snapshot_id}.json"
        destination = directory / filename
        payload = self._payload(snapshot_id, created_at, run, facts, event)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(payload, stream, ensure_ascii=True, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return PlanningSnapshotReference(
            snapshot_id=snapshot_id, relative_path=str(destination.relative_to(self._base_dir))
        )

    @staticmethod
    def _payload(
        snapshot_id: str,
        created_at: datetime,
        run: AgenticDeploymentRun,
        facts: PlanningFacts,
        event: str,
    ) -> dict[str, object]:
        request = run.request.model_dump(mode="json", exclude={"planner_prompt"})
        request["planner_prompt_present"] = bool(run.request.planner_prompt)
        return {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "created_at": created_at.isoformat(),
            "event": event,
            "run_id": run.id,
            "request": request,
            "resolved_planning_facts": asdict(facts),
            "cluster_snapshot": run.planning_cluster_snapshot,
            "planning_evidence": [evidence.model_dump(mode="json") for evidence in run.planning_evidence],
            "candidate_catalog": [candidate.model_dump(mode="json") for candidate in run.candidates],
            "selected_candidate": run.selected_candidate.model_dump(mode="json"),
            "decision": None if run.decision is None else run.decision.model_dump(mode="json"),
            "decision_evidence_ids": run.decision_evidence_ids,
            "decision_metadata": None
            if run.decision_metadata is None
            else run.decision_metadata.model_dump(mode="json"),
            "generator": {
                "name": run.generator,
                "model": run.generator_model,
                "fallback_reason": run.generator_fallback_reason,
                "fallback_detail": run.generator_fallback_detail,
                "candidate_proposals": run.generator_candidate_proposals,
                "rejected_candidates": [candidate.model_dump(mode="json") for candidate in run.rejected_candidates],
                "tool_trace": run.generator_tool_trace,
            },
            "planner": {"name": run.planner, "fallback_reason": run.planner_fallback_reason},
        }
