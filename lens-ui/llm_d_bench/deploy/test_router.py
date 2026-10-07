"""Tests for the public deployment query API."""

import asyncio
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from llm_d_bench.deploy.contracts import DeploymentCaseStatus, DeploymentStatus
from llm_d_bench.deploy.providers.hardware_profile import runtime_image
from llm_d_bench.deploy.standard_kubernetes_service import (
    StandardKubernetesServiceRequest,
    build_standard_kubernetes_service_configuration,
)
from llm_d_bench.storage.contracts import StorageVolumeKind, StorageVolumePurpose

router = importlib.import_module("llm_d_bench.deploy.router")
executions = importlib.import_module("llm_d_bench.deploy.executions")


def test_standard_kubernetes_service_configuration_is_an_immutable_baseline_artifact():
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-8B",
        deployment_name="qwen3-8b-20260904",
        cluster_session_id="a" * 36,
        replicas=2,
        tensor_parallel_size=4,
        storage_type="local-cache",
        model_cache_path="/home/user/.cache",
        max_model_len=8192,
        gpu_memory_utilization=0.85,
        max_num_seqs=128,
        max_num_batched_tokens=4096,
        enable_prefix_caching=False,
        enforce_eager=True,
        use_cluster_proxy=True,
        http_proxy="http://proxy.example:911",
        https_proxy="http://proxy.example:911",
        no_proxy="localhost,.svc",
    )

    configuration = build_standard_kubernetes_service_configuration(request)

    assert configuration.provider_ref == "baseline-vllm"
    assert configuration.type == "baseline"
    assert configuration.content["model"] == {"name": "Qwen/Qwen3-8B"}
    assert configuration.content["decode"] == {
        "replicaCount": 2,
        "tensorParallelSize": 4,
        "maxModelLen": 8192,
    }
    assert configuration.content["runtime"] == {
        "image": runtime_image(),
        "imageMode": "pull-existing",
        "modelSource": "auto-cache",
        "mountPath": "/home/user/.cache",
        "environment": {
            "HTTP_PROXY": "http://proxy.example:911",
            "HTTPS_PROXY": "http://proxy.example:911",
            "NO_PROXY": "localhost,.svc,127.0.0.1,.cluster.local",
        },
    }
    assert configuration.provenance["deployment_name"] == "qwen3-8b-20260904"
    assert {item["name"]: item["value"] for item in configuration.content["customParameters"]} == {
        "gpu-memory-utilization": "0.85",
        "enable-prefix-caching": "false",
        "max-num-seqs": "128",
        "max-num-batched-tokens": "4096",
        "enforce-eager": "true",
    }


def test_standard_kubernetes_service_run_keeps_the_requested_name_in_run_provenance(monkeypatch):
    captured = {}

    async def start_run(request, **_kwargs):
        captured["request"] = request
        return request

    monkeypatch.setattr(router.deployment_run_manager, "start_run", start_run)
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-0.6B",
        deployment_name="qwen3-standard",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
    )

    asyncio.run(router.create_standard_kubernetes_service_run(request))

    assert captured["request"].provenance["deployment_name"] == "qwen3-standard"


def test_baseline_vllm_renders_boolean_arguments_as_cli_flags():
    from llm_d_bench.deploy.providers.baseline_vllm import BaselineVllmAdapter

    resources = BaselineVllmAdapter._resources(
        {
            "model": "Qwen/Qwen3-0.6B",
            "image": "ghcr.io/llm-d/llm-d-xpu:v0.9.0",
            "replicas": 1,
            "tensor_parallel_size": 1,
            "max_model_len": 4096,
            "mount_path": "",
            "pvc_name": "",
            "model_source": "huggingface",
            "environment": {},
            "custom_parameters": [
                {"target": "decode", "kind": "argument", "name": "enable-auto-tool-choice", "value": "true"},
                {"target": "decode", "kind": "argument", "name": "enable-prefix-caching", "value": "true"},
                {"target": "decode", "kind": "argument", "name": "enforce-eager", "value": "false"},
                {"target": "decode", "kind": "argument", "name": "max-num-seqs", "value": "256"},
            ],
        }
    )

    args = resources[1]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--enable-auto-tool-choice" in args
    assert "--enable-prefix-caching" in args
    assert "--no-enforce-eager" in args
    assert "--max-num-seqs=256" in args
    assert not any(argument.startswith("--enable-auto-tool-choice=") for argument in args)
    assert not any(argument.startswith("--enable-prefix-caching=") for argument in args)


