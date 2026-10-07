"""Tests for cluster monitoring stack discovery and API contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_d_bench.api.main import app
from llm_d_bench.monitoring.cluster_stack.discovery import discover_cluster_stack
from llm_d_bench.monitoring.cluster_stack.models import ClusterStackInstallRequest
from llm_d_bench.monitoring.cluster_stack.service import validate_namespace
from llm_d_bench.utils.shell import CommandResult


def _result(argv: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(tuple(argv), returncode, stdout, stderr)


class FakeRunner:
    def __init__(
        self,
        *,
        installed: bool,
        include_pods: bool = False,
        use_labels: bool = True,
        drop_kube_state: bool = False,
    ) -> None:
        self.installed = installed
        self.include_pods = include_pods
        self.use_labels = use_labels
        self.drop_kube_state = drop_kube_state

    def executable(self, name: str) -> str | None:
        return f"/usr/bin/{name}"

    async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
        del timeout
        command = " ".join(argv)
        if command == "kubectl config current-context":
            return _result(argv, "kind-test\n")
        if command == "kubectl cluster-info":
            return _result(argv, "Kubernetes control plane is running")
        if command.startswith("kubectl get clusterversion"):
            return _result(argv, stderr="not found", returncode=1)
        if command.startswith("helm status"):
            if not self.installed:
                return _result(argv, stderr="Error: release: not found", returncode=1)
            return _result(
                argv,
                json.dumps(
                    {
                        "version": 2,
                        "info": {"status": "deployed"},
                        "chart": "kube-prometheus-stack-72.0.0",
                    }
                ),
            )
        if command.startswith("helm list"):
            return _result(argv, json.dumps([{"chart": "kube-prometheus-stack-72.0.0"}]))
        if "app.kubernetes.io/instance=llmd" in command:
            items = []
            if self.installed:
                if self.include_pods:
                    items = [self._pod("llmd-grafana-85c664997b-s6gxk", 3, 3)]
                    if not self.drop_kube_state:
                        items.append(self._pod("llmd-kube-state-metrics-6d4f9c8b7-abc12", 1, 1))
                else:
                    items = [
                        self._deployment("llmd-kube-prometheus-stack-prometheus"),
                        self._deployment("llmd-grafana"),
                    ]
                    if not self.drop_kube_state:
                        items.append(self._deployment("llmd-kube-state-metrics"))
            return _result(argv, json.dumps({"items": items}))
        if "app.kubernetes.io/name in (prometheus,alertmanager)" in command:
            items = []
            if self.installed:
                if self.include_pods:
                    items = [
                        self._pod("prometheus-llmd-kube-prometheus-stack-prometheus-0", 2, 2, "prometheus"),
                        self._pod("alertmanager-llmd-kube-prometheus-stack-alertmanager-0", 2, 2, "alertmanager"),
                    ]
                else:
                    items = [
                        self._statefulset("prometheus-llmd-kube-prometheus-stack-prometheus", "prometheus"),
                        self._statefulset("alertmanager-llmd-kube-prometheus-stack-alertmanager", "alertmanager"),
                    ]
            return _result(argv, json.dumps({"items": items}))
        if "app.kubernetes.io/name=prometheus-operator" in command:
            items = [self._deployment("shared-prometheus-operator")] if self.installed and self.use_labels else []
            return _result(argv, json.dumps({"items": items}))
        if "app=node-exporter" in command:
            items = [self._daemonset("shared-node-exporter")] if self.installed and self.use_labels else []
            return _result(argv, json.dumps({"items": items}))
        if "--all-namespaces" in command and "pods,deployments" in command and "-l" not in command:
            items = [self._deployment("shared-prometheus-operator")] if self.installed else []
            return _result(argv, json.dumps({"items": items}))
        if "--all-namespaces" in command and "pods,daemonsets" in command and "-l" not in command:
            items = [self._daemonset("shared-node-exporter")] if self.installed else []
            return _result(argv, json.dumps({"items": items}))
        if "grafana_dashboard=1" in command:
            items = [{"kind": "ConfigMap", "metadata": {"name": "llm-d-dashboard"}}] if self.installed else []
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl get crd"):
            return _result(argv, "customresourcedefinition") if self.installed else _result(argv, returncode=1)
        raise AssertionError(f"Unexpected command: {command}")

    @staticmethod
    def _deployment(name: str) -> dict:
        return {
            "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {"replicas": 1},
            "status": {"replicas": 1, "readyReplicas": 1},
        }

    @staticmethod
    def _daemonset(name: str) -> dict:
        return {
            "kind": "DaemonSet",
            "metadata": {"name": name},
            "status": {"numberReady": 1, "desiredNumberScheduled": 1},
        }

    @staticmethod
    def _statefulset(name: str, label: str) -> dict:
        return {
            "kind": "StatefulSet",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/name": label}},
            "spec": {"replicas": 1},
            "status": {"replicas": 1, "readyReplicas": 1},
        }

    @staticmethod
    def _pod(name: str, ready: int, total: int, label: str | None = None) -> dict:
        metadata: dict = {"name": name}
        if label:
            metadata["labels"] = {"app.kubernetes.io/name": label}
        return {
            "kind": "Pod",
            "metadata": metadata,
            "spec": {"containers": [{"name": f"container-{i}"} for i in range(total)]},
            "status": {"containerStatuses": [{"name": f"container-{i}", "ready": i < ready} for i in range(total)]},
        }


@pytest.mark.asyncio
async def test_discovery_reports_absent_stack():
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=False))

    assert status.status == "absent"
    assert status.cluster.context == "kind-test"
    assert status.release.name == "llmd"


@pytest.mark.asyncio
async def test_discovery_reports_ready_with_external_operator():
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=True))

    assert status.status == "ready"
    components = {component.name: component for component in status.components}
    assert components["prometheus"].status == "ready"
    assert components["grafana"].status == "ready"
    assert components["prometheus_operator"].status == "external"
    assert components["node_exporter"].status == "external"
    assert components["node_exporter"].source == "cluster"
    assert components["alertmanager"].status == "ready"
    assert components["monitoring_crds"].ready == 3


@pytest.mark.asyncio
async def test_discovery_reports_degraded_when_one_of_eight_components_is_not_ready():
    # Regression test: the aggregate status computation used to only look at
    # prometheus/grafana/operator/crds/dashboards, silently ignoring
    # alertmanager/kube_state_metrics/node_exporter entirely. That meant a
    # cluster with e.g. 7 of its 8 tracked components ready (kube-state-metrics
    # missing here) still reported the whole stack as "ready" instead of
    # "degraded" -- exactly the "shows 7/8 but still says Ready" bug.
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=True, drop_kube_state=True))

    components = {component.name: component for component in status.components}
    ready_count = sum(1 for component in components.values() if component.status in {"ready", "external"})
    assert ready_count == 7
    assert len(components) == 8
    assert components["kube_state_metrics"].status == "missing"
    assert status.status == "degraded"


@pytest.mark.asyncio
async def test_discovery_reports_external_when_labels_absent():
    # Operators/node-exporters installed without the standard labels are still
    # discovered cluster-wide by name, mirroring the installer's detection.
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=True, use_labels=False))

    assert status.status == "ready"
    components = {component.name: component for component in status.components}
    assert components["prometheus_operator"].status == "external"
    assert components["prometheus_operator"].source == "cluster"
    assert components["node_exporter"].status == "external"
    assert components["node_exporter"].source == "cluster"


@pytest.mark.asyncio
async def test_discovery_reports_pod_container_counts():
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=True, include_pods=True))
    components = {component.name: component for component in status.components}
    assert components["prometheus"].status == "ready"
    assert (components["prometheus"].ready, components["prometheus"].desired) == (2, 2)
    assert (components["alertmanager"].ready, components["alertmanager"].desired) == (2, 2)
    assert (components["grafana"].ready, components["grafana"].desired) == (3, 3)


@pytest.mark.asyncio
async def test_discovery_reports_expected_dashboard_counts():
    runner = FakeRunner(installed=True)

    ready_status = await discover_cluster_stack("llm-d-monitoring", runner, expected_dashboards=["llm-d-dashboard"])
    ready_components = {component.name: component for component in ready_status.components}
    assert ready_components["llm_d_dashboards"].status == "ready"
    assert ready_components["llm_d_dashboards"].ready == 1
    assert ready_components["llm_d_dashboards"].desired == 1
    assert ready_status.status == "ready"

    missing_status = await discover_cluster_stack(
        "llm-d-monitoring",
        runner,
        expected_dashboards=["llm-d-vllm-overview", "llm-d-sglang-overview"],
    )
    missing_components = {component.name: component for component in missing_status.components}
    assert missing_components["llm_d_dashboards"].status == "missing"
    assert missing_components["llm_d_dashboards"].ready == 0
    assert missing_components["llm_d_dashboards"].desired == 2
    assert missing_status.status == "degraded"


def test_install_request_forbids_unknown_fields_and_validates_namespace():
    with pytest.raises(ValueError):
        ClusterStackInstallRequest.model_validate({"namespace": "Invalid_Name"})
    with pytest.raises(ValueError):
        ClusterStackInstallRequest.model_validate({"unexpected": True})
    assert validate_namespace("llm-d-monitoring") == "llm-d-monitoring"


def test_shared_api_registers_scoped_monitoring_routes():
    client = TestClient(app)
    paths = client.get("/api/simulation/openapi.json").json()["paths"]

    assert "/api/v1/monitoring/cluster-stack/status" in paths
    assert "/api/v1/monitoring/cluster-stack/installations" in paths
    assert "/api/v1/monitoring/cluster-stack/operations/{operation_id}" in paths
    assert "/api/v1/monitoring/cluster-stack/links" in paths


def test_operation_path_cannot_escape_store(tmp_path: Path):
    from llm_d_bench.monitoring.cluster_stack.models import ClusterStackOperationResponse
    from llm_d_bench.monitoring.cluster_stack.operations import (
        ClusterStackOperationManager,
        OperationStore,
    )

    store = OperationStore(tmp_path)
    assert store.load("../../etc/passwd") is None

    manager = ClusterStackOperationManager(store)
    operation = ClusterStackOperationResponse(
        operation_id="a" * 32,
        status="running",
        phase="installing_chart",
        namespace="llm-d-monitoring",
    )
    manager._append(operation, "\x1b[32mGrafana admin password: admin\x1b[0m")
    assert operation.logs[0].message == "Grafana admin password: [REDACTED]"

    store.save(operation)
    recovered_manager = ClusterStackOperationManager(store)
    recovered = recovered_manager.get(operation.operation_id)
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.error is not None
    assert recovered.error.code == "SERVICE_RESTARTED"


@pytest.mark.asyncio
async def test_install_operation_requires_ready_verification(tmp_path: Path):
    import asyncio
    from datetime import UTC, datetime

    from llm_d_bench.monitoring.cluster_stack.models import (
        ClusterStackStatusResponse,
        ClusterSummary,
        HelmReleaseSummary,
    )
    from llm_d_bench.monitoring.cluster_stack.operations import ClusterStackOperationManager, OperationStore

    script = tmp_path / "install.sh"
    script.write_text(
        "#!/bin/sh\necho 'Installing Prometheus stack'\necho 'Grafana admin password: admin'\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | 0o100)
    manager = ClusterStackOperationManager(OperationStore(tmp_path / "operations"))

    async def verify(namespace: str) -> ClusterStackStatusResponse:
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context="kind-test", reachable=True, platform="kubernetes"),
            namespace=namespace,
            release=HelmReleaseSummary(status="deployed"),
            status="ready",
            message="ready",
            observed_at=datetime.now(UTC),
        )

    created = await manager.create(
        ClusterStackInstallRequest(),
        context="kind-test",
        script=script,
        verifier=verify,
        idempotency_key="request-1",
    )
    for _ in range(50):
        completed = manager.get(created.operation_id)
        if completed and completed.status in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.01)

    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.phase == "completed"
    assert all("admin" not in entry.message.rsplit(":", 1)[-1].strip() for entry in completed.logs)
    persisted = manager.store.load(created.operation_id)
    assert persisted is not None
    assert persisted.status == "succeeded"


@pytest.mark.asyncio
async def test_reinstall_runs_uninstall_then_install(tmp_path: Path):
    import asyncio
    from datetime import UTC, datetime

    from llm_d_bench.monitoring.cluster_stack.models import (
        ClusterStackStatusResponse,
        ClusterSummary,
        HelmReleaseSummary,
    )
    from llm_d_bench.monitoring.cluster_stack.operations import ClusterStackOperationManager, OperationStore

    script = tmp_path / "install.sh"
    script.write_text('#!/bin/sh\necho "argv: $*"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | 0o100)
    manager = ClusterStackOperationManager(OperationStore(tmp_path / "operations"))

    async def verify(namespace: str) -> ClusterStackStatusResponse:
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context="kind-test", reachable=True, platform="kubernetes"),
            namespace=namespace,
            release=HelmReleaseSummary(status="deployed"),
            status="ready",
            message="ready",
            observed_at=datetime.now(UTC),
        )

    created = await manager.create(
        ClusterStackInstallRequest(reinstall=True),
        context="kind-test",
        script=script,
        verifier=verify,
        idempotency_key="reinstall-1",
    )
    for _ in range(50):
        completed = manager.get(created.operation_id)
        if completed and completed.status in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.01)

    assert completed is not None
    assert completed.status == "succeeded"
    argv_lines = [entry.message for entry in completed.logs if entry.message.startswith("argv:")]
    assert len(argv_lines) == 2
    assert "--uninstall" in argv_lines[0]
    assert "--uninstall" not in argv_lines[1]


@pytest.mark.asyncio
async def test_cluster_links_open_tunnels(monkeypatch):
    from llm_d_bench.monitoring.cluster_stack import service as cluster_service
    from llm_d_bench.monitoring.cluster_stack.service import get_links

    class ServicesRunner:
        async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
            del timeout
            assert "services" in " ".join(argv)
            return _result(
                argv,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "llmd-grafana"},
                                "spec": {"ports": [{"port": 80}]},
                            },
                            {
                                "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
                                "spec": {"ports": [{"port": 9090}, {"port": 8080}]},
                            },
                        ]
                    }
                ),
            )

    forwarded = {}

    class FakeForward:
        local_port = 18080

    async def fake_ensure(namespace: str, service: str, remote_port: int, **kwargs) -> FakeForward:
        del kwargs
        forwarded[(namespace, service, remote_port)] = 18080
        return FakeForward()

    monkeypatch.setattr(cluster_service, "_runner", lambda _cluster_id: ServicesRunner())
    monkeypatch.setattr(cluster_service, "ensure_port_forward", fake_ensure)

    response = await get_links("llm-d-monitoring")
    links = {link.kind: link for link in response.links}

    assert links["prometheus"].available is True
    assert links["prometheus"].service == "llmd-kube-prometheus-stack-prometheus"
    assert links["prometheus"].port == 9090
    assert links["prometheus"].local_port == 18080
    assert links["grafana"].available is True
    assert links["grafana"].service == "llmd-grafana"
    assert links["grafana"].port == 80
    assert links["grafana"].local_port == 18080
    assert ("llm-d-monitoring", "llmd-kube-prometheus-stack-prometheus", 9090) in forwarded
    assert ("llm-d-monitoring", "llmd-grafana", 80) in forwarded


@pytest.mark.asyncio
async def test_cluster_links_reports_unavailable_when_services_missing(monkeypatch):
    from llm_d_bench.monitoring.cluster_stack import service as cluster_service
    from llm_d_bench.monitoring.cluster_stack.service import get_links

    class EmptyServicesRunner:
        async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
            del timeout
            return _result(argv, json.dumps({"items": []}))

    monkeypatch.setattr(cluster_service, "_runner", lambda _cluster_id: EmptyServicesRunner())

    response = await get_links("llm-d-monitoring")
    assert {link.kind for link in response.links} == {"prometheus", "grafana"}
    assert all(link.available is False for link in response.links)
    assert all(link.message for link in response.links)
