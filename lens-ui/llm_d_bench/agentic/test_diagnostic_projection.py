"""Tests for replayable diagnostic evidence projections."""

import hashlib
import json

from llm_d_bench.agentic.diagnostic_projection import (
    project_diagnostic_excerpt,
    project_planning_snapshot,
    projection_index_document,
)


def test_projects_logs_traces_and_reference_sources_as_bounded_excerpts():
    for source_type in ("deployment_log", "profiling_trace", "github", "guide", "paper"):
        projection = project_diagnostic_excerpt(
            evidence_id=f"{source_type}:1",
            source_type=source_type,
            title="Reference",
            excerpt="out of memory " * 1_000,
            tenant_id="tenant-a",
            acl=("operator",),
        )

        assert len(projection.excerpt) == 8_000
        assert projection.content_hash == hashlib.sha256(projection.excerpt.encode("utf-8")).hexdigest()


def test_projects_planning_snapshot_without_prompt_or_tool_trace(tmp_path):
    snapshot = {
        "schema_version": 1,
        "snapshot_id": "snapshot-1",
        "created_at": "2026-09-21T00:00:00+00:00",
        "event": "created",
        "run_id": "run-1",
        "selected_candidate": {
            "id": "candidate-1",
            "provider_ref": "baseline-vllm",
            "replicas": 1,
            "tensor_parallel_size": 1,
        },
        "decision": {"action": "select_candidate", "candidate_id": "candidate-1", "rationale": "secret reasoning"},
        "candidate_catalog": [{"id": "candidate-1"}],
        "planner": {"name": "deterministic"},
        "generator": {"name": "ai-mcp", "tool_trace": [{"arguments": "sensitive"}]},
        "request": {"planner_prompt": "must not be indexed"},
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    projection = project_planning_snapshot(path, tenant_id="tenant-a", acl=("operator",))
    document = projection_index_document(projection)

    assert projection.evidence_id == "planning-snapshot:snapshot-1"
    assert projection.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert projection.plan_id == "run-1"
    assert "secret reasoning" not in projection.excerpt
    assert "sensitive" not in projection.excerpt
    assert "must not be indexed" not in projection.excerpt
    assert document["source_type"] == "planning_snapshot"
