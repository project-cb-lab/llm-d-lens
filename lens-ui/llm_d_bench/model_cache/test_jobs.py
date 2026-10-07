"""Tests for Model Cache Job manifest building and orchestration."""

from dataclasses import dataclass

import pytest

from llm_d_bench.model_cache import jobs as jobs_module
from llm_d_bench.model_cache.contracts import (
    HuggingFaceSource,
    ModelCacheEntry,
    ModelSource,
    ModelSourceKind,
    NodeDownloadStatus,
    TokenSource,
    TokenSourceMode,
)
from llm_d_bench.model_cache.jobs import (
    ModelCacheJobError,
    _delete_command,
    _download_command,
    _ensure_token_secret,
    _environment,
    _job_manifest,
    delete_jobs_for_volume,
    missing_nodes,
    poll_progress,
    start_delete,
    start_download,
    start_download_for_new_nodes,
)
from llm_d_bench.storage.contracts import LocalDiskSpec, NfsSpec, StorageNodeSummary, StorageVolume, StorageVolumeKind


@dataclass
class FakeResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class FakeRunner:
    def __init__(self, *, ok: bool = True, stdout: str = "") -> None:
        self.ok = ok
        self.stdout = stdout
        self.calls: list[list[str]] = []

    async def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return FakeResult(0 if self.ok else 1, stdout=self.stdout if self.ok else "", stderr="" if self.ok else "boom")


def _entry(**overrides) -> ModelCacheEntry:
    defaults = {
        "clusterId": "cluster-1",
        "storageVolumeId": "vol-1",
        "source": ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        "cachePath": "models--org--model",
    }
    defaults.update(overrides)
    return ModelCacheEntry(**defaults)


def _nfs_volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "shared-cache",
        "kind": StorageVolumeKind.NFS,
        "capacity": "10Gi",
        "readOnly": False,
        "nfs": NfsSpec(server="nfs.example.com", path="/export/shared-cache"),
        "pvcName": "prism-storage-vol-1",
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def _local_disk_volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "local-cache",
        "kind": StorageVolumeKind.LOCAL_DISK,
        "capacity": "100Gi",
        "readOnly": False,
        "localDisk": LocalDiskSpec(hostPath="/data/models"),
        "pvcName": "prism-storage-vol-1",
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def test_download_command_installs_cli_then_downloads_in_cache_mode():
    entry = _entry()
    command = _download_command(entry)
    assert command[:2] == ["sh", "-c"]
    script = command[2]
    assert "pip install --quiet --no-cache-dir 'huggingface_hub[cli,hf_transfer]'" in script
    assert script.endswith("hf download org/model --revision main")


def test_delete_command_removes_only_the_entry_cache_path():
    entry = _entry(cachePath="models--org--model")
    assert _delete_command(entry) == ["rm", "-rf", "/model-cache/models--org--model"]


def test_environment_omits_hf_token_when_token_mode_is_none():
    entry = _entry(tokenSource=TokenSource(mode=TokenSourceMode.NONE))
    names = {item["name"] for item in _environment(entry)}
    assert "HF_TOKEN" not in names


def test_environment_references_local_secret_for_host_token_mode():
    entry = _entry(tokenSource=TokenSource(mode=TokenSourceMode.HOST))
    environment = {item["name"]: item for item in _environment(entry)}
    assert environment["HF_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "llm-d-hf-token"


def test_environment_references_local_secret_for_existing_secret_token_mode():
    # Even though the user picked a Secret from a different namespace/name,
    # _environment() must reference the fixed local copy (see
    # _ensure_token_secret) -- a secretKeyRef can never resolve a Secret
    # living in another namespace, which was the original bug.
    entry = _entry(
        tokenSource=TokenSource(mode=TokenSourceMode.EXISTING_SECRET, namespace="other-ns", name="other-secret")
    )
    environment = {item["name"]: item for item in _environment(entry)}
    assert environment["HF_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "llm-d-hf-token"


@pytest.mark.asyncio
async def test_ensure_token_secret_is_noop_when_mode_is_none(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)
    entry = _entry(tokenSource=TokenSource(mode=TokenSourceMode.NONE))
    await _ensure_token_secret(entry, namespace="llm-d-bench-storage")
    assert runner.calls == []


@pytest.mark.asyncio
async def test_ensure_token_secret_is_noop_when_host_token_file_missing(monkeypatch, tmp_path):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)
    monkeypatch.setattr(jobs_module.Path, "home", classmethod(lambda cls: tmp_path))
    entry = _entry(tokenSource=TokenSource(mode=TokenSourceMode.HOST))
    await _ensure_token_secret(entry, namespace="llm-d-bench-storage")
    assert runner.calls == []


@pytest.mark.asyncio
async def test_ensure_token_secret_applies_host_file_token_as_local_secret(monkeypatch, tmp_path):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)
    monkeypatch.setattr(jobs_module.Path, "home", classmethod(lambda cls: tmp_path))
    token_dir = tmp_path / ".cache" / "huggingface"
    token_dir.mkdir(parents=True)
    (token_dir / "token").write_text("hf_abc123\n", encoding="utf-8")

    entry = _entry(tokenSource=TokenSource(mode=TokenSourceMode.HOST))
    await _ensure_token_secret(entry, namespace="llm-d-bench-storage")

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call[:3] == ["kubectl", "apply", "--namespace"]


