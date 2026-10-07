"""Tests for the Model Cache entry store."""

from llm_d_bench.model_cache.contracts import HuggingFaceSource, ModelCacheEntry, ModelSource, ModelSourceKind
from llm_d_bench.model_cache.store import ModelCacheStore


def _entry(**overrides) -> ModelCacheEntry:
    defaults = {
        "clusterId": "cluster-1",
        "storageVolumeId": "vol-1",
        "source": ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        "cachePath": "models--org--model",
    }
    defaults.update(overrides)
    return ModelCacheEntry(**defaults)


def test_create_and_get_round_trips(tmp_path):
    store = ModelCacheStore(tmp_path)
    entry = _entry()
    store.create(entry)
    loaded = store.get(entry.id)
    assert loaded is not None
    assert loaded.id == entry.id
    assert loaded.source.huggingface.repo_id == "org/model"


def test_list_is_sorted_by_created_at(tmp_path):
    store = ModelCacheStore(tmp_path)
    first = _entry(storageVolumeId="vol-a")
    second = _entry(storageVolumeId="vol-b")
    store.create(second)
    store.create(first)
    assert [entry.id for entry in store.list()] in ([first.id, second.id], [second.id, first.id])
    assert len(store.list()) == 2


def test_find_by_volume_and_source(tmp_path):
    store = ModelCacheStore(tmp_path)
    entry = _entry(storageVolumeId="vol-1")
    store.create(entry)
    found = store.find_by_volume_and_source("vol-1", "org/model@main")
    assert found is not None
    assert found.id == entry.id
    assert store.find_by_volume_and_source("vol-1", "org/other@main") is None
    assert store.find_by_volume_and_source("vol-2", "org/model@main") is None


def test_list_for_volume_filters_by_volume(tmp_path):
    store = ModelCacheStore(tmp_path)
    store.create(_entry(storageVolumeId="vol-1"))
    store.create(_entry(storageVolumeId="vol-2"))
    assert [entry.storage_volume_id for entry in store.list_for_volume("vol-1")] == ["vol-1"]


def test_save_updates_existing_record(tmp_path):
    store = ModelCacheStore(tmp_path)
    entry = _entry()
    store.create(entry)
    entry.size_bytes = 1234
    store.save(entry)
    assert store.get(entry.id).size_bytes == 1234


def test_delete_removes_record(tmp_path):
    store = ModelCacheStore(tmp_path)
    entry = _entry()
    store.create(entry)
    store.delete(entry.id)
    assert store.get(entry.id) is None
