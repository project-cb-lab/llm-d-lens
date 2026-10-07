"""Tests for the JSON-file-backed storage volume store."""

import pytest

from llm_d_bench.storage.contracts import LocalDiskSpec, StorageVolume, StorageVolumeKind, StorageVolumeStatus
from llm_d_bench.storage.store import StorageVolumeStore, StorageVolumeStoreError


def _volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "qwen-cache",
        "kind": StorageVolumeKind.LOCAL_DISK,
        "capacity": "100Gi",
        "readOnly": True,
        "localDisk": LocalDiskSpec(hostPath="/data/models"),
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def test_create_and_get_roundtrip(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    store.create(volume)
    fetched = store.get(volume.id)
    assert fetched is not None
    assert fetched.name == "qwen-cache"
    assert fetched.local_disk.host_path == "/data/models"


def test_create_rejects_duplicate_id(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    store.create(volume)
    with pytest.raises(StorageVolumeStoreError):
        store.create(volume)


def test_get_missing_returns_none(tmp_path):
    store = StorageVolumeStore(tmp_path)
    assert store.get("storage-missing") is None


def test_list_sorted_by_created_at(tmp_path):
    store = StorageVolumeStore(tmp_path)
    first = _volume(name="first")
    second = _volume(name="second")
    store.create(second)
    store.create(first)
    names = [volume.name for volume in store.list()]
    assert set(names) == {"first", "second"}


def test_save_updates_existing_record(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    store.create(volume)
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-xyz"
    store.save(volume)
    fetched = store.get(volume.id)
    assert fetched.status == StorageVolumeStatus.READY
    assert fetched.pvc_name == "prism-storage-xyz"


def test_save_missing_raises(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    with pytest.raises(StorageVolumeStoreError):
        store.save(volume)


def test_delete_is_idempotent(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    store.create(volume)
    store.delete(volume.id)
    assert store.get(volume.id) is None
    store.delete(volume.id)