@pytest.mark.asyncio
async def test_ensure_token_secret_copies_existing_secret_cross_namespace(monkeypatch):
    import json as json_module

    responses = iter(
        [
            FakeResult(0, stdout=json_module.dumps({"data": {"HF_TOKEN": "aGZfYWJj"}})),
            FakeResult(0),
        ]
    )

    class _Runner:
        def __init__(self):
            self.calls: list[list[str]] = []

        async def run(self, argv, **kwargs):
            self.calls.append(list(argv))
            return next(responses)

    runner = _Runner()
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)
    entry = _entry(
        tokenSource=TokenSource(mode=TokenSourceMode.EXISTING_SECRET, namespace="deploy-ns", name="hf-secret")
    )

    await _ensure_token_secret(entry, namespace="llm-d-bench-storage")

    assert runner.calls[0][:5] == ["kubectl", "get", "secret", "hf-secret", "--namespace"]
    assert runner.calls[1][:3] == ["kubectl", "apply", "--namespace"]


@pytest.mark.asyncio
async def test_ensure_token_secret_raises_when_source_secret_missing_hf_token(monkeypatch):
    import json as json_module

    class _Runner:
        def __init__(self):
            self.calls: list[list[str]] = []

        async def run(self, argv, **kwargs):
            self.calls.append(list(argv))
            return FakeResult(0, stdout=json_module.dumps({"data": {}}))

    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: _Runner())
    entry = _entry(
        tokenSource=TokenSource(mode=TokenSourceMode.EXISTING_SECRET, namespace="deploy-ns", name="hf-secret")
    )
    with pytest.raises(ModelCacheJobError):
        await _ensure_token_secret(entry, namespace="llm-d-bench-storage")


@pytest.mark.asyncio
async def test_poll_progress_recovers_empty_shared_node_progress(monkeypatch):
    async def fake_list_resources(*args, **kwargs):
        return [
            {
                "metadata": {"name": "model-cache-download-model-cache-abc123"},
                "status": {"succeeded": 1},
            }
        ]

    monkeypatch.setattr(jobs_module, "list_resources", fake_list_resources)
    entry = _entry(id="model-cache-abc123", nodeProgress=[])
    refreshed = await poll_progress(entry, volume=_nfs_volume(), action="download")

    assert [(progress.node, progress.status) for progress in refreshed.node_progress] == [("*", "ready")]


@pytest.mark.asyncio
async def test_delete_jobs_for_volume_selects_by_storage_volume_label(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)
    volume = _nfs_volume()

    await delete_jobs_for_volume(volume, cluster_id="cluster-1")

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call[:3] == ["kubectl", "delete", "job"]
    assert "--selector" in call
    assert call[call.index("--selector") + 1] == f"prism.ai/storage-volume-id={volume.id}"


def test_job_manifest_defaults_to_public_pullable_image(monkeypatch):
    monkeypatch.delenv("MODEL_CACHE_DOWNLOADER_IMAGE", raising=False)
    entry = _entry()
    volume = _nfs_volume()
    manifest = _job_manifest(entry, volume, node="*", command=["echo", "hi"], action="download")
    assert manifest["spec"]["template"]["spec"]["containers"][0]["image"] == "python:3.12-slim"


def test_job_manifest_honors_downloader_image_override(monkeypatch):
    monkeypatch.setenv("MODEL_CACHE_DOWNLOADER_IMAGE", "registry.internal/model-cache-downloader:v1")
    entry = _entry()
    volume = _nfs_volume()
    manifest = _job_manifest(entry, volume, node="*", command=["echo", "hi"], action="download")
    assert (
        manifest["spec"]["template"]["spec"]["containers"][0]["image"] == "registry.internal/model-cache-downloader:v1"
    )