def test_baseline_vllm_accepts_the_standard_model_market_namespace():
    from llm_d_bench.deploy.providers.baseline_vllm import BaselineVllmAdapter

    adapter = BaselineVllmAdapter(None, "llmd-", 900, None)

    assert adapter._namespace({"namespace": "standard-4-1-20260904081451502388"}) == "standard-4-1-20260904081451502388"


def test_standard_kubernetes_service_configuration_uses_named_pvc_for_model_cache():
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-0.6B",
        deployment_name="qwen3-pvc",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
        storage_type="pvc",
        pvc_name="shared-model-cache",
    )

    configuration = build_standard_kubernetes_service_configuration(request)

    assert configuration.content["runtime"]["pvcName"] == "shared-model-cache"
    assert "mountPath" not in configuration.content["runtime"]


def test_standard_kubernetes_service_configuration_uses_registered_model_cache_storage():
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-0.6B",
        deployment_name="qwen3-model-cache",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
        storage_type="model-cache",
        storage_volume_id="storage-model-cache",
    )

    configuration = build_standard_kubernetes_service_configuration(request)

    assert configuration.content["runtime"]["storageVolumeId"] == "storage-model-cache"
    assert "mountPath" not in configuration.content["runtime"]
    assert "pvcName" not in configuration.content["runtime"]


def test_ready_model_cache_entry_exists_requires_matching_ready_huggingface_model():
    ready_entry = SimpleNamespace(
        status="ready",
        source=SimpleNamespace(huggingface=SimpleNamespace(repo_id="Qwen/Qwen3-0.6B")),
    )
    downloading_entry = SimpleNamespace(
        status="downloading",
        source=SimpleNamespace(huggingface=SimpleNamespace(repo_id="Qwen/Qwen3-0.6B")),
    )

    assert router._ready_model_cache_entry_exists([downloading_entry, ready_entry], "qwen/qwen3-0.6b")


def test_ready_model_cache_entry_exists_rejects_missing_model():
    ready_entry = SimpleNamespace(
        status="ready",
        source=SimpleNamespace(huggingface=SimpleNamespace(repo_id="TheBloke/Mistral-7B-Instruct-v0.2-GGUF")),
    )

    assert not router._ready_model_cache_entry_exists([ready_entry], "Qwen/Qwen3-0.6B")


def test_standard_kubernetes_service_run_rejects_dynamic_pvc_model_cache_storage(monkeypatch):
    import llm_d_bench.cluster.sessions as cluster_sessions
    import llm_d_bench.storage.service as storage_service

    monkeypatch.setattr(
        cluster_sessions,
        "require_active_session",
        lambda _session_id: SimpleNamespace(server_id="cluster-1"),
    )

    async def get_ready_volume(_volume_id, *, cluster_id):
        assert cluster_id == "cluster-1"
        return SimpleNamespace(
            kind=StorageVolumeKind.DYNAMIC_PVC,
            purposes=[StorageVolumePurpose.MODEL_CACHE],
        )

    monkeypatch.setattr(storage_service, "get_ready_volume", get_ready_volume)
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-0.6B",
        deployment_name="qwen3-model-cache",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
        storage_type="model-cache",
        storage_volume_id="storage-dynamic-pvc",
    )

    with pytest.raises(HTTPException) as captured:
        asyncio.run(router.create_standard_kubernetes_service_run(request))

    assert captured.value.status_code == 422


