"""OpenSearch adapter for non-structured Agentic evidence projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from llm_d_bench.utils.artifact_store import resolve_artifact
from llm_d_bench.utils.paths import storage_path

from .diagnostic_projection import projection_index_document
from .evidence_search_repository import (
    DiagnosticEvidenceChunk,
    DiagnosticEvidenceProjection,
    EvidenceSearchQuery,
    EvidenceSearchRepository,
)


class EvidenceSearchError(RuntimeError):
    """The rebuildable OpenSearch projection cannot serve a trustworthy result."""


class OpenSearchEvidenceProjectionWriter:
    """Write rebuildable diagnostic projections with idempotent document IDs."""

    def __init__(
        self,
        endpoint: str,
        *,
        index: str = "evidence-diagnostics-v1",
        api_key: str | None = None,
        timeout_seconds: float = 5,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._index = index
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def write(self, projections: Iterable[DiagnosticEvidenceProjection]) -> int:
        projections = tuple(projections)
        if not projections:
            return 0
        lines: list[str] = []
        for projection in projections:
            lines.append(json.dumps({"index": {"_index": self._index, "_id": projection.evidence_id}}))
            lines.append(json.dumps(projection_index_document(projection), ensure_ascii=True, separators=(",", ":")))
        headers = {"Content-Type": "application/x-ndjson"}
        if self._api_key:
            headers["Authorization"] = f"ApiKey {self._api_key}"
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(f"{self._endpoint}/_bulk", headers=headers, content="\n".join(lines) + "\n")
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise EvidenceSearchError("OpenSearch evidence projection failed") from error
        if payload.get("errors"):
            raise EvidenceSearchError("OpenSearch rejected one or more evidence projections")
        return len(projections)


class OpenSearchEvidenceSearchRepository(EvidenceSearchRepository):
    """Search a bounded evidence projection with mandatory tenant and ACL filters."""

    def __init__(
        self,
        endpoint: str,
        *,
        index: str = "evidence-logs-v1-*,evidence-profiles-v1,evidence-references-v1,evidence-diagnostics-v1",
        api_key: str | None = None,
        timeout_seconds: float = 5,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._index = index
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def search_diagnostics(self, query: EvidenceSearchQuery) -> list[DiagnosticEvidenceChunk]:
        headers = {"Authorization": f"ApiKey {self._api_key}"} if self._api_key else {}
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                f"{self._endpoint}/{self._index}/_search",
                headers=headers,
                json={"size": query.limit, "query": self._query(query), "_source": self._source_fields()},
            )
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise EvidenceSearchError("OpenSearch evidence search failed") from error
        try:
            return [self._chunk(hit) for hit in payload["hits"]["hits"]]
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceSearchError("OpenSearch returned an invalid evidence document") from error

    @staticmethod
    def _source_fields() -> list[str]:
        return [
            "evidence_id",
            "source_type",
            "excerpt",
            "content_hash",
            "source_revision",
            "artifact_uri",
            "file_sha256",
            "manifest_sha256",
            "tenant_id",
            "acl",
            "observed_at",
            "runtime.backend",
            "hardware.accelerator",
            "execution_id",
            "plan_id",
        ]

    @staticmethod
    def _query(query: EvidenceSearchQuery) -> dict[str, Any]:
        filters: list[dict[str, Any]] = [
            {"term": {"tenant_id": query.tenant_id}},
            {"terms": {"acl": list(query.allowed_acl)}},
        ]
        if query.source_types:
            filters.append({"terms": {"source_type": list(query.source_types)}})
        for field, value in (
            ("runtime.backend", query.runtime_backend),
            ("hardware.accelerator", query.accelerator),
            ("execution_id", query.execution_id),
            ("plan_id", query.plan_id),
        ):
            if value is not None:
                filters.append({"term": {field: value}})
        if query.observed_after is not None or query.observed_before is not None:
            bounds = {
                key: value.isoformat()
                for key, value in (("gte", query.observed_after), ("lte", query.observed_before))
                if value is not None
            }
            filters.append({"range": {"observed_at": bounds}})
        lexical_query: dict[str, Any] = {"multi_match": {"query": query.text, "fields": ["excerpt^2", "title"]}}
        if query.query_vector is None:
            must: list[dict[str, Any]] = [lexical_query]
        else:
            must = [
                {
                    "bool": {
                        "should": [
                            lexical_query,
                            {"knn": {"embedding": {"vector": list(query.query_vector), "k": query.limit}}},
                        ],
                        "minimum_should_match": 1,
                    },
                }
            ]
        return {"bool": {"filter": filters, "must": must}}

    @staticmethod
    def _chunk(hit: dict[str, Any]) -> DiagnosticEvidenceChunk:
        source = hit["_source"]
        return DiagnosticEvidenceChunk(
            evidence_id=source["evidence_id"],
            source_type=source["source_type"],
            excerpt=source["excerpt"],
            content_hash=source["content_hash"],
            source_revision=source.get("source_revision"),
            artifact_uri=source.get("artifact_uri"),
            file_sha256=source.get("file_sha256"),
            manifest_sha256=source.get("manifest_sha256"),
            tenant_id=source["tenant_id"],
            acl=tuple(source["acl"]),
            observed_at=source.get("observed_at"),
            runtime_backend=(source.get("runtime") or {}).get("backend"),
            accelerator=(source.get("hardware") or {}).get("accelerator"),
            execution_id=source.get("execution_id"),
            plan_id=source.get("plan_id"),
            score=float(hit.get("_score") or 0),
            ranking_explanation="OpenSearch metadata-filtered lexical/semantic rank",
        )


def verify_artifact_evidence(
    root: Path,
    chunk: DiagnosticEvidenceChunk,
    *,
    approved_roots: Iterable[Path] | None = None,
) -> Path:
    """Resolve and checksum a locally indexed artifact before Agentic uses it."""
    if not all((chunk.artifact_uri, chunk.file_sha256, chunk.manifest_sha256)):
        raise EvidenceSearchError("artifact evidence is missing immutable references")
    root = Path(root).resolve()
    approved_roots = approved_roots or (
        storage_path("data", "artifacts"),
        storage_path("data", "datasets"),
    )
    if not any(root.is_relative_to(Path(item).resolve()) for item in approved_roots):
        raise EvidenceSearchError("artifact evidence root is not approved for retrieval")
    manifest_path = root / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("retention_class") not in {"evidence", "diagnostic"}:
            raise ValueError("manifest retention class is not searchable evidence")
        path = resolve_artifact(root, manifest, chunk.artifact_uri)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise EvidenceSearchError("artifact evidence cannot be resolved from its manifest") from error
    if hashlib.sha256(manifest_bytes).hexdigest() != chunk.manifest_sha256:
        raise EvidenceSearchError("artifact manifest checksum does not match indexed evidence")
    if hashlib.sha256(path.read_bytes()).hexdigest() != chunk.file_sha256:
        raise EvidenceSearchError("artifact checksum does not match indexed evidence")
    return path
