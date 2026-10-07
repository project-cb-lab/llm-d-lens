"""Contract tests for the OpenSearch non-structured evidence adapter."""

import asyncio
import hashlib
import json

import pytest

from llm_d_bench.agentic.evidence_search_repository import DiagnosticEvidenceProjection, EvidenceSearchQuery
from llm_d_bench.agentic.opensearch_evidence_search import (
    EvidenceSearchError,
    OpenSearchEvidenceProjectionWriter,
    OpenSearchEvidenceSearchRepository,
    verify_artifact_evidence,
)
from llm_d_bench.utils.artifact_store import register_artifacts


def test_opensearch_query_requires_tenant_and_acl_filters():
    query = EvidenceSearchQuery(text="out of memory", tenant_id="tenant-a", allowed_acl=("operator",))

    body = OpenSearchEvidenceSearchRepository._query(query)

    assert body["bool"]["filter"][:2] == [
        {"term": {"tenant_id": "tenant-a"}},
        {"terms": {"acl": ["operator"]}},
    ]
    assert body["bool"]["must"] == [{"multi_match": {"query": "out of memory", "fields": ["excerpt^2", "title"]}}]


def test_opensearch_query_uses_explicit_vector_without_embedding_model():
    query = EvidenceSearchQuery(
        text="out of memory",
        tenant_id="tenant-a",
        allowed_acl=("operator",),
        query_vector=(0.1, 0.2),
    )

    body = OpenSearchEvidenceSearchRepository._query(query)

    assert body["bool"]["must"][0]["bool"]["should"][1] == {"knn": {"embedding": {"vector": [0.1, 0.2], "k": 10}}}


def test_verify_artifact_evidence_rejects_replaced_content(tmp_path):
    root = tmp_path / "evaluation"
    root.mkdir()
    path = root / "diagnostic.log"
    path.write_text("OOM", encoding="utf-8")
    manifest = register_artifacts(root, owner_type="evaluation", owner_id="run-1", retention_class="diagnostic")
    entry = manifest["files"][0]
    from llm_d_bench.agentic.evidence_search_repository import DiagnosticEvidenceChunk

    chunk = DiagnosticEvidenceChunk(
        evidence_id="evidence-1",
        source_type="deployment_log",
        excerpt="OOM",
        content_hash="summary-hash",
        artifact_uri=entry["uri"],
        file_sha256=entry["sha256"],
        manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
        tenant_id="tenant-a",
        acl=("operator",),
        score=1,
        ranking_explanation="test",
    )
    assert verify_artifact_evidence(root, chunk, approved_roots=(tmp_path,)) == path

    path.write_text("replaced", encoding="utf-8")
    with pytest.raises(EvidenceSearchError, match="checksum"):
        verify_artifact_evidence(root, chunk, approved_roots=(tmp_path,))


def test_projection_writer_uses_evidence_id_for_idempotent_bulk_documents(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"errors": False}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr("llm_d_bench.agentic.opensearch_evidence_search.httpx.AsyncClient", lambda **_kwargs: Client())
    projection = DiagnosticEvidenceProjection(
        evidence_id="planning-snapshot:one",
        source_type="planning_snapshot",
        title="Plan",
        excerpt="summary",
        content_hash="hash",
        tenant_id="tenant-a",
        acl=("operator",),
    )

    written = asyncio.run(OpenSearchEvidenceProjectionWriter("http://search").write([projection]))

    lines = captured["content"].splitlines()
    assert written == 1
    assert captured["url"] == "http://search/_bulk"
    assert json.loads(lines[0]) == {"index": {"_index": "evidence-diagnostics-v1", "_id": "planning-snapshot:one"}}
    assert json.loads(lines[1])["source_type"] == "planning_snapshot"


def test_default_search_index_includes_diagnostic_projections():
    repository = OpenSearchEvidenceSearchRepository("http://search")

    assert "evidence-diagnostics-v1" in repository._index