def test_get_execution_pods_returns_pods(monkeypatch):
    mock_context = SimpleNamespace(
        execution_id="exec-123",
        namespace="prism-exec-123",
        cluster_id="cluster-1",
        cluster_session_id=None,
    )
    monkeypatch.setattr(router, "find_execution_context", lambda _id: mock_context)

    k8s_pods_json = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "pod-1",
                        "namespace": "prism-exec-123",
                        "creationTimestamp": "2026-09-06T00:00:00Z",
                    },
                    "status": {
                        "phase": "Running",
                        "podIP": "10.244.0.5",
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "containerStatuses": [
                            {
                                "name": "vllm",
                                "image": "vllm:latest",
                                "ready": True,
                                "restartCount": 0,
                                "state": {"running": {}},
                            }
                        ],
                    },
                    "spec": {"nodeName": "worker-1"},
                }
            ]
        }
    )

    async def mock_run_kubectl(cmd, cluster_id, timeout):
        return SimpleNamespace(returncode=0, stdout=k8s_pods_json, stderr="")

    import llm_d_bench.utils.kubernetes as k8s

    monkeypatch.setattr(k8s, "run_kubectl", mock_run_kubectl)

    res = asyncio.run(router.get_execution_pods("exec-123"))
    assert res["execution_id"] == "exec-123"
    assert res["namespace"] == "prism-exec-123"
    assert len(res["pods"]) == 1
    assert res["pods"][0]["name"] == "pod-1"
    assert res["pods"][0]["phase"] == "Running"
    assert res["pods"][0]["ready"] is True
    assert res["pods"][0]["pod_ip"] == "10.244.0.5"


def test_get_execution_pod_logs_returns_logs(monkeypatch):
    mock_context = SimpleNamespace(
        execution_id="exec-123",
        namespace="prism-exec-123",
        cluster_id="cluster-1",
        cluster_session_id=None,
    )
    monkeypatch.setattr(router, "find_execution_context", lambda _id: mock_context)

    async def mock_run_kubectl(cmd, cluster_id, timeout):
        assert "logs" in cmd
        assert "pod-1" in cmd
        return SimpleNamespace(returncode=0, stdout="[INFO] Server started\n", stderr="")

    import llm_d_bench.utils.kubernetes as k8s

    monkeypatch.setattr(k8s, "run_kubectl", mock_run_kubectl)

    res = asyncio.run(router.get_execution_pod_logs("exec-123", "pod-1", tail=100))
    assert res["execution_id"] == "exec-123"
    assert res["pod_name"] == "pod-1"
    assert res["logs"] == "[INFO] Server started"
    assert res["success"] is True


def test_standard_kubernetes_service_configuration_accepts_empty_pvc_name_for_local_cache():
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-0.6B",
        deployment_name="qwen3-local-cache",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
        storage_type="local-cache",
        pvc_name="",
    )

    configuration = build_standard_kubernetes_service_configuration(request)

    assert configuration.content["runtime"]["mountPath"] == str(Path.home() / ".cache")


def test_baseline_vllm_renders_model_cache_pvc():
    from llm_d_bench.deploy.providers.baseline_vllm import BaselineVllmAdapter

    resources = BaselineVllmAdapter._resources(
        {
            "model": "Qwen/Qwen3-0.6B",
            "image": "ghcr.io/llm-d/llm-d-xpu:v0.9.0",
            "replicas": 1,
            "tensor_parallel_size": 1,
            "max_model_len": 4096,
            "mount_path": "",
            "pvc_name": "shared-model-cache",
            "model_source": "auto-cache",
            "environment": {},
            "custom_parameters": [],
        }
    )

    pod_spec = resources[1]["spec"]["template"]["spec"]
    assert pod_spec["volumes"] == [
        {"name": "model-cache", "persistentVolumeClaim": {"claimName": "shared-model-cache"}}
    ]


def test_standard_kubernetes_service_configuration_inherits_backend_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:911")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:911")
    monkeypatch.setenv("NO_PROXY", "internal.example")
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-0.6B",
        deployment_name="qwen3-0.6b-20260904",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
    )

    configuration = build_standard_kubernetes_service_configuration(request)

    assert configuration.content["runtime"]["environment"] == {
        "HTTP_PROXY": "http://proxy.example:911",
        "HTTPS_PROXY": "http://proxy.example:911",
        "NO_PROXY": "internal.example,localhost,127.0.0.1,.svc,.cluster.local",
    }


@pytest.mark.asyncio
async def test_deployment_run_scheduling_is_idempotent():
    from llm_d_bench.deploy.application import DeploymentRunManager

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class Worker:
        async def execute_run(self, run_id):
            nonlocal calls
            assert run_id == "run-1"
            calls += 1
            started.set()
            await release.wait()

    manager = DeploymentRunManager(store=SimpleNamespace())
    worker = Worker()
    manager._schedule_run("run-1", worker)
    manager._schedule_run("run-1", worker)
    await started.wait()
    assert calls == 1
    release.set()
    await manager._run_tasks["run-1"]


