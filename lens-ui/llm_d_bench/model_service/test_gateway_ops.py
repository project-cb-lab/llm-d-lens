"""Tests for model-service gateway operations (shared Gateway, model routes)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm_d_bench.db.dao.model_service_gateway_operation import GatewayOperationDao
from llm_d_bench.db.dao.model_service_group import ModelServiceGroupDao
from llm_d_bench.db.dao.model_service_member import ModelServiceMemberDao
from llm_d_bench.model_service import gateway_ops as gateway_ops_module
from llm_d_bench.model_service.contracts import ModelServiceGroup, ModelServiceMember
from llm_d_bench.model_service.gateway_ops import GatewayOpsService
from llm_d_bench.model_service.targets import ExecutionMissingError


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRunner:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if any("infrastructure.parametersRef.name" in str(arg) for arg in argv):
            return _Result(stdout="")
        if list(argv[:3]) == ["kubectl", "get", "envoyproxy"]:
            return _Result(stdout="")
        if list(argv[:3]) == ["kubectl", "get", "httproute"]:
            return _Result(stdout="")
        if list(argv[:4]) == ["kubectl", "get", "gateway", "lens-inference-gateway"]:
            return _Result(stdout="True")
        if list(argv[:3]) == ["kubectl", "get", "gateway"]:
            return _Result(stdout="True")
        return _Result(stdout="applied")


class _ExistingRouteRunner(_FakeRunner):
    """Replays one Lens-managed HTTPRoute already on the cluster."""

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "httproute"]:
            return _Result(stdout="qwen-empty")
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


class _ExistingServiceTypeRunner(_FakeRunner):
    """Replays a Gateway already bound to a NodePort EnvoyProxy."""

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if any("infrastructure.parametersRef.name" in str(arg) for arg in argv):
            return _Result(stdout="lens-inference-gateway-proxy")
        if any("envoyService.type" in str(arg) for arg in argv):
            return _Result(stdout="NodePort")
        return _Result(stdout="True")


class _BusyPortRunner(_FakeRunner):
    """Gateway service plus another Service already using a nodePort."""

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        if list(argv[:3]) == ["kubectl", "get", "svc"] and "-l" in argv:
            self.calls.append((argv, input))
            return _Result(stdout="gw-svc")
        if list(argv[:3]) == ["kubectl", "get", "svc"] and "-A" in argv:
            self.calls.append((argv, input))
            return _Result(
                stdout='{"items":[{"metadata":{"namespace":"other","name":"other-svc"},'
                '"spec":{"ports":[{"nodePort":30999}]}}]}'
            )
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


def _seed_member(cluster_id: str = "cluster-a") -> ModelServiceMember:
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name=f"qwen3-8b-{cluster_id}",
            model_ref="Qwen/Qwen3-8B",
            cluster_id=cluster_id,
            base_model="Qwen/Qwen3-8B",
        )
    )
    return ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id=f"exec-{cluster_id}",
            cluster_id=cluster_id,
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="qwen",
            endpoint_kind="llm-d-epp",
        )
    )


def _service(*, calls: list, runner_cls=_FakeRunner) -> GatewayOpsService:
    return GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
        runner_factory=lambda _cluster_id: runner_cls(calls),
    )


@pytest.mark.asyncio
async def test_preflight_missing_cluster(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: None)
    operation = await service.preflight("cluster-a")
    assert operation.status == "failed"
    assert "not found" in operation.message


@pytest.mark.asyncio
async def test_install_inference_gateway_applies_shared_gateway(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: object())

    operation = await service.install_inference_gateway("cluster-a", "istio", install_prerequisites=False)
    assert operation.status == "succeeded"
    assert operation.detail_json["provider"] == "istio"
    applies = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]]
    assert applies
    manifest = applies[-1][1]
    assert "kind: Gateway" in manifest
    assert "controllerName: istio.io/gateway-controller" in manifest
    assert "kind: HTTPRoute" not in manifest


@pytest.mark.asyncio
async def test_install_preserves_existing_envoy_service_type(monkeypatch):
    calls: list = []
    service = _service(calls=calls, runner_cls=_ExistingServiceTypeRunner)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: object())

    operation = await service.install_inference_gateway("cluster-a", "envoy-ai-gateway", install_prerequisites=False)
    assert operation.status == "succeeded"
    manifest = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]][-1][1]
    assert "kind: EnvoyProxy" in manifest
    assert "type: NodePort" in manifest


@pytest.mark.asyncio
async def test_reconcile_routes_a_single_pool_per_service(monkeypatch):
    import yaml

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="qwen3-8b", model_ref="Qwen/Qwen3-8B", cluster_id="cluster-a", base_model="Qwen/Qwen3-8B"
        )
    )
    for index, (service_name, namespace) in enumerate(
        [("optimized-baseline-epp", "ns-a"), ("optimized-baseline-b-epp", "ns-b")]
    ):
        ModelServiceMemberDao().create(
            ModelServiceMember(
                group_id=group.id,
                execution_id=f"exec-{index}",
                cluster_id="cluster-a",
                target_namespace=namespace,
                target_service=service_name,
                target_port=80,
                epp_ref=service_name,
                endpoint_kind="llm-d-epp",
            )
        )
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "succeeded"
    assert operation.detail_json["routes"] == 1
    manifest = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]][-1][1]
    route = next(doc for doc in yaml.safe_load_all(manifest) if doc and doc["kind"] == "HTTPRoute")
    assert len(route["spec"]["rules"][0]["backendRefs"]) == 1


@pytest.mark.asyncio
async def test_probe_prunes_member_whose_execution_is_gone():
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="orphan-model", model_ref="Qwen/Qwen3-8B", cluster_id="cluster-a", base_model="Qwen/Qwen3-8B"
        )
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-gone",
            cluster_id="cluster-a",
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=80,
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )

    def resolver(_execution_id: str):
        raise ExecutionMissingError("execution not found")

    calls: list = []
    service = GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
        runner_factory=lambda _cluster_id: _FakeRunner(calls),
        target_resolver=resolver,
    )
    await service.probe_members()

    assert ModelServiceMemberDao().get(member.id) is None


@pytest.mark.asyncio
async def test_reconcile_removes_route_for_memberless_group(monkeypatch):
    calls: list = []
    service = _service(calls=calls, runner_cls=_ExistingRouteRunner)
    cluster = SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "succeeded"
    assert operation.detail_json["removed"] == 1
    deletes = [c for c in calls if list(c[0][:3]) == ["kubectl", "delete", "httproute"]]
    assert deletes and deletes[0][0][3] == "qwen-empty"


@pytest.mark.asyncio
async def test_install_rejects_exposed_port_out_of_range(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_provider="envoy-ai-gateway",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_port=80,
        ),
    )
    operation = await service.install_inference_gateway("cluster-a", "envoy-ai-gateway", install_prerequisites=False)
    assert operation.status == "failed"
    assert "30000" in operation.message


@pytest.mark.asyncio
async def test_install_rejects_busy_exposed_port(monkeypatch):
    calls: list = []
    service = _service(calls=calls, runner_cls=_BusyPortRunner)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_provider="envoy-ai-gateway",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_port=30999,
        ),
    )
    operation = await service.install_inference_gateway("cluster-a", "envoy-ai-gateway", install_prerequisites=False)
    assert operation.status == "failed"
    assert "already in use" in operation.message


@pytest.mark.asyncio
async def test_install_falls_back_to_nodeport_without_loadbalancer(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: object())
    waits = [False, True]

    async def fake_wait(*_args, **_kwargs):
        return waits.pop(0)

    async def fake_unassigned(*_args, **_kwargs):
        return True

    async def fake_existing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_wait_gateway_programmed", fake_wait)
    monkeypatch.setattr(service, "_gateway_address_unassigned", fake_unassigned)
    monkeypatch.setattr(service, "_existing_gateway_service_type", fake_existing)

    operation = await service.install_inference_gateway("cluster-a", "envoy-ai-gateway", install_prerequisites=False)
    assert operation.status == "succeeded"
    assert operation.detail_json["programmed"] is True
    assert operation.detail_json["serviceType"] == "NodePort"
    manifests = [c[1] for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]]
    assert "kind: EnvoyProxy" in manifests[-1]


@pytest.mark.asyncio
async def test_scale_gateway_scales_data_plane_deployment(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_provider="istio",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_port=None,
        ),
    )
    operation = await service.scale_gateway("cluster-a", 0)
    assert operation.status == "succeeded"
    scales = [c for c in calls if list(c[0][:2]) == ["kubectl", "scale"]]
    assert scales and scales[0][0][2] == "deployment/lens-inference-gateway-istio"


@pytest.mark.asyncio
async def test_resolve_log_target_for_ipp(monkeypatch):
    service = _service(calls=[])
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_provider="istio",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_port=None,
        ),
    )
    namespace, name = await service.resolve_log_target("cluster-a", component="ipp", namespace=None, name=None)
    assert (namespace, name) == ("lens-gateway", "lens-ipp")


@pytest.mark.asyncio
async def test_install_inference_gateway_rejects_unknown_provider(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: object())

    operation = await service.install_inference_gateway("cluster-a", "traefik")
    assert operation.status == "failed"
    assert "unsupported gateway provider" in operation.message


@pytest.mark.asyncio
async def test_reconcile_cluster_gateway_applies_model_routes(monkeypatch):
    _seed_member(cluster_id="cluster-a")
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "succeeded"
    assert operation.detail_json["routes"] == 1
    applies = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]]
    route_manifest = applies[-1][1]
    assert "kind: HTTPRoute" in route_manifest
    assert "kind: ConfigMap" in route_manifest
    assert "name: qwen" in route_manifest
    assert "X-Gateway-Base-Model-Name" in route_manifest


@pytest.mark.asyncio
async def test_reconcile_derives_pool_name_for_legacy_member(monkeypatch):
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="legacy-model",
            model_ref="Qwen/Qwen3-0.6B",
            cluster_id="cluster-a",
            base_model="Qwen/Qwen3-0.6B",
        )
    )
    ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-legacy",
            cluster_id="cluster-a",
            target_namespace="ns",
            target_service="optimized-baseline-epp",
            target_port=8000,
            epp_ref="optimized-baseline-epp",
            endpoint_kind="llm-d-epp",
        )
    )
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "succeeded"
    assert operation.detail_json["routes"] == 1
    route_manifest = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]][-1][1]
    assert "name: optimized-baseline" in route_manifest


@pytest.mark.asyncio
async def test_status_reports_gateway_state(monkeypatch):
    _seed_member(cluster_id="cluster-a")
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        name="Cluster A",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(
        "llm_d_bench.cluster.registry.list_clusters", lambda: [SimpleNamespace(id="cluster-a", name="Cluster A")]
    )

    status = await service.status()
    assert len(status.clusters) == 1
    cluster_status = status.clusters[0]
    assert cluster_status.gateway_provider == "istio"
    assert cluster_status.gateway_state == "installed"
    assert cluster_status.gateway_ready is True
    assert cluster_status.members[0].pool_name == "qwen"
    # A gateway operation is not created merely by reading status.
    assert cluster_status.last_operation is None


class _DriftedProviderRunner(_FakeRunner):
    """Gateway is istio-classed, but no provider-specific Service lookup finds it
    (provider mislabeled as envoy-ai-gateway, and the `-istio` Service is absent)."""

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "svc"] and "-l" in argv:
            return _Result(stdout="")  # envoy labelled lookup: not found
        if list(argv[:3]) == ["kubectl", "get", "svc"] and argv[3].endswith("-istio"):
            return _Result(returncode=1, stderr="not found")
        if any("status.addresses[0].value" in str(arg) for arg in argv):
            return _Result(stdout="lens-inference-gateway-istio.lens-gateway.svc.cluster.local")
        if any("spec.gatewayClassName" in str(arg) for arg in argv):
            return _Result(stdout="istio")
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


@pytest.mark.asyncio
async def test_gateway_service_falls_back_to_discovery_on_provider_drift():
    """Regression: a wrong stored provider must not skip the public exposure."""
    calls: list = []
    service = _service(calls=calls, runner_cls=_DriftedProviderRunner)
    target = await service._gateway_service("cluster-a", "lens-gateway", "lens-inference-gateway", "envoy-ai-gateway")
    assert target == ("lens-gateway", "lens-inference-gateway-istio")


@pytest.mark.asyncio
async def test_actual_gateway_provider_maps_gateway_class():
    calls: list = []
    service = _service(calls=calls, runner_cls=_DriftedProviderRunner)
    assert await service._actual_gateway_provider("cluster-a", "lens-gateway", "lens-inference-gateway") == "istio"


@pytest.mark.asyncio
async def test_gateway_service_falls_back_when_istio_service_is_absent():
    """Regression: provider says istio but no such Service -> discover the real one."""
    calls: list = []
    service = _service(calls=calls, runner_cls=_DriftedProviderRunner)
    target = await service._gateway_service("cluster-a", "lens-gateway", "lens-inference-gateway", "istio")
    assert target == ("lens-gateway", "lens-inference-gateway-istio")


class _IppConfigRunner(_FakeRunner):
    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "configmap"]:
            return _Result(
                stdout=(
                    '{"custom-ipp-config.yaml":"plugins: []\\n",'
                    '"default-ipp-config.yaml":"plugins:\\n- type: body-field-to-header\\n"}'
                )
            )
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


@pytest.mark.asyncio
async def test_get_ipp_config_reads_live_configmap(monkeypatch):
    """IPP config is read live from the cluster ConfigMap (not Lens storage)."""
    calls: list = []
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway"),
    )
    service = _service(calls=calls, runner_cls=_IppConfigRunner)
    cfg = await service.get_ipp_config("cluster-a")
    assert cfg["custom"] is True
    assert cfg["config"] == "plugins: []\n"
    assert "body-field-to-header" in cfg["default"]


@pytest.mark.asyncio
async def test_get_ipp_config_falls_back_to_default_when_no_custom(monkeypatch):
    calls: list = []

    class R(_IppConfigRunner):
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                return _Result(stdout='{"default-ipp-config.yaml":"plugins:\\n- type: x\\n"}')
            return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)

    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway"),
    )
    service = _service(calls=calls, runner_cls=R)
    cfg = await service.get_ipp_config("cluster-a")
    assert cfg["custom"] is False
    assert cfg["config"] == cfg["default"]


class _TlsEppRunner(_FakeRunner):
    """An EPP Deployment left on the default secure-serving=true (TLS)."""

    def __init__(self, calls):
        super().__init__(calls)
        self.patched = None

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "deploy"] and "-o" in argv and "json" in argv:
            return _Result(
                stdout=(
                    '{"spec":{"template":{"spec":{"containers":['
                    '{"name":"envoy-proxy","args":["--log-level","warn"]},'
                    '{"name":"epp","args":["--pool-name","qwen","--secure-serving=true"]}'
                    "]}}}}"
                )
            )
        if list(argv[:3]) == ["kubectl", "patch", "deploy"]:
            self.patched = argv
            return _Result(stdout="patched")
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


@pytest.mark.asyncio
async def test_ensure_epp_plaintext_rewrites_secure_serving():
    """Regression: an EPP on TLS (secure-serving=true) must be switched to plaintext."""
    calls: list = []
    runner = _TlsEppRunner(calls)
    service = GatewayOpsService(runner_factory=lambda _cid: runner)
    changed = await service._ensure_epp_plaintext("cluster-a", "ns", "qwen-epp")
    assert changed is True
    assert runner.patched is not None
    assert "--secure-serving=false" in runner.patched[-1]
    assert "/spec/template/spec/containers/1/args/2" in runner.patched[-1]


@pytest.mark.asyncio
async def test_ensure_epp_plaintext_is_noop_when_already_plaintext():
    calls: list = []

    class R(_FakeRunner):
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout=(
                        '{"spec":{"template":{"spec":{"containers":['
                        '{"name":"epp","args":["--pool-name","qwen","--secure-serving=false"]}'
                        "]}}}}"
                    )
                )
            if list(argv[:3]) == ["kubectl", "patch", "deploy"]:
                raise AssertionError("must not patch an already-plaintext EPP")
            return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)

    service = GatewayOpsService(runner_factory=lambda _cid: R(calls))
    assert await service._ensure_epp_plaintext("cluster-a", "ns", "qwen-epp") is False


@pytest.mark.asyncio
async def test_install_ipp_restarts_rollout_to_apply_config(monkeypatch):
    """A config change forces a restart so it is loaded even if inotify fails."""
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        ipp_version=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.install_ipp("cluster-a", config="plugins: []")

    assert operation.status == "succeeded"
    assert any(list(call[0][:3]) == ["kubectl", "rollout", "restart"] for call in calls)


@pytest.mark.asyncio
async def test_apply_node_inotify_limit_applies_daemonset(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        ipp_version=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.apply_node_inotify_limit("cluster-a", 8192)

    assert operation.status == "succeeded"
    apply_calls = [call for call in calls if list(call[0][:3]) == ["kubectl", "apply", "-f"]]
    assert apply_calls
    manifest = apply_calls[-1][1]
    assert "kind: DaemonSet" in manifest
    assert "fs.inotify.max_user_instances=8192" in manifest


@pytest.mark.asyncio
async def test_apply_node_inotify_limit_rejects_zero(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        ipp_version=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.apply_node_inotify_limit("cluster-a", 0)
    assert operation.status == "failed"


class _ComponentRunner(_FakeRunner):
    """Canned HTTPRoute/InferencePool/Service/EndpointSlice reads."""

    def __init__(self, calls: list, *, model_server_ready: bool = True) -> None:
        super().__init__(calls)
        self._model_server_ready = model_server_ready

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "endpoints"]:
            return _Result(stdout='{"subsets":[{"addresses":[{"ip":"1.2.3.4"}]}]}')
        if list(argv[:3]) == ["kubectl", "get", "httproute"]:
            return _Result(stdout='{"status":{"parents":[{"conditions":[{"type":"Accepted","status":"True"}]}]}}')
        if list(argv[:3]) == ["kubectl", "get", "inferencepool"]:
            return _Result(
                stdout='{"spec":{"selector":{"matchLabels":{"llm-d.ai/guide":"pool"}}},'
                '"status":{"parents":[{"conditions":[{"type":"Accepted","status":"True"}]}]}}'
            )
        if list(argv[:3]) == ["kubectl", "get", "endpointslices"]:
            ready = "true" if self._model_server_ready else "false"
            return _Result(
                stdout='{"items":[{"endpoints":[{"addresses":["1.2.3.4"],"conditions":{"ready":' + ready + "}}]}]}"
            )
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


def _seed_component_member(name: str) -> ModelServiceMember:
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name=name, model_ref=name, cluster_id="cluster-a", base_model=name)
    )
    return ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id=f"exec-{name}",
            cluster_id="cluster-a",
            target_namespace="ns",
            target_service="pool-epp",
            target_port=80,
            pool_name="ns/pool",
            epp_ref="pool-epp",
            endpoint_kind="llm-d-epp",
        )
    )


@pytest.mark.asyncio
async def test_probe_members_reports_per_component_health(monkeypatch):
    member = _seed_component_member("percomp-ready")
    calls: list = []
    service = GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
        runner_factory=lambda _cid: _ComponentRunner(calls),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"),
    )
    await service.probe_members()

    saved = ModelServiceMemberDao().get(member.id)
    assert saved.status == "active"
    assert saved.health_json["components"] == {
        "epp": "ready",
        "httpRoute": "ready",
        "inferencePool": "ready",
        "modelServer": "ready",
        "ipp": "ready",
        "serving": "n/a",
    }


def test_data_plane_degraded_detects_broken_paths():
    assert gateway_ops_module._data_plane_degraded({"ipp": "missing"})
    assert gateway_ops_module._data_plane_degraded({"serving": "degraded"})
    assert gateway_ops_module._data_plane_degraded({"serving": "unreachable"})
    assert gateway_ops_module._data_plane_degraded({"httpRoute": "missing"})
    assert gateway_ops_module._data_plane_degraded({"inferencePool": "degraded"})
    assert not gateway_ops_module._data_plane_degraded({"epp": "ready", "ipp": "ready", "serving": "ready"})
    # A model server still starting is not a data-plane failure.
    assert not gateway_ops_module._data_plane_degraded({"modelServer": "missing"})


@pytest.mark.asyncio
async def test_heal_data_plane_uses_no_actor_so_the_operation_can_be_recorded(monkeypatch):
    """`created_by_user_id` is a foreign key into `users`; a literal placeholder
    actor (for example "auto-heal") violates it and silently breaks the
    self-heal path, so automatic runs must pass actor=None like every other
    background reconcile (see GatewayOpsService._finish's `is_automatic` check).
    """
    calls: list[tuple[str, object]] = []
    service = GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
    )

    async def fake_install_cluster_gateway(cluster_id, *, actor=None):
        calls.append(("gateway", actor))

    async def fake_install_ipp(cluster_id, *, actor=None):
        calls.append(("ipp", actor))

    monkeypatch.setattr(service, "install_cluster_gateway", fake_install_cluster_gateway)
    monkeypatch.setattr(service, "install_ipp", fake_install_ipp)

    await service._heal_data_plane("cluster-heal")

    assert calls == [("gateway", None), ("ipp", None)]


@pytest.mark.asyncio
async def test_probe_members_flags_missing_model_server(monkeypatch):
    member = _seed_component_member("percomp-modelserver-down")
    calls: list = []
    service = GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
        runner_factory=lambda _cid: _ComponentRunner(calls, model_server_ready=False),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"),
    )
    await service.probe_members()

    saved = ModelServiceMemberDao().get(member.id)
    assert saved.status == "unhealthy"
    assert saved.health_json["components"]["modelServer"] == "missing"
    assert saved.health_json["components"]["epp"] == "ready"


def test_resolve_authz_endpoint_uses_live_serving_port(monkeypatch):
    monkeypatch.setattr(gateway_ops_module.hostinfo, "current_serving_port", lambda: 8084)
    assert gateway_ops_module.resolve_authz_endpoint(None) == (None, 8081)
    assert gateway_ops_module.resolve_authz_endpoint("  ") == (None, 8081)
    assert gateway_ops_module.resolve_authz_endpoint("10.0.0.5") == ("10.0.0.5", 8084)
    # A stray user-supplied port is ignored: Lens always uses its own.
    assert gateway_ops_module.resolve_authz_endpoint("10.0.0.5:9999") == ("10.0.0.5", 8084)


def test_resolve_authz_endpoint_falls_back_before_first_request(monkeypatch):
    monkeypatch.setattr(gateway_ops_module.hostinfo, "current_serving_port", lambda: None)
    assert gateway_ops_module.resolve_authz_endpoint("10.0.0.5") == ("10.0.0.5", 8081)
