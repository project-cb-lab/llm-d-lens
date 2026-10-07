"""Read-only contracts for non-structured Agentic planning evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

EvidenceSourceType = Literal[
    "deployment_log",
    "profiling_trace",
    "artifact",
    "guide",
    "github",
    "paper",
    "planning_snapshot",
]


class DiagnosticEvidenceProjection(BaseModel):
    """A rebuildable, bounded document that may be indexed for diagnostics."""

    evidence_id: str
    source_type: EvidenceSourceType
    title: str = Field(max_length=500)
    excerpt: str = Field(max_length=8_000)
    content_hash: str
    tenant_id: str = Field(min_length=1, max_length=200)
    acl: tuple[str, ...] = Field(min_length=1, max_length=50)
    source_revision: str | None = None
    artifact_uri: str | None = None
    observed_at: datetime | None = None
    runtime_backend: str | None = Field(default=None, max_length=64)
    accelerator: str | None = Field(default=None, max_length=64)
    execution_id: str | None = Field(default=None, max_length=128)
    plan_id: str | None = Field(default=None, max_length=128)


class EvidenceSearchQuery(BaseModel):
    """A constrained diagnostic or reference lookup, never a performance query."""

    text: str = Field(min_length=1, max_length=2_000)
    tenant_id: str = Field(min_length=1, max_length=200)
    allowed_acl: tuple[str, ...] = Field(min_length=1, max_length=50)
    source_types: tuple[EvidenceSourceType, ...] = ()
    runtime_backend: str | None = Field(default=None, max_length=64)
    accelerator: str | None = Field(default=None, max_length=64)
    execution_id: str | None = Field(default=None, max_length=128)
    plan_id: str | None = Field(default=None, max_length=128)
    observed_after: datetime | None = None
    observed_before: datetime | None = None
    query_vector: tuple[float, ...] | None = Field(default=None, max_length=4_096)
    limit: int = Field(default=10, ge=1, le=50)


class DiagnosticEvidenceChunk(BaseModel):
    """A bounded projection; original artifact content is never returned here."""

    evidence_id: str
    source_type: EvidenceSourceType
    excerpt: str = Field(max_length=8_000)
    content_hash: str
    source_revision: str | None = None
    artifact_uri: str | None = None
    file_sha256: str | None = None
    manifest_sha256: str | None = None
    tenant_id: str
    acl: tuple[str, ...]
    observed_at: datetime | None = None
    runtime_backend: str | None = None
    accelerator: str | None = None
    execution_id: str | None = None
    plan_id: str | None = None
    score: float
    ranking_explanation: str


class EvidenceSearchRepository(Protocol):
    """Search a rebuildable non-structured evidence projection."""

    async def search_diagnostics(self, query: EvidenceSearchQuery) -> list[DiagnosticEvidenceChunk]: ...


class InMemoryEvidenceSearchRepository:
    """Deterministic test fake; it enforces the same tenant/ACL boundary."""

    def __init__(self, chunks: tuple[DiagnosticEvidenceChunk, ...] = ()) -> None:
        self._chunks = chunks

    async def search_diagnostics(self, query: EvidenceSearchQuery) -> list[DiagnosticEvidenceChunk]:
        words = set(query.text.lower().split())
        matches = [
            chunk
            for chunk in self._chunks
            if chunk.tenant_id == query.tenant_id
            and set(chunk.acl).intersection(query.allowed_acl)
            and (not query.source_types or chunk.source_type in query.source_types)
            and (query.runtime_backend is None or query.runtime_backend == chunk.runtime_backend)
            and (query.accelerator is None or query.accelerator == chunk.accelerator)
            and (query.execution_id is None or query.execution_id == chunk.execution_id)
            and (query.plan_id is None or query.plan_id == chunk.plan_id)
            and (
                query.observed_after is None
                or (chunk.observed_at is not None and chunk.observed_at >= query.observed_after)
            )
            and (
                query.observed_before is None
                or (chunk.observed_at is not None and chunk.observed_at <= query.observed_before)
            )
        ]
        return sorted(
            matches,
            key=lambda chunk: (
                -len(words.intersection(chunk.excerpt.lower().split())),
                -chunk.score,
                chunk.evidence_id,
            ),
        )[: query.limit]