def test_cluster_deployment_source_uses_registered_checkout(monkeypatch, tmp_path):
    from llm_d_bench.deploy import application

    checkout = tmp_path / "llm-d"
    (checkout / "guides").mkdir(parents=True)
    monkeypatch.setattr(
        application.cluster_registry,
        "require_cluster",
        lambda cluster_id: SimpleNamespace(
            id=cluster_id, name="test-cluster", llm_d_repo_path=str(checkout), llm_d_ref="v1.2.3"
        ),
    )

    source = application.cluster_deployment_source(
        SimpleNamespace(server_id="cluster-1"),
        {"kind": "evaluation", "repository": "https://example.com/ignored.git", "branch": "main"},
    )

    assert source == {
        "kind": "evaluation",
        "resolved_repository": str(checkout),
        "ref": "v1.2.3",
        "resolved_from": "cluster-registry",
    }


def test_worker_uses_registered_checkout_for_legacy_cluster_bound_run(monkeypatch):
    from llm_d_bench.deploy import application

    manager = application.DeploymentRunManager(store=SimpleNamespace())
    captured = {}
    monkeypatch.setattr(
        application,
        "cluster_deployment_source",
        lambda session, source: {"resolved_repository": "/checkout/llm-d"},
    )
    monkeypatch.setattr(application, "deployment_runtime_overrides", lambda _session_id: {})
    monkeypatch.setattr(
        manager,
        "_build_worker",
        lambda environment, lifecycle_error=None: captured.update(environment=environment) or SimpleNamespace(),
    )
    # This legacy cluster has no saved proxy config; keep the environment limited
    # to what the run itself supplies.
    monkeypatch.setattr("llm_d_bench.cluster.service.resolve_proxy_env", lambda _cluster_id: {})

    manager._worker(
        SimpleNamespace(
            provenance={
                "cluster_server_id": "cluster-1",
                "cluster_session_id": "session-1",
                "deployment_source": {"kind": "agentic-deployment"},
            }
        )
    )

    assert captured["environment"] == {"LLM_D_ROOT": "/checkout/llm-d"}


def _execution(execution_id: str, status: DeploymentStatus, metadata=None):
    return SimpleNamespace(
        execution_id=execution_id,
        status=status,
        endpoint=(
            SimpleNamespace(url=f"http://{execution_id}.namespace.svc:8000")
            if status == DeploymentStatus.READY
            else None
        ),
        forwarded_endpoint=None,
        namespace="namespace",
        artifact=SimpleNamespace(manifest_ref="vllm/manifest.yaml"),
        provenance={
            "cluster_server_id": "cluster-1",
            "cluster_session_id": "session-1",
            "preserve_deployment": True,
        },
        metadata=metadata,
        created_at=datetime.now(UTC),
        updated_at=None,
    )


def _case(execution_id: str):
    return SimpleNamespace(
        id=f"case-{execution_id}",
        run_id="run-1",
        execution_id=execution_id,
        provider_ref="optimized-baseline",
        status=DeploymentCaseStatus.READY,
        create_request=SimpleNamespace(
            configuration_artifacts=[SimpleNamespace(content='{"model":{"name":"Qwen"}}')],
            deployment_policy=SimpleNamespace(value={"deployment_name": "qwen"}),
        ),
    )


@pytest.mark.asyncio
async def test_list_ready_executions_is_side_effect_free_and_filtered(monkeypatch):
    ready = _execution("execution-ready", DeploymentStatus.READY)
    failed = _execution("execution-failed", DeploymentStatus.FAILED)
    run = SimpleNamespace(
        id="run-1",
        provenance={"cluster_server_id": "cluster-1"},
        cases=[_case(ready.execution_id), _case(failed.execution_id)],
        created_at=datetime.now(UTC),
    )
    store = {ready.execution_id: ready, failed.execution_id: failed}
    monkeypatch.setattr(
        executions,
        "_store",
        SimpleNamespace(list_runs=lambda: [run], get_execution=store.get),
    )

    response = await router.list_executions(
        status=DeploymentStatus.READY,
        cluster_id="cluster-1",
    )

    assert len(response["items"]) == 1
    assert response["items"][0] == {
        "execution_id": "execution-ready",
        "status": "ready",
        "cluster_id": "cluster-1",
        "cluster_session_id": "session-1",
        "namespace": "namespace",
        "name": "qwen",
        "display_name": "",
        "description": "",
        "model": "Qwen",
        "backend": "vLLM",
        "guide": "optimized-baseline",
        "endpoint": "http://execution-ready.namespace.svc:8000",
        "forwarded_endpoint": None,
        "replicas": None,
        "tensor_parallel_size": None,
        "image": None,
        "preserve_deployment": True,
        "created_at": ready.created_at.isoformat(),
        "updated_at": None,
        "max_model_len": None,
        "gpu_memory_utilization": None,
        "enable_prefix_caching": None,
        "max_num_seqs": None,
        "max_num_batched_tokens": None,
        "storage_type": None,
        "storage_volume_id": None,
        "mount_path": None,
        "pvc_name": None,
        "failure": None,
        "custom_parameters": [],
        "baseline_endpoint": None,
        "service_ref": None,
    }


