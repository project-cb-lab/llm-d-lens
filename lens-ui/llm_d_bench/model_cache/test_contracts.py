"""Tests for Model Cache contract validation."""

import pytest
from pydantic import ValidationError

from llm_d_bench.model_cache.contracts import (
    HuggingFaceSource,
    ModelCacheEntry,
    ModelSource,
    ModelSourceKind,
    TokenSource,
    TokenSourceMode,
    hf_cache_path,
)


def test_huggingface_source_requires_matching_spec():
    with pytest.raises(ValidationError):
        ModelSource(kind=ModelSourceKind.HUGGINGFACE)


def test_huggingface_source_rejects_mismatched_spec():
    with pytest.raises(ValidationError):
        ModelSource(
            kind=ModelSourceKind.MODEL_CATALOG,
            huggingface=HuggingFaceSource(repoId="org/model"),
        )


def test_huggingface_source_display_name():
    source = ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model"))
    assert source.display_name() == "org/model@main"


def test_token_source_existing_secret_requires_coordinates():
    with pytest.raises(ValidationError):
        TokenSource(mode=TokenSourceMode.EXISTING_SECRET)
    token = TokenSource(mode=TokenSourceMode.EXISTING_SECRET, namespace="ns", name="secret")
    assert token.name == "secret"


def test_hf_cache_path_matches_hf_layout():
    assert hf_cache_path("meta-llama/Llama-3-8B") == "hub/models--meta-llama--Llama-3-8B"


def test_model_cache_entry_api_payload_uses_camel_case():
    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId="vol-1",
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
    )
    payload = entry.api_payload()
    assert payload["clusterId"] == "cluster-1"
    assert payload["storageVolumeId"] == "vol-1"
    assert payload["cachePath"] == "models--org--model"
    assert payload["status"] == "pending"
