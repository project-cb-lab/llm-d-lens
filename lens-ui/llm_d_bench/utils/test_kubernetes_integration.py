"""Real SDK integration through old public/domain execution boundaries."""

from pathlib import Path

import pytest

from llm_d_bench.utils.kubernetes import scoped_runner
from llm_d_bench.utils.test_kubernetes_reads import cluster_api as cluster_api


@pytest.mark.asyncio
async def test_monitoring_recognizes_untyped_items_in_multi_resource_sdk_lists(cluster_api):
    import json

    from llm_d_bench.monitoring.cluster_stack.discovery import _component

    def response(request):
        if request.path.endswith("/pods"):
            return {
                "kind": "PodList",
                "apiVersion": "v1",
                "items": [
                    {
                        "metadata": {"name": "grafana"},
                        "status": {"containerStatuses": [{"name": "grafana", "ready": True}]},
                    }
                ],
            }
        return {"kind": "DeploymentList", "apiVersion": "apps/v1", "items": []}

    cluster_api.response = response
    result = await scoped_runner(None).run(["kubectl", "get", "pods,deployments", "-o", "json"])
    assert result.returncode == 0
    component = _component("grafana", json.loads(result.stdout)["items"])
    assert (component.status, component.ready, component.desired) == ("ready", 1, 1)


@pytest.mark.asyncio
async def test_storage_links_bound_volume_from_untyped_sdk_list_items(cluster_api):
    from llm_d_bench.storage.contracts import StorageVolume
    from llm_d_bench.storage.service import storage_resource_statuses

    volume = StorageVolume(
        id="storage-test",
        clusterId="a",
        name="cache",
        kind="local-disk",
        capacity="100Gi",
        readOnly=True,
        localDisk={"hostPath": "/data/models"},
    )

    def response(request):
        metadata = {"name": "cache", "labels": {"prism.ai/storage-volume-id": "storage-test"}}
        if request.path.endswith("/persistentvolumeclaims"):
            return {
                "kind": "PersistentVolumeClaimList",
                "apiVersion": "v1",
                "items": [
                    {
                        "metadata": {**metadata, "namespace": "llm-d-bench-storage"},
                        "spec": {"volumeName": "cache"},
                        "status": {"phase": "Bound"},
                    }
                ],
            }
        return {
            "kind": "PersistentVolumeList",
            "apiVersion": "v1",
            "items": [
                {
                    "metadata": metadata,
                    "status": {"phase": "Bound"},
                    "spec": {"persistentVolumeReclaimPolicy": "Retain", "hostPath": {"path": "/data/models"}},
                }
            ],
        }

    cluster_api.response = response
    result = await storage_resource_statuses([volume])
    pvc = result[volume.id]["persistentVolumeClaims"][0]
    assert pvc["persistentVolume"] is not None
    assert pvc["persistentVolume"]["phase"] == "Bound"
    assert pvc["persistentVolume"]["source"] == {"type": "hostPath", "detail": "/data/models"}


@pytest.mark.asyncio
async def test_scoped_runner_defaults_to_sdk_without_shell(cluster_api, monkeypatch):
    monkeypatch.delenv("PRISM_KUBERNETES_READ_BACKEND")
    runner = scoped_runner(None)
    result = await runner.run(["kubectl", "get", "nodes", "-o", "json"])
    assert result.returncode == 0 and "worker" in result.stdout
    cluster_api.response = {"kind": "Node", "metadata": {"name": "worker"}, "spec": {"unschedulable": True}}
    result = await runner.run(["kubectl", "cordon", "worker"])
    assert result.returncode == 0
    assert cluster_api.methods == ["GET", "PATCH"]
    assert cluster_api.calls[-1][0] == "/api/v1/nodes/worker"


@pytest.mark.asyncio
async def test_restricted_deployment_runner_keeps_policy_before_sdk(cluster_api, monkeypatch):
    monkeypatch.setenv("PRISM_KUBERNETES_BACKEND", "sdk")
    from llm_d_bench.deploy.runtime.composition import RestrictedKubectlRunner, RuntimeConfigurationError

    runner = RestrictedKubectlRunner(Path("/not-installed/kubectl"), "llm-d-bench-", kubeconfig=str(cluster_api.config))
    with pytest.raises(RuntimeConfigurationError):
        await runner(["kubectl", "delete", "namespace", "production", "--wait=false"])
    assert cluster_api.calls == []
    cluster_api.response = {"kind": "Namespace", "metadata": {"name": "llm-d-bench-test"}}
    code, _, _ = await runner(["kubectl", "create", "namespace", "llm-d-bench-test"])
    assert code == 0
    assert cluster_api.methods == ["POST"]
    assert runner.take_evidence_refs()


@pytest.mark.asyncio
async def test_evaluation_snapshot_uses_sdk_result_contract(cluster_api):
    from llm_d_bench.evaluate.router import _kubectl_json

    result = await _kubectl_json(["get", "nodes"], {"KUBECONFIG": str(cluster_api.config)})
    assert result["items"][0]["metadata"]["name"] == "worker"
    cluster_api.status = 403
    assert await _kubectl_json(["get", "nodes"], {"KUBECONFIG": str(cluster_api.config)}) is None


@pytest.mark.asyncio
async def test_public_scoped_runner_preserves_timeout_exception(cluster_api):
    from llm_d_bench.utils.kubernetes import KubernetesCommandRunner
    from llm_d_bench.utils.shell import CommandTimeoutError

    cluster_api.delay = 0.2
    runner = KubernetesCommandRunner({"KUBECONFIG": str(cluster_api.config)})
    with pytest.raises(CommandTimeoutError):
        await runner.run(["kubectl", "get", "nodes", "-o", "json"], timeout=0.05)


@pytest.mark.asyncio
async def test_legacy_read_switch_does_not_enable_sdk_writes(cluster_api, monkeypatch):
    from llm_d_bench.utils.kubernetes import KubernetesCommandRunner
    from llm_d_bench.utils.shell import CommandResult, ScopedCommandRunner

    async def cli_run(self, argv, **kwargs):
        return CommandResult(tuple(argv), 0, "legacy CLI write", "")

    monkeypatch.setattr(ScopedCommandRunner, "run", cli_run)
    runner = KubernetesCommandRunner({"KUBECONFIG": str(cluster_api.config)})
    result = await runner.run(["kubectl", "cordon", "worker"])
    assert result.stdout == "legacy CLI write"
    assert cluster_api.calls == []