@pytest.mark.asyncio
async def test_execution_payload_omits_run_and_case_identifiers(monkeypatch):
    """run_id/case_id stay inside Deploy and must not leak to API consumers."""
    ready = _execution("execution-ready", DeploymentStatus.READY)
    run = SimpleNamespace(
        id="run-1",
        provenance={"cluster_server_id": "cluster-1"},
        cases=[_case(ready.execution_id)],
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        executions,
        "_store",
        SimpleNamespace(list_runs=lambda: [run], get_execution=lambda _id: ready),
    )

    payload = await router.get_execution("execution-ready")

    assert payload["execution_id"] == "execution-ready"
    assert "run_id" not in payload
    assert "case_id" not in payload


@pytest.mark.asyncio
async def test_get_execution_reports_missing_execution(monkeypatch):
    monkeypatch.setattr(
        executions,
        "_store",
        SimpleNamespace(list_runs=lambda: [], get_execution=lambda _id: None),
    )

    with pytest.raises(HTTPException) as error:
        await router.get_execution("absent")

    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_list_executions_rejects_other_cluster(monkeypatch):
    ready = _execution("execution-ready", DeploymentStatus.READY)
    run = SimpleNamespace(
        id="run-1",
        provenance={"cluster_server_id": "cluster-1"},
        cases=[_case(ready.execution_id)],
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        executions,
        "_store",
        SimpleNamespace(list_runs=lambda: [run], get_execution=lambda _id: ready),
    )

    response = await router.list_executions(cluster_id="cluster-2")

    assert response == {"items": []}


def test_execution_readable_honors_a_direct_execution_share():
    from uuid import uuid4

    from llm_d_bench.auth.records import RoleRecord, UserRecord, UserRoleBindingRecord
    from llm_d_bench.auth.service import default_service
    from llm_d_bench.db.dao.cluster import ClusterDao
    from llm_d_bench.db.dao.role import RoleDao, RolePermissionDao, UserRoleBindingDao
    from llm_d_bench.db.dao.user import UserDao

    suffix = uuid4().hex[:6]
    cluster_id = ClusterDao().create(f"dep-{suffix}", "", "apiVersion: v1\nkind: Config").id
    base_role = RoleDao().create(RoleRecord(name=f"base-{suffix}"))
    RolePermissionDao().set_for_role(base_role.id, {"cluster:cluster:read"})
    share_role = RoleDao().create(RoleRecord(name=f"share-{suffix}"))
    RolePermissionDao().set_for_role(share_role.id, {"deployment:run:read"})
    user = UserDao().create(UserRecord(username=f"dep-{suffix}"))
    UserRoleBindingDao().create(
        UserRoleBindingRecord(user_id=user.id, role_id=base_role.id, scope_type="cluster", scope_cluster_id=cluster_id)
    )
    service = default_service()
    service.share_resource(
        subject_type="user",
        subject_id=user.id,
        role_id=share_role.id,
        resource_type="deployment_execution",
        resource_id="exec-x",
        cluster_id=cluster_id,
    )
    principal = service.principal_for(user)
    shared = SimpleNamespace(run_id="run-x", execution_id="exec-x", cluster_id=cluster_id)
    assert router._execution_readable(principal, shared, None)
    unshared = SimpleNamespace(run_id="run-y", execution_id="exec-y", cluster_id=cluster_id)
    assert not router._execution_readable(principal, unshared, None)