def test_environment_omits_proxy_vars_when_unset(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    entry = _entry()
    names = {item["name"] for item in jobs_module._environment(entry)}
    assert "HTTP_PROXY" not in names
    assert "HTTPS_PROXY" not in names


def test_environment_forwards_proxy_vars_from_backend_host(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    entry = _entry()
    environment = {item["name"]: item["value"] for item in jobs_module._environment(entry)}
    assert environment["HTTP_PROXY"] == "http://proxy.internal:8080"
    assert environment["HTTPS_PROXY"] == "http://proxy.internal:8080"
    assert "localhost" in environment["NO_PROXY"]
    assert ".cluster.local" in environment["NO_PROXY"]


def test_environment_falls_back_to_lowercase_proxy_vars(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", "http://proxy.internal:3128")
    entry = _entry()
    environment = {item["name"]: item["value"] for item in jobs_module._environment(entry)}
    assert environment["HTTP_PROXY"] == "http://proxy.internal:3128"


def test_job_manifest_sets_node_selector_for_local_disk():
    entry = _entry()
    volume = _local_disk_volume()
    manifest = _job_manifest(entry, volume, node="node-1", command=["echo", "hi"], action="download")
    assert manifest["spec"]["template"]["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "node-1"}
    assert manifest["metadata"]["name"].endswith("node-1")


def test_job_manifest_omits_node_selector_for_shared_volumes():
    entry = _entry()
    volume = _nfs_volume()
    manifest = _job_manifest(entry, volume, node="*", command=["echo", "hi"], action="download")
    assert "nodeSelector" not in manifest["spec"]["template"]["spec"]
    assert manifest["metadata"]["name"] == f"model-cache-download-{entry.id}"


@pytest.mark.asyncio
async def test_start_download_creates_one_job_for_shared_volume(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)

    entry = _entry()
    volume = _nfs_volume()
    result = await start_download(entry, volume=volume)
    assert len(result.node_progress) == 1
    assert result.node_progress[0].status == "downloading"
    apply_calls = [call for call in runner.calls if "apply" in call]
    assert len(apply_calls) == 1


@pytest.mark.asyncio
async def test_start_download_creates_one_job_per_ready_node_for_local_disk(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)

    async def fake_discover_nodes(cluster_id):
        return [
            StorageNodeSummary(name="node-1", ready=True),
            StorageNodeSummary(name="node-2", ready=False),
            StorageNodeSummary(name="node-3", ready=True),
        ]

    monkeypatch.setattr(jobs_module, "discover_nodes", fake_discover_nodes)

    entry = _entry()
    volume = _local_disk_volume()
    result = await start_download(entry, volume=volume)
    assert {progress.node for progress in result.node_progress} == {"node-1", "node-3"}
    apply_calls = [call for call in runner.calls if "apply" in call]
    assert len(apply_calls) == 2


@pytest.mark.asyncio
async def test_start_download_skips_control_plane_only_nodes_for_local_disk(monkeypatch):
    """Reproduces a real cluster bug: a control-plane-only node (tainted
    NoSchedule, not also a worker) gets a download Job scheduled with a
    ``nodeSelector`` targeting it directly, but the Job sets no tolerations,
    so the Pod sits ``Pending`` forever ("0/N nodes are available: 1 node(s)
    had untolerated taint(s)"). ``_target_nodes`` must exclude nodes where
    ``schedulable=False``. A single-node dev cluster whose only node is both
    control-plane and worker (taint removed, schedulable=True) must still be
    included.
    """
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)

    async def fake_discover_nodes(cluster_id):
        return [
            StorageNodeSummary(name="control-plane-only", ready=True, schedulable=False),
            StorageNodeSummary(name="worker-1", ready=True, schedulable=True),
            StorageNodeSummary(name="single-node-control-plane", ready=True, schedulable=True),
        ]

    monkeypatch.setattr(jobs_module, "discover_nodes", fake_discover_nodes)

    entry = _entry()
    volume = _local_disk_volume()
    result = await start_download(entry, volume=volume)
    assert {progress.node for progress in result.node_progress} == {"worker-1", "single-node-control-plane"}
    apply_calls = [call for call in runner.calls if "apply" in call]
    assert len(apply_calls) == 2


@pytest.mark.asyncio
async def test_start_download_raises_on_kubectl_failure(monkeypatch):
    runner = FakeRunner(ok=False)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)

    entry = _entry()
    volume = _nfs_volume()
    with pytest.raises(ModelCacheJobError):
        await start_download(entry, volume=volume)


@pytest.mark.asyncio
async def test_start_delete_reuses_existing_node_progress(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)

    entry = _entry(nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")])
    volume = _local_disk_volume()
    result = await start_delete(entry, volume=volume)
    assert [progress.node for progress in result.node_progress] == ["node-1"]
    apply_calls = [call for call in runner.calls if "apply" in call]
    assert len(apply_calls) == 1


@pytest.mark.asyncio
async def test_ensure_delete_started_is_noop_when_delete_job_already_exists(monkeypatch):
    entry = _entry(nodeProgress=[NodeDownloadStatus(node="node-1", status="downloading")])
    volume = _local_disk_volume()

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        assert kwargs["selector"] == f"prism.ai/model-cache-entry-id={entry.id},prism.ai/model-cache-action=delete"
        return [{"metadata": {"name": "model-cache-delete-something"}}]

    monkeypatch.setattr(jobs_module, "list_resources", fake_list_resources)

    async def fail_start_delete(entry, *, volume, nodes=None):
        raise AssertionError("start_delete should not be called when a delete Job already exists")

    monkeypatch.setattr(jobs_module, "start_delete", fail_start_delete)

    result = await jobs_module.ensure_delete_started(entry, volume=volume)
    assert result is entry


@pytest.mark.asyncio
async def test_ensure_delete_started_recreates_missing_delete_job(monkeypatch):
    """Reproduces the stuck-forever bug: a `deleting` entry whose delete Job
    was never actually created (e.g. backend restarted between
    request_delete() and provision_delete()). ``ensure_delete_started`` must
    detect no delete Job exists and re-run ``start_delete()``.
    """
    entry = _entry(nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")])
    volume = _local_disk_volume()

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return []

    monkeypatch.setattr(jobs_module, "list_resources", fake_list_resources)

    called = {}

    async def fake_start_delete(entry, *, volume, nodes=None):
        called["ran"] = True
        entry.node_progress = [NodeDownloadStatus(node="node-1", status="downloading")]
        return entry

    monkeypatch.setattr(jobs_module, "start_delete", fake_start_delete)

    result = await jobs_module.ensure_delete_started(entry, volume=volume)
    assert called.get("ran") is True
    assert result.node_progress[0].status == "downloading"


@pytest.mark.asyncio
async def test_poll_progress_reports_success(monkeypatch):
    entry = _entry(nodeProgress=[NodeDownloadStatus(node="*", status="downloading")])
    volume = _nfs_volume()

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        assert resource == "jobs"
        return [
            {
                "metadata": {"name": f"model-cache-download-{entry.id}"},
                "spec": {"backoffLimit": 2},
                "status": {"succeeded": 1},
            }
        ]

    monkeypatch.setattr(jobs_module, "list_resources", fake_list_resources)
    result = await jobs_module.poll_progress(entry, volume=volume, action="download")
    assert result.node_progress[0].status == "ready"


@pytest.mark.asyncio
async def test_tail_logs_returns_stdout_on_success(monkeypatch):
    runner_calls_stdout = "line1\nline2"

    class _Runner(FakeRunner):
        async def run(self, argv, **kwargs):
            self.calls.append(list(argv))
            return FakeResult(0, stdout=runner_calls_stdout)

    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: _Runner())

    entry = _entry()
    volume = _nfs_volume()
    logs = await jobs_module.tail_logs(entry, volume=volume, node="*", action="download")
    assert logs == runner_calls_stdout


@pytest.mark.asyncio
async def test_tail_logs_returns_stderr_on_failure(monkeypatch):
    class _Runner(FakeRunner):
        async def run(self, argv, **kwargs):
            self.calls.append(list(argv))
            return FakeResult(1, stderr="pod not found")

    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: _Runner())

    entry = _entry()
    volume = _nfs_volume()
    logs = await jobs_module.tail_logs(entry, volume=volume, node="*", action="download")
    assert "pod not found" in logs
    entry = _entry(nodeProgress=[NodeDownloadStatus(node="*", status="downloading")])
    volume = _nfs_volume()

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {
                "metadata": {"name": f"model-cache-download-{entry.id}"},
                "spec": {"backoffLimit": 2},
                "status": {
                    "failed": 2,
                    "conditions": [{"type": "Failed", "message": "ImagePullBackOff"}],
                },
            }
        ]

    monkeypatch.setattr(jobs_module, "list_resources", fake_list_resources)
    result = await jobs_module.poll_progress(entry, volume=volume, action="download")
    assert result.node_progress[0].status == "failed"
    assert "ImagePullBackOff" in (result.node_progress[0].failure_detail or "")


@pytest.mark.asyncio
async def test_missing_nodes_reports_nodes_not_yet_targeted(monkeypatch):
    async def fake_discover_nodes(cluster_id):
        return [
            StorageNodeSummary(name="node-1", ready=True),
            StorageNodeSummary(name="node-2", ready=True),
        ]

    monkeypatch.setattr(jobs_module, "discover_nodes", fake_discover_nodes)
    entry = _entry(nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")])
    volume = _local_disk_volume()
    assert await missing_nodes(entry, volume) == ["node-2"]


@pytest.mark.asyncio
async def test_missing_nodes_empty_when_all_nodes_already_tracked(monkeypatch):
    async def fake_discover_nodes(cluster_id):
        return [StorageNodeSummary(name="node-1", ready=True)]

    monkeypatch.setattr(jobs_module, "discover_nodes", fake_discover_nodes)
    entry = _entry(nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")])
    volume = _local_disk_volume()
    assert await missing_nodes(entry, volume) == []


@pytest.mark.asyncio
async def test_missing_nodes_always_empty_for_shared_volumes(monkeypatch):
    async def fake_discover_nodes(cluster_id):
        raise AssertionError("discover_nodes should not be called for nfs/dynamic-pvc volumes")

    monkeypatch.setattr(jobs_module, "discover_nodes", fake_discover_nodes)
    entry = _entry(nodeProgress=[NodeDownloadStatus(node="*", status="ready")])
    volume = _nfs_volume()
    assert await missing_nodes(entry, volume) == []


@pytest.mark.asyncio
async def test_start_download_for_new_nodes_appends_without_disturbing_ready_nodes(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)

    async def fake_discover_nodes(cluster_id):
        return [
            StorageNodeSummary(name="node-1", ready=True),
            StorageNodeSummary(name="node-2", ready=True),
        ]

    monkeypatch.setattr(jobs_module, "discover_nodes", fake_discover_nodes)
    entry = _entry(nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")])
    volume = _local_disk_volume()

    result = await start_download_for_new_nodes(entry, volume=volume)
    assert result is not None
    by_node = {progress.node: progress.status for progress in result.node_progress}
    assert by_node == {"node-1": "ready", "node-2": "downloading"}
    apply_calls = [call for call in runner.calls if "apply" in call]
    assert len(apply_calls) == 1


@pytest.mark.asyncio
async def test_start_download_for_new_nodes_is_noop_when_nothing_missing(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)

    async def fake_discover_nodes(cluster_id):
        return [StorageNodeSummary(name="node-1", ready=True)]

    monkeypatch.setattr(jobs_module, "discover_nodes", fake_discover_nodes)
    entry = _entry(nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")])
    volume = _local_disk_volume()

    result = await start_download_for_new_nodes(entry, volume=volume)
    assert result is None
    assert not runner.calls


@pytest.mark.parametrize(
    ("dirname", "expected"),
    [
        ("models--org--model", "org/model"),
        ("models--meta-llama--Llama-3-8B", "meta-llama/Llama-3-8B"),
        ("not-a-model-dir", None),
        ("models--", None),
        ("models--onlyorg", None),
        ("", None),
    ],
)
def test_repo_id_from_scan_dirname(dirname, expected):
    assert jobs_module._repo_id_from_scan_dirname(dirname) == expected


@pytest.mark.asyncio
async def test_scan_existing_models_shared_volume_returns_repo_ids_with_wildcard_node(monkeypatch):
    """An nfs/dynamic-pvc volume runs a single shared scan Job; any repo found
    is visible from every node, so it's reported under the ``"*"`` node key."""
    volume = _nfs_volume()

    apply_calls = []

    async def fake_apply_job(manifest, *, cluster_id):
        apply_calls.append(manifest["metadata"]["name"])

    monkeypatch.setattr(jobs_module, "_apply_job", fake_apply_job)

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        job_name = jobs_module._scan_job_name(volume, node=jobs_module._ALL_NODES_KEY)
        return [{"metadata": {"name": job_name}, "status": {"succeeded": 1}}]

    monkeypatch.setattr(jobs_module, "list_resources", fake_list_resources)

    runner = FakeRunner(ok=True, stdout="models--org--model-a\nmodels--org--model-b\nnot-a-model-dir\n")
    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: runner)

    found = await jobs_module.scan_existing_models(volume, cluster_id="cluster-1")

    assert found == {"org/model-a": ["*"], "org/model-b": ["*"]}
    assert len(apply_calls) == 1


@pytest.mark.asyncio
async def test_scan_existing_models_local_disk_reports_only_nodes_that_have_it(monkeypatch):
    """A local-disk volume runs one scan Job per ready node; a model found on
    only some nodes must be reported under only those node names so the
    node-drift/"Sync new nodes" flow can detect the gap on the rest."""
    volume = _local_disk_volume()

    async def fake_ready_schedulable_node_names(cluster_id):
        return ["node-a", "node-b"]

    monkeypatch.setattr(jobs_module, "_ready_schedulable_node_names", fake_ready_schedulable_node_names)

    async def fake_apply_job(manifest, *, cluster_id):
        return None

    monkeypatch.setattr(jobs_module, "_apply_job", fake_apply_job)

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        selector = kwargs["selector"]
        assert selector == f"prism.ai/storage-volume-id={volume.id},prism.ai/model-cache-action=scan"
        node_a_name = jobs_module._scan_job_name(volume, node="node-a")
        node_b_name = jobs_module._scan_job_name(volume, node="node-b")
        return [
            {"metadata": {"name": node_a_name}, "status": {"succeeded": 1}},
            {"metadata": {"name": node_b_name}, "status": {"succeeded": 1}},
        ]

    monkeypatch.setattr(jobs_module, "list_resources", fake_list_resources)

    class _PerNodeRunner:
        def __init__(self):
            self.calls = []

        async def run(self, argv, **kwargs):
            self.calls.append(list(argv))
            job_ref = argv[2]  # "job/<name>"
            if job_ref.endswith(jobs_module._scan_job_name(volume, node="node-a")):
                return FakeResult(0, stdout="models--org--model\n")
            return FakeResult(0, stdout="")

    monkeypatch.setattr(jobs_module, "scoped_runner", lambda cluster_id: _PerNodeRunner())

    found = await jobs_module.scan_existing_models(volume, cluster_id="cluster-1")

    assert found == {"org/model": ["node-a"]}


@pytest.mark.asyncio
async def test_scan_existing_models_ignores_a_node_whose_scan_job_fails(monkeypatch):
    """Best-effort semantics: a node whose scan Job times out/fails must not
    fail the whole scan -- just contribute nothing for that node."""
    volume = _local_disk_volume()

    async def fake_ready_schedulable_node_names(cluster_id):
        return ["node-a"]

    monkeypatch.setattr(jobs_module, "_ready_schedulable_node_names", fake_ready_schedulable_node_names)

    async def fake_apply_job(manifest, *, cluster_id):
        raise ModelCacheJobError("boom")

    monkeypatch.setattr(jobs_module, "_apply_job", fake_apply_job)

    found = await jobs_module.scan_existing_models(volume, cluster_id="cluster-1")

    assert found == {}


@pytest.mark.asyncio
async def test_scan_existing_models_returns_empty_when_no_pvc(monkeypatch):
    volume = _local_disk_volume(pvcName=None)
    found = await jobs_module.scan_existing_models(volume, cluster_id="cluster-1")
    assert found == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["download", "delete"])
async def test_job_launch_failure_keeps_pending_batch_and_propagates(monkeypatch, action):
    entry = _entry()
    applied = []

    async def fail_second(manifest, *, cluster_id):
        applied.append(manifest)
        if len(applied) == 2:
            raise ModelCacheJobError("second node failed")

    monkeypatch.setattr(jobs_module, "_apply_job", fail_second)
    with pytest.raises(ModelCacheJobError, match="second node failed"):
        await jobs_module.launch_cache_jobs(
            entry, volume=_nfs_volume(), nodes=["node-1", "node-2"], action=action,
            command_for_entry=lambda _: ["sh", "-c", "echo test"],
        )
    assert [p.status for p in entry.node_progress] == ["pending", "pending"]
    assert [m["metadata"]["labels"]["prism.ai/model-cache-action"] for m in applied] == [action, action]
