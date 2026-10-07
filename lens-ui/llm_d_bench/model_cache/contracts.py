"""Versioned input and output contracts for the Model Cache module.

Model Cache associates a registered Storage volume (``llm_d_bench.storage``)
with model files downloaded onto it. It never mutates a ``StorageVolume``
record — it only reads volumes to validate requests (see
``docs/fern/pages/api-reference/model-cache.mdx`` for the module boundary).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ModelSourceKind(StrEnum):
    HUGGINGFACE = "huggingface"
    MODEL_CATALOG = "model-catalog"


_REPO_ID_PATTERN = r"^[\w.\-]+/[\w.\-]+$"
_REVISION_PATTERN = r"^[\w.\-/]+$"


class HuggingFaceSource(StrictModel):
    repo_id: str = Field(alias="repoId", pattern=_REPO_ID_PATTERN)
    revision: str = Field(default="main", pattern=_REVISION_PATTERN)


class ModelCatalogSource(StrictModel):
    """Placeholder for the not-yet-implemented Model Catalog data source."""

    catalog_entry_id: str = Field(alias="catalogEntryId", min_length=1)


class ModelSource(StrictModel):
    kind: ModelSourceKind
    huggingface: HuggingFaceSource | None = None
    model_catalog: ModelCatalogSource | None = Field(default=None, alias="modelCatalog")

    @model_validator(mode="after")
    def _require_matching_spec(self):
        spec_by_kind = {
            ModelSourceKind.HUGGINGFACE: self.huggingface,
            ModelSourceKind.MODEL_CATALOG: self.model_catalog,
        }
        if spec_by_kind[self.kind] is None:
            raise ValueError(f"{self.kind.value} requires a matching spec payload")
        for kind, spec in spec_by_kind.items():
            if kind != self.kind and spec is not None:
                raise ValueError(f"unexpected {kind.value} spec for a {self.kind.value} model source")
        return self

    def display_name(self) -> str:
        if self.kind == ModelSourceKind.HUGGINGFACE and self.huggingface:
            return f"{self.huggingface.repo_id}@{self.huggingface.revision}"
        if self.kind == ModelSourceKind.MODEL_CATALOG and self.model_catalog:
            return self.model_catalog.catalog_entry_id
        return ""


class TokenSourceMode(StrEnum):
    NONE = "none"
    EXISTING_SECRET = "existing-secret"  # noqa: S105 - enum value, not a credential
    HOST = "host"


class TokenSource(StrictModel):
    mode: TokenSourceMode = TokenSourceMode.NONE
    namespace: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _require_secret_coordinates(self):
        if self.mode == TokenSourceMode.EXISTING_SECRET and (not self.namespace or not self.name):
            raise ValueError("existing-secret token source requires namespace and name")
        return self


class NodeDownloadStatus(StrictModel):
    node: str
    status: Literal["pending", "downloading", "ready", "failed"] = "pending"
    failure_detail: str | None = Field(default=None, alias="failureDetail")


class ModelCacheEntryStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


def hf_cache_path(repo_id: str) -> str:
    """Derive the standard HuggingFace cache subdirectory name for a repo id.

    ``huggingface_hub`` (and therefore ``hf download``/vLLM/transformers) does
    NOT cache directly under ``HF_HOME``: unless ``HF_HUB_CACHE`` is set
    explicitly, the effective cache dir is ``$HF_HOME/hub`` (see
    ``huggingface_hub.constants``). This module only sets ``HF_HOME`` (to stay
    byte-for-byte consistent with Deploy's ``auto-cache`` mode in
    ``deploy/providers/storage_mount.py``, which also only sets ``HF_HOME``),
    so the on-disk path is ``hub/models--org--name``, not ``models--org--name``.
    Matching that real path here (rather than the wrong flat guess) is what
    lets this module's own delete Job find and remove the right directory.
    """
    org, name = repo_id.split("/", 1)
    return f"hub/models--{org}--{name}"


class ModelCacheCreateRequest(StrictModel):
    cluster_id: str = Field(alias="clusterId", min_length=1)
    storage_volume_id: str = Field(alias="storageVolumeId", min_length=1)
    source: ModelSource
    token_source: TokenSource = Field(default_factory=TokenSource, alias="tokenSource")


class HuggingFaceModelSummary(StrictModel):
    """One row in the Hub search results list (model source picker)."""

    repo_id: str = Field(alias="repoId")
    likes: int = 0
    downloads: int = 0
    pipeline_tag: str | None = Field(default=None, alias="pipelineTag")
    tags: list[str] = Field(default_factory=list)
    last_modified: str | None = Field(default=None, alias="lastModified")
    gated: bool | str = False

    @classmethod
    def from_hub_json(cls, raw: dict) -> HuggingFaceModelSummary:
        return cls(
            repoId=str(raw.get("id") or raw.get("modelId") or ""),
            likes=int(raw.get("likes") or 0),
            downloads=int(raw.get("downloads") or 0),
            pipelineTag=raw.get("pipeline_tag"),
            tags=list(raw.get("tags") or []),
            lastModified=raw.get("lastModified"),
            gated=raw.get("gated", False),
        )


class HuggingFaceModelDetail(HuggingFaceModelSummary):
    """Full detail shown in the model source picker once a repo is selected."""

    readme: str = ""

    @classmethod
    def from_hub_payload(cls, payload: dict) -> HuggingFaceModelDetail:
        info = payload.get("info") or {}
        summary = HuggingFaceModelSummary.from_hub_json(info)
        return cls(**summary.model_dump(by_alias=True), readme=payload.get("readme") or "")


class ModelCacheEntry(StrictModel):
    id: str = Field(default_factory=lambda: f"model-cache-{uuid4().hex[:8]}")
    cluster_id: str = Field(alias="clusterId")
    storage_volume_id: str = Field(alias="storageVolumeId")
    source: ModelSource
    token_source: TokenSource = Field(default_factory=TokenSource, alias="tokenSource")
    status: ModelCacheEntryStatus = ModelCacheEntryStatus.PENDING
    cache_path: str = Field(alias="cachePath")
    size_bytes: int | None = Field(default=None, alias="sizeBytes")
    node_progress: list[NodeDownloadStatus] = Field(default_factory=list, alias="nodeProgress")
    failure_detail: str | None = Field(default=None, alias="failureDetail")
    created_at: datetime = Field(default_factory=utcnow, alias="createdAt")
    updated_at: datetime = Field(default_factory=utcnow, alias="updatedAt")

    def api_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)
