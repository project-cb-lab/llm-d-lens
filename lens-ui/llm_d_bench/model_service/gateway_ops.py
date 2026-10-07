"""Gateway operations for the model-service data plane (llm-d Gateway Mode).

The cluster installs ONE shared Gateway (provider-selectable) at creation; each
model service publishes an HTTPRoute -> InferencePool plus IPP model-mapping.
Deployments own their InferencePool + EPP and run no proxy. Design reference:
docs/design/model-service-llmd-routing-design.zh-CN.md sections 5/10.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

import httpx
import yaml

from llm_d_bench.auth.service import default_service as default_auth_service
from llm_d_bench.cluster.gateway_crds import apply_gateway_crds
from llm_d_bench.cluster.registry import get_cluster, update_cluster
from llm_d_bench.db.dao.model_service_gateway_operation import GatewayOperationDao
from llm_d_bench.db.dao.model_service_group import ModelServiceGroupDao
from llm_d_bench.db.dao.model_service_member import ModelServiceMemberDao
from llm_d_bench.model_service.contracts import utcnow
from llm_d_bench.model_service.gateway_contracts import (
    GatewayOperation,
    GatewayStatus,
    GatewayStatusCluster,
    GatewayStatusMember,
)
from llm_d_bench.model_service.gateway_providers import (
    AUTHZ_SERVICE_PORT,
    DEFAULT_PROVIDER,
    MANAGED_LABEL,
    MANAGED_VALUE,
    PoolBinding,
    default_pool_name,
    ensure_istioctl,
    istio_authz_overlay,
    provider_spec,
    render_inference_gateway,
    render_model_route,
    render_node_inotify_daemonset,
    resource_name,
    split_pool_name,
)
from llm_d_bench.model_service.targets import (
    ExecutionMissingError,
    ExecutionTarget,
    ExecutionTargetError,
    resolve_execution_target,
)
from llm_d_bench.utils import hostinfo
from llm_d_bench.utils.kubernetes import (
    PortForwardError,
    ensure_port_forward,
    scoped_runner,
    stop_port_forward_for,
)
from llm_d_bench.versions import (
    gateway_provider_version,
    inference_payload_processor_chart,
    inference_payload_processor_version,
)

logger = logging.getLogger(__name__)

RunnerFactory = Callable[[str | None], object]
OnLog = Callable[[str], Awaitable[None]]

DEFAULT_GATEWAY_NAMESPACE = "lens-gateway"
DEFAULT_GATEWAY_NAME = "lens-inference-gateway"
#: IPP Deployment/Service name installed per Gateway.
DEFAULT_IPP_NAME = "lens-ipp"
#: IPP gRPC (ext_proc) port the gateway connects to.
IPP_GRPC_PORT = 9004
#: Fallback ext_authz port used only before the backend has served any request
#: (so its real listening port is not yet known).
DEFAULT_AUTHZ_PORT = 8081


def resolve_authz_endpoint(value: str | None) -> tuple[str | None, int]:
    """Resolve a per-cluster Lens address into ``(host, destination_port)``.

    ``gateway_authz_host`` is the host/IP the cluster can reach Lens on (auto-filled
    in the create/edit form); the destination port is *not* a user setting — Lens
    uses its own live serving port
    (:func:`llm_d_bench.utils.hostinfo.current_serving_port`), falling back to
    :data:`DEFAULT_AUTHZ_PORT` before the first request. The in-cluster
    ``lens-authz`` Service keeps a stable port (:data:`AUTHZ_SERVICE_PORT`) and its
    Endpoints route there. An unset host disables ext_authz.
    """
    host = (value or "").strip()
    head, sep, tail = host.rpartition(":")
    if sep and tail.isdigit():
        host = head
    host = host or None
    if host is None:
        return None, DEFAULT_AUTHZ_PORT
    return host, hostinfo.current_serving_port() or DEFAULT_AUTHZ_PORT


class GatewayOpsError(Exception):
    """Gateway operation failed or the request is invalid."""


def member_pool_name(member) -> str | None:
    """Resolve a member's InferencePool name (bare), healing legacy rows.

    Members may store a namespace-qualified ``<namespace>/<pool>`` default; the
    HTTPRoute needs the bare pool name (the namespace travels separately).
    """
    _, name = split_pool_name(member.pool_name)
    if name:
        return name
    return default_pool_name(member.epp_ref or member.target_service)


def local_public_ip() -> str:
    """The host's outbound IP (used as the public host for exposed Gateways)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def _is_container_network(host: str) -> bool:
    """True for docker/kind bridge addresses (``172.16.0.0/12``).

    Such a node IP is only reachable inside the host running the containers, so
    the Gateway must be published through the Lens host's managed tunnel instead.
    """
    parts = host.split(".")
    return len(parts) == 4 and parts[0] == "172" and parts[1].isdigit() and 16 <= int(parts[1]) <= 31


def gateway_base_url(cluster: object, public_host: str, global_url: str | None) -> str:
    """Resolve the externally reachable OpenAI base URL for a cluster's Gateway.

    Layered so no single network layout is assumed:
      1. an explicit global/per-cluster public URL always wins;
      2. a LoadBalancer/Gateway address (the provider's own external address);
      3. a NodePort: the node address, unless it is a container-only (docker/kind)
         address, in which case only the Lens host's managed tunnel is reachable;
      4. empty when nothing else is available.
    """
    if global_url:
        return global_url
    # The user-supplied value is only a host/IP; the port comes from gateway_port.
    cluster_host = (getattr(cluster, "gateway_public_url", None) or "").strip()
    port = getattr(cluster, "gateway_port", None)
    if cluster_host:
        return f"http://{cluster_host}:{port}/v1" if port else f"http://{cluster_host}/v1"
    if port:
        node_host = getattr(cluster, "gateway_node_address", None)
        if node_host and _is_container_network(node_host):
            node_host = None
        host = node_host or public_host
        return f"http://{host}:{port}/v1" if host else ""
    address = getattr(cluster, "gateway_address", None)
    return f"http://{address}/v1" if address else ""


#: Minimum seconds between automatic data-plane re-reconciles for one cluster.
HEAL_COOLDOWN_SECONDS = 300.0


def _data_plane_degraded(components: dict) -> bool:
    """True when a member's components show a broken (not merely unready) data plane.

    Distinguishes the shared-Gateway serving path from a deployment that simply
    has not finished starting: a missing IPP, a failed end-to-end serving probe,
    or a missing/degraded route/pool warrants a re-reconcile.
    """
    return (
        components.get("ipp") == "missing"
        or components.get("serving") in {"degraded", "unreachable"}
        or components.get("httpRoute") in {"missing", "degraded"}
        or components.get("inferencePool") in {"missing", "degraded"}
    )


class GatewayOpsService:
    def __init__(
        self,
        *,
        operations: GatewayOperationDao | None = None,
        members: ModelServiceMemberDao | None = None,
        groups: ModelServiceGroupDao | None = None,
        runner_factory: RunnerFactory = scoped_runner,
        target_resolver: Callable[[str], ExecutionTarget] = resolve_execution_target,
        auth_service=None,
        gateway_ready_timeout: float = 30.0,
        gateway_poll_interval: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.operations = operations or GatewayOperationDao()
        self.members = members or ModelServiceMemberDao()
        self.groups = groups or ModelServiceGroupDao()
        self._runner_factory = runner_factory
        self._target_resolver = target_resolver
        self._auth = auth_service or default_auth_service()
        self._gateway_ready_timeout = gateway_ready_timeout
        self._gateway_poll_interval = gateway_poll_interval
        self._sleep = sleep
        self._last_heal: dict[str, float] = {}

    # --- helpers -------------------------------------------------------------
    def _record(self, operation: GatewayOperation) -> GatewayOperation:
        return self.operations.create(operation)

    @staticmethod
    async def _log(on_log: OnLog | None, message: str) -> None:
        if on_log is not None:
            await on_log(message)

    def _finish(self, operation: GatewayOperation, *, ok: bool, message: str, detail=None) -> GatewayOperation:
        operation.status = "succeeded" if ok else "failed"
        operation.message = message
        operation.detail_json = detail
        operation.finished_at = utcnow()
        saved = self.operations.save(operation)
        # Automatic reconcile runs on a background loop with no actor; those runs
        # are noise for the audit trail, so only user-initiated operations log.
        is_automatic = operation.kind == "reconcile-gateway" and operation.created_by_user_id is None
        if not is_automatic:
            self._auth.audit(
                "model_gateway_operation",
                result="success" if ok else "failure",
                actor_user_id=operation.created_by_user_id,
                target_type="model_gateway",
                target_id=operation.cluster_id or operation.kind,
                cluster_id=operation.cluster_id,
                detail={"kind": operation.kind, "status": saved.status, "message": message},
            )
        return saved

    async def _run(self, cluster_id: str, argv: list[str], *, input: str | None = None, timeout: float = 60):  # noqa: A002
        runner = self._runner_factory(cluster_id)
        return await runner.run(argv, input=input, timeout=timeout)

    async def _kubectl(self, cluster_id: str, args: list[str], *, input: str | None = None, timeout: float = 60):  # noqa: A002
        return await self._run(cluster_id, ["kubectl", *args], input=input, timeout=timeout)

    async def _existing_gateway_service_type(
        self, cluster_id: str, namespace: str, name: str, provider: str
    ) -> str | None:
        """Preserve a data-plane Service type already bound to the Gateway.

        Envoy Gateway stores it on the ``EnvoyProxy`` (``envoyService.type``); Istio
        stores it on the ``ConfigMap`` referenced by ``infrastructure.parametersRef``
        (``data.service``). Lets the automatic NodePort fallback for clusters without
        a LoadBalancer survive reconciles that do not repeat it.
        """
        try:
            ref = await self._kubectl(
                cluster_id,
                ["get", "gateway", name, "-n", namespace, "-o", "jsonpath={.spec.infrastructure.parametersRef.name}"],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        target = (ref.stdout or "").strip() if ref.returncode == 0 else ""
        if not target:
            return None
        if provider == "istio":
            try:
                config = await self._kubectl(
                    cluster_id,
                    ["get", "configmap", target, "-n", namespace, "-o", "jsonpath={.data.service}"],
                    timeout=15,
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return None
            match = re.search(r"type:\s*([A-Za-z]+)", config.stdout or "") if config.returncode == 0 else None
            return match.group(1) if match else None
        try:
            service = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "envoyproxy",
                    target,
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={.spec.provider.kubernetes.envoyService.type}",
                ],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        return (service.stdout or "").strip() or None if service.returncode == 0 else None

    async def _gateway_address_unassigned(self, cluster_id: str, namespace: str, name: str) -> bool:
        """True when the Gateway is not Programmed specifically for lack of an address."""
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "gateway",
                    name,
                    "-n",
                    namespace,
                    "-o",
                    'jsonpath={.status.conditions[?(@.type=="Programmed")].reason}',
                ],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return False
        return result.returncode == 0 and "AddressNotAssigned" in (result.stdout or "")

    def _published(self, cluster_id: str | None = None) -> list:
        members = self.members.list_by_cluster(cluster_id) if cluster_id else self.members.list_all()
        return [member for member in members if member.status != "disabled"]

    # --- operations ----------------------------------------------------------
    async def preflight(self, cluster_id: str, *, actor: str | None = None) -> GatewayOperation:
        operation = self._record(GatewayOperation(kind="preflight", cluster_id=cluster_id, created_by_user_id=actor))
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        try:
            result = await self._kubectl(cluster_id, ["version", "--client", "-o", "json"], timeout=15)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if result.returncode != 0:
            return self._finish(operation, ok=False, message=(result.stderr or "kubectl failed").strip())
        members = self._published(cluster_id)
        return self._finish(
            operation,
            ok=True,
            message=f"preflight ok ({len(members)} member(s))",
            detail={"members": len(members), "provider": cluster.gateway_provider},
        )

    async def _wait_gateway_programmed(
        self, cluster_id: str, namespace: str, name: str, *, on_log: OnLog | None = None
    ) -> bool:
        """Poll until the Gateway reports Programmed=True (or time out)."""
        elapsed = 0.0
        while True:
            state, ready = await self._gateway_state(cluster_id, namespace, name)
            if state == "installed" and ready:
                return True
            if elapsed >= self._gateway_ready_timeout:
                return False
            await self._log(on_log, f"Waiting for the gateway to program the route ({int(elapsed)}s)...")
            await (self._sleep or asyncio.sleep)(self._gateway_poll_interval)
            elapsed += self._gateway_poll_interval

    async def _gateway_state(self, cluster_id: str, namespace: str, name: str) -> tuple[str, bool | None]:
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "gateway",
                    name,
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={.status.conditions[?(@.type=='Programmed')].status}",
                ],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown", None
        if result.returncode != 0:
            return "missing", None
        return "installed", (result.stdout or "").strip() == "True"

    async def _gateway_address(self, cluster_id: str, namespace: str, name: str) -> str | None:
        """The Gateway's published address (node IP / LB hostname), or None."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "gateway", name, "-n", namespace, "-o", "jsonpath={.status.addresses[0].value}"],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        return (result.stdout or "").strip() or None if result.returncode == 0 else None

    async def _node_address(self, cluster_id: str) -> str | None:
        """A node's InternalIP, for building a public NodePort URL."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "nodes", "-o", 'jsonpath={.items[0].status.addresses[?(@.type=="InternalIP")].address}'],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        return (result.stdout or "").strip() or None if result.returncode == 0 else None

    async def _managed_route_names(self, cluster_id: str, namespace: str) -> set[str]:
        """HTTPRoute names Lens manages in a cluster's Gateway namespace."""
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "httproute",
                    "-n",
                    namespace,
                    "-l",
                    f"{MANAGED_LABEL}={MANAGED_VALUE}",
                    "-o",
                    "jsonpath={.items[*].metadata.name}",
                ],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return set()
        if result.returncode != 0:
            return set()
        return set((result.stdout or "").split())

    async def _gateway_service(
        self, cluster_id: str, namespace: str, name: str, provider: str
    ) -> tuple[str, str] | None:
        """The (namespace, name) of the Gateway's data-plane Service.

        The stored ``provider`` is only a hint: it can drift from what is actually
        installed (e.g. an old record), which used to silently skip the public
        exposure. Fall back to discovering the Service from the Gateway status.
        """
        target = await self._provider_gateway_service(cluster_id, namespace, name, provider)
        if target is not None:
            return target
        return await self._discover_gateway_service(cluster_id, namespace, name)

    async def _gateway_node_port(
        self, cluster_id: str, namespace: str, name: str, provider: str
    ) -> int | None:
        """The NodePort the data-plane Service exposes for its http (port 80) listener."""
        target = await self._gateway_service(cluster_id, namespace, name, provider)
        if target is None:
            return None
        service_namespace, service_name = target
        result = await self._kubectl(
            cluster_id, ["get", "svc", service_name, "-n", service_namespace, "-o", "json"], timeout=20
        )
        if result.returncode != 0:
            return None
        try:
            ports = (json.loads(result.stdout).get("spec") or {}).get("ports") or []
        except (ValueError, TypeError):
            return None
        for port in ports:
            if port.get("port") == 80 or port.get("name") == "http":
                node_port = port.get("nodePort")
                return int(node_port) if node_port else None
        return None

    async def _provider_gateway_service(
        self, cluster_id: str, namespace: str, name: str, provider: str
    ) -> tuple[str, str] | None:
        if provider == "istio":
            service = f"{name}-istio"
            try:
                result = await self._kubectl(
                    cluster_id, ["get", "svc", service, "-n", namespace, "-o", "name"], timeout=20
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return None
            return (namespace, service) if result.returncode == 0 else None
        if provider == "envoy-ai-gateway":
            try:
                result = await self._kubectl(
                    cluster_id,
                    [
                        "get",
                        "svc",
                        "-n",
                        "envoy-gateway-system",
                        "-l",
                        f"gateway.envoyproxy.io/owning-gateway-name={name}",
                        "-o",
                        "jsonpath={.items[0].metadata.name}",
                    ],
                    timeout=20,
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return None
            service = (result.stdout or "").strip() if result.returncode == 0 else ""
            return ("envoy-gateway-system", service) if service else None
        return None

    async def _discover_gateway_service(self, cluster_id: str, namespace: str, name: str) -> tuple[str, str] | None:
        """Provider-agnostic Service lookup for the Gateway (status address, then label)."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "gateway", name, "-n", namespace, "-o", "jsonpath={.status.addresses[0].value}"],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            result = None
        address = (result.stdout or "").strip() if result is not None and result.returncode == 0 else ""
        host = address.split(":")[0]
        parts = host.split(".")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return parts[1], parts[0]
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "svc",
                    "-n",
                    namespace,
                    "-l",
                    f"gateway.networking.k8s.io/gateway-name={name}",
                    "-o",
                    "jsonpath={.items[0].metadata.name}",
                ],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        service = (result.stdout or "").strip() if result.returncode == 0 else ""
        return (namespace, service) if service else None

    async def _actual_gateway_provider(self, cluster_id: str, namespace: str, name: str) -> str | None:
        """Map the installed Gateway's class to a Lens provider key."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "gateway", name, "-n", namespace, "-o", "jsonpath={.spec.gatewayClassName}"],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        class_name = (result.stdout or "").strip() if result.returncode == 0 else ""
        if not class_name:
            return None
        if class_name.startswith("gke"):
            return "gke"
        return {
            "istio": "istio",
            "envoy-ai-gateway": "envoy-ai-gateway",
            "agentgateway": "agentgateway",
        }.get(class_name)

    async def _ensure_gateway_exposure(
        self, cluster_id: str, provider: str, namespace: str, name: str, port: int, on_log: OnLog | None = None
    ) -> None:
        """Expose the Gateway publicly on ``0.0.0.0:<port>`` via a managed tunnel.

        On clusters whose nodes are not reachable (e.g. kind without published
        ports) the NodePort is only on the node's internal network; a host-side
        ``kubectl port-forward --address 0.0.0.0`` makes ``<lens-host>:<port>``
        reachable from other machines.
        """
        target = await self._gateway_service(cluster_id, namespace, name, provider)
        if target is None:
            return
        service_namespace, service = target
        try:
            await ensure_port_forward(
                service_namespace,
                service,
                80,
                local_port=port,  # noqa: S104 - deliberate public bind
                address="0.0.0.0",  # noqa: S104 - required for remote clients to reach the published Gateway
                cluster_id=cluster_id,  # noqa: S104 - deliberate public bind
            )
            await self._log(on_log, f"Gateway exposed on 0.0.0.0:{port} -> {service_namespace}/{service}")
        except (PortForwardError, FileNotFoundError, TimeoutError, OSError) as error:
            await self._log(on_log, f"Gateway exposure failed: {error}")

    async def _stop_gateway_exposure(
        self, cluster_id: str, provider: str, namespace: str, name: str, port: int | None
    ) -> None:
        if not port:
            return
        target = await self._gateway_service(cluster_id, namespace, name, provider)
        if target is None:
            return
        service_namespace, service = target
        await stop_port_forward_for(
            service_namespace,
            service,
            80,  # noqa: S104 - matches the public bind above
            address="0.0.0.0",  # noqa: S104 - matches the externally reachable Gateway bind
            cluster_id=cluster_id,  # noqa: S104 - matches public bind
        )

    async def _gateway_deployment(
        self, cluster_id: str, namespace: str, name: str, provider: str
    ) -> tuple[str, str] | None:
        """The (namespace, name) of the Gateway's data-plane Deployment."""
        if provider == "istio":
            return namespace, f"{name}-istio"
        if provider == "envoy-ai-gateway":
            try:
                result = await self._kubectl(
                    cluster_id,
                    [
                        "get",
                        "deploy",
                        "-n",
                        "envoy-gateway-system",
                        "-l",
                        f"gateway.envoyproxy.io/owning-gateway-name={name}",
                        "-o",
                        "jsonpath={.items[0].metadata.name}",
                    ],
                    timeout=20,
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return None
            deploy = (result.stdout or "").strip() if result.returncode == 0 else ""
            return ("envoy-gateway-system", deploy) if deploy else None
        return None

    async def scale_gateway(
        self, cluster_id: str, replicas: int, *, actor: str | None = None, on_log: OnLog | None = None
    ) -> GatewayOperation:
        """Start (1) or stop (0) a cluster's Gateway data-plane Deployment."""
        operation = self._record(
            GatewayOperation(kind="component-scale", cluster_id=cluster_id, created_by_user_id=actor)
        )
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        provider = cluster.gateway_provider or DEFAULT_PROVIDER
        namespace = cluster.gateway_namespace or DEFAULT_GATEWAY_NAMESPACE
        name = cluster.gateway_name or DEFAULT_GATEWAY_NAME
        target = await self._gateway_deployment(cluster_id, namespace, name, provider)
        if target is None:
            return self._finish(operation, ok=False, message="Gateway data-plane Deployment not found")
        deployment_namespace, deployment = target
        await self._log(on_log, f"Scaling {deployment_namespace}/{deployment} to {replicas}...")
        try:
            result = await self._kubectl(
                cluster_id,
                ["scale", f"deployment/{deployment}", "-n", deployment_namespace, f"--replicas={replicas}"],
                timeout=60,
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if result.returncode != 0:
            return self._finish(operation, ok=False, message=(result.stderr or "scale failed").strip())
        return self._finish(
            operation,
            ok=True,
            message=f"Gateway {'stopped' if replicas == 0 else 'started'} ({deployment_namespace}/{deployment})",
            detail={"namespace": deployment_namespace, "name": deployment, "replicas": replicas},
        )

    async def resolve_log_target(
        self, cluster_id: str, *, component: str | None, namespace: str | None, name: str | None
    ) -> tuple[str, str]:
        """Resolve a component name to (namespace, name) for log streaming."""
        cluster = get_cluster(cluster_id)
        if cluster is None:
            raise GatewayOpsError(f"cluster not found: {cluster_id}")
        gateway_namespace = cluster.gateway_namespace or DEFAULT_GATEWAY_NAMESPACE
        if component == "gateway":
            provider = cluster.gateway_provider or DEFAULT_PROVIDER
            gateway_name = cluster.gateway_name or DEFAULT_GATEWAY_NAME
            target = await self._gateway_deployment(cluster_id, gateway_namespace, gateway_name, provider)
            if target is None:
                raise GatewayOpsError("Gateway data-plane Deployment not found")
            return target
        if component == "ipp":
            return (namespace or gateway_namespace, name or DEFAULT_IPP_NAME)
        if not namespace or not name:
            raise GatewayOpsError("namespace and name are required")
        return namespace, name

    async def stream_component_logs(
        self,
        cluster_id: str,
        namespace: str,
        name: str,
        *,
        kind: str = "deployment",
        container: str | None = None,
        tail: int = 200,
    ):
        """Yield live log lines for a component (``kubectl logs -f``)."""
        from llm_d_bench.utils.kubernetes import kubeconfig_environment  # noqa: PLC0415
        from llm_d_bench.utils.shell import spawn, which  # noqa: PLC0415

        if which("kubectl") is None:
            raise GatewayOpsError("kubectl is not installed")
        env = kubeconfig_environment(cluster_id, strict=True)
        argv = [
            "kubectl",
            "logs",
            "-f",
            f"{kind}/{name}",
            "-n",
            namespace,
            f"--tail={tail}",
            "--all-containers=true",
            "--prefix=true",
        ]
        if container:
            argv = ["kubectl", "logs", "-f", f"{kind}/{name}", "-n", namespace, f"--tail={tail}", "-c", container]
        process = await spawn(argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            assert process.stdout is not None
            async for raw in process.stdout:
                yield raw.decode("utf-8", "replace").rstrip("\n")
        finally:
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                await process.communicate()

    async def node_port_owner(self, cluster_id: str, port: int) -> tuple[str, str] | None:
        """Public port-conflict check: who (if anyone) already uses ``port``."""
        cluster = get_cluster(cluster_id)
        provider = (cluster.gateway_provider if cluster else None) or DEFAULT_PROVIDER
        namespace = (cluster.gateway_namespace if cluster else None) or DEFAULT_GATEWAY_NAMESPACE
        name = (cluster.gateway_name if cluster else None) or DEFAULT_GATEWAY_NAME
        service = await self._gateway_service(cluster_id, namespace, name, provider)
        return await self._node_port_owner(cluster_id, port, service)

    async def release_gateway_port(self, cluster_id: str, port: int | None) -> None:
        """Tear down the managed exposure tunnel on ``port`` (used when it changes)."""
        if not port:
            return
        cluster = get_cluster(cluster_id)
        provider = (cluster.gateway_provider if cluster else None) or DEFAULT_PROVIDER
        namespace = (cluster.gateway_namespace if cluster else None) or DEFAULT_GATEWAY_NAMESPACE
        name = (cluster.gateway_name if cluster else None) or DEFAULT_GATEWAY_NAME
        await self._stop_gateway_exposure(cluster_id, provider, namespace, name, port)

    async def _node_port_owner(
        self, cluster_id: str, port: int, gateway_service: tuple[str, str] | None
    ) -> tuple[str, str] | None:
        """(namespace, name) of another Service already using ``port`` as a nodePort."""
        try:
            result = await self._kubectl(cluster_id, ["get", "svc", "-A", "-o", "json"], timeout=30)
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            items = json.loads(result.stdout or "{}").get("items", [])
        except json.JSONDecodeError:
            return None
        for item in items:
            metadata = item.get("metadata") or {}
            if gateway_service and (metadata.get("namespace"), metadata.get("name")) == gateway_service:
                continue
            for spec_port in (item.get("spec") or {}).get("ports", []):
                if spec_port.get("nodePort") == port:
                    return (metadata.get("namespace", ""), metadata.get("name", ""))
        return None

    async def ensure_exposures(self) -> int:
        """Ensure the public 0.0.0.0 tunnel for every cluster with a configured port."""
        from llm_d_bench.cluster.registry import list_clusters  # noqa: PLC0415

        ensured = 0
        for cluster in list_clusters():
            port = getattr(cluster, "gateway_port", None)
            if not port:
                continue
            namespace = cluster.gateway_namespace or DEFAULT_GATEWAY_NAMESPACE
            name = cluster.gateway_name or DEFAULT_GATEWAY_NAME
            provider = cluster.gateway_provider or DEFAULT_PROVIDER
            try:
                await self._ensure_gateway_exposure(cluster.id, provider, namespace, name, port)
                ensured += 1
            except Exception:  # noqa: BLE001, S112 - one cluster must not skip the rest
                continue
        return ensured

    async def _deployment_state(self, cluster_id: str, namespace: str, name: str) -> tuple[str, bool | None]:
        """Whether a component Deployment exists (and has a ready replica)."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "deploy", name, "-n", namespace, "-o", "jsonpath={.status.readyReplicas}"],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown", None
        if result.returncode != 0:
            return "missing", None
        try:
            ready = int((result.stdout or "0").strip() or "0")
        except ValueError:
            ready = 0
        return "installed", ready > 0

    async def install_inference_gateway(
        self,
        cluster_id: str,
        provider: str = DEFAULT_PROVIDER,
        *,
        namespace: str = DEFAULT_GATEWAY_NAMESPACE,
        name: str = DEFAULT_GATEWAY_NAME,
        install_prerequisites: bool = True,
        actor: str | None = None,
        on_log: OnLog | None = None,
    ) -> GatewayOperation:
        """Install (or reuse) the cluster's shared llm-d Gateway."""
        operation = self._record(
            GatewayOperation(kind="install-gateway", cluster_id=cluster_id, created_by_user_id=actor)
        )
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        try:
            spec = provider_spec(provider)
        except ValueError as error:
            return self._finish(operation, ok=False, message=str(error))
        # Per-cluster ext_authz host (auto-filled in the wizard/edit form) plus
        # Lens' own live serving port. Unset host = ext_authz disabled.
        authz_host, authz_port = resolve_authz_endpoint(getattr(cluster, "gateway_authz_host", None))
        if install_prerequisites:
            # Apply the pinned Gateway API / Gateway API Inference Extension CRDs
            # before the provider, so their versions follow the stack profile.
            try:
                crds = await apply_gateway_crds(cluster_id)
                for key, result in crds.items():
                    await self._log(on_log, f"{key} CRDs: {'applied' if result['ok'] else result['message']}")
            except Exception as error:  # noqa: BLE001 - provider install reports the real failure
                await self._log(on_log, f"could not apply pinned CRDs: {error}")
            install_commands = list(spec.install_commands)
            if provider == "istio":
                # `istioctl install` uses the binary's own version, so run the
                # istioctl pinned by the Lens stack profile.
                version = gateway_provider_version("istio") or ""
                try:
                    istioctl = str(await asyncio.to_thread(ensure_istioctl, version))
                except Exception as error:  # noqa: BLE001 - report download failures
                    return self._finish(operation, ok=False, message=f"could not obtain istioctl {version}: {error}")
                install_commands = [(istioctl, *command[1:]) for command in install_commands]
                if authz_host:
                    # Istio's ext_authz provider lives in meshConfig, which must be applied
                    # at install time; a values overlay is the reliable way to express the
                    # nested extensionProviders config.
                    overlay = tempfile.NamedTemporaryFile(  # noqa: SIM115 - kept for istioctl -f
                        mode="w", suffix="-istio-authz.yaml", delete=False, prefix="lens-"
                    )
                    overlay.write(istio_authz_overlay(namespace, AUTHZ_SERVICE_PORT, cluster_id))
                    overlay.close()
                    install_commands = [(istioctl, "install", "-y", "-f", overlay.name)]
            for argv in install_commands:
                argv = list(argv)
                await self._log(on_log, f"$ {' '.join(argv)}")
                try:
                    result = await self._run(cluster_id, list(argv), timeout=900)
                except (FileNotFoundError, TimeoutError, OSError) as error:
                    return self._finish(operation, ok=False, message=f"{argv[0]} unavailable: {error}")
                except Exception as error:  # noqa: BLE001 - runner-specific
                    return self._finish(operation, ok=False, message=str(error))
                if result.returncode != 0:
                    return self._finish(
                        operation,
                        ok=False,
                        message=(result.stderr or result.stdout or f"{argv[0]} install failed").strip(),
                    )
                await self._log(on_log, f"done: {spec.display_name}")
        service_type = (os.environ.get("LENS_GATEWAY_SERVICE_TYPE") or "").strip() or None
        if service_type is None:
            service_type = await self._existing_gateway_service_type(cluster_id, namespace, name, provider)
        service_port = getattr(cluster, "gateway_port", None)
        if service_port is not None:
            if not (30000 <= service_port <= 32767):
                return self._finish(
                    operation,
                    ok=False,
                    message=f"exposed port must be between 30000 and 32767 (got {service_port})",
                )
            owner = await self._node_port_owner(
                cluster_id, service_port, await self._gateway_service(cluster_id, namespace, name, provider)
            )
            if owner:
                return self._finish(
                    operation,
                    ok=False,
                    message=f"exposed port {service_port} is already in use by {owner[0]}/{owner[1]}",
                )

        async def apply_gateway(st: str | None):
            manifest = render_inference_gateway(
                namespace=namespace,
                gateway_name=name,
                provider=provider,
                service_type=st,
                service_port=service_port,
                authz_host=authz_host,
                authz_port=authz_port,
                authz_cluster_id=cluster_id,
            )
            return await self._kubectl(cluster_id, ["apply", "-f", "-"], input=manifest, timeout=120)

        await self._log(on_log, f"Applying shared Gateway ({provider}) in {namespace}...")
        try:
            applied = await apply_gateway(service_type)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if applied.returncode != 0:
            return self._finish(operation, ok=False, message=(applied.stderr or "apply failed").strip())
        programmed = await self._wait_gateway_programmed(cluster_id, namespace, name, on_log=on_log)
        if (
            not programmed
            and service_type is None
            and await self._gateway_address_unassigned(cluster_id, namespace, name)
        ):
            # No LoadBalancer address was assigned (e.g. a local kind cluster): switch the
            # data-plane Service to NodePort through the provider's own mechanism so the
            # Gateway can become Programmed. Persisted by the existing-type lookup above.
            await self._log(on_log, "Gateway has no address (no LoadBalancer); retrying with NodePort...")
            service_type = "NodePort"
            try:
                applied = await apply_gateway(service_type)
            except (FileNotFoundError, TimeoutError, OSError) as error:
                return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
            if applied.returncode != 0:
                return self._finish(operation, ok=False, message=(applied.stderr or "apply failed").strip())
            programmed = await self._wait_gateway_programmed(cluster_id, namespace, name, on_log=on_log)
        if programmed and service_port:
            await self._ensure_gateway_exposure(cluster_id, provider, namespace, name, service_port, on_log=on_log)
        # A NodePort data-plane Service with no pinned port gets a random one from
        # the provider; read it back and persist it, so the Gateway stays
        # reachable at a known external port (the wizard's "30080" is only a
        # placeholder) and later reconciles keep the same port.
        node_port = None
        if service_type == "NodePort":
            node_port = await self._gateway_node_port(cluster_id, namespace, name, provider)
            if node_port is not None and not service_port:
                with suppress(Exception):
                    update_cluster(cluster.id, gateway_port=node_port)
        detail = {
            "provider": provider,
            "namespace": namespace,
            "name": name,
            "programmed": programmed,
            "serviceType": service_type,
        }
        if node_port is not None:
            detail["nodePort"] = node_port
        if programmed:
            return self._finish(operation, ok=True, message=f"shared Gateway ready ({provider})", detail=detail)
        return self._finish(
            operation,
            ok=True,
            message="shared Gateway applied; provider is still programming it",
            detail=detail,
        )

    async def install_cluster_gateway(
        self,
        cluster_id: str,
        *,
        provider: str | None = None,
        install_prerequisites: bool = True,
        actor: str | None = None,
        on_log: OnLog | None = None,
    ) -> GatewayOperation:
        """Install the shared Gateway using the cluster record's pinned provider/coordinates."""
        cluster = get_cluster(cluster_id)
        if cluster is None:
            operation = self._record(
                GatewayOperation(kind="install-gateway", cluster_id=cluster_id, created_by_user_id=actor)
            )
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        return await self.install_inference_gateway(
            cluster_id,
            provider or cluster.gateway_provider or DEFAULT_PROVIDER,
            namespace=cluster.gateway_namespace or DEFAULT_GATEWAY_NAMESPACE,
            name=cluster.gateway_name or DEFAULT_GATEWAY_NAME,
            install_prerequisites=install_prerequisites,
            actor=actor,
            on_log=on_log,
        )

    async def apply_node_inotify_limit(
        self, cluster_id: str, limit: int, *, actor: str | None = None, on_log: OnLog | None = None
    ) -> GatewayOperation:
        """Apply ``fs.inotify.max_user_instances`` on every node via a DaemonSet.

        The limit is per real uid; a busy node (many pods) exhausts kind's 128
        default and controllers fail with "too many open files".
        """
        operation = self._record(GatewayOperation(kind="node-sysctl", cluster_id=cluster_id, created_by_user_id=actor))
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        if limit < 1:
            return self._finish(operation, ok=False, message="inotify limit must be at least 1")
        namespace = cluster.gateway_namespace or DEFAULT_GATEWAY_NAMESPACE
        image = os.environ.get("LENS_NODE_SYSCTL_IMAGE")
        manifest = render_node_inotify_daemonset(limit, namespace, image=image)
        await self._log(on_log, f"Applying node inotify limit {limit} in {namespace}...")
        try:
            applied = await self._kubectl(cluster_id, ["apply", "-f", "-"], input=manifest, timeout=60)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if applied.returncode != 0:
            return self._finish(operation, ok=False, message=(applied.stderr or "apply failed").strip())
        return self._finish(
            operation,
            ok=True,
            message=f"node inotify limit set to {limit}",
            detail={"limit": limit, "namespace": namespace},
        )

    async def uninstall_inference_gateway(self, cluster_id: str, *, actor: str | None = None) -> GatewayOperation:
        """Delete the managed shared Gateway resources on a cluster."""
        operation = self._record(
            GatewayOperation(kind="uninstall-gateway", cluster_id=cluster_id, created_by_user_id=actor)
        )
        if get_cluster(cluster_id) is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        cluster = get_cluster(cluster_id)
        await self._stop_gateway_exposure(
            cluster_id,
            (cluster.gateway_provider if cluster else None) or DEFAULT_PROVIDER,
            (cluster.gateway_namespace if cluster else None) or DEFAULT_GATEWAY_NAMESPACE,
            (cluster.gateway_name if cluster else None) or DEFAULT_GATEWAY_NAME,
            getattr(cluster, "gateway_port", None) if cluster else None,
        )
        try:
            first = await self._kubectl(
                cluster_id,
                [
                    "delete",
                    "gateway,gatewayclass,clienttrafficpolicy,httproute,inferencepool",
                    "-A",
                    "-l",
                    "app.kubernetes.io/part-of=lens-inference-gateway",
                    "--ignore-not-found=true",
                    "--wait=false",
                ],
                timeout=120,
            )
            # Legacy leftovers from the retired Agent Router (lens-agent-router).
            await self._kubectl(
                cluster_id,
                [
                    "delete",
                    "gateway,gatewayclass,httproute,aigatewayroute,aiservicebackend,backend",
                    "-l",
                    "app.kubernetes.io/part-of=lens-agent-router",
                    "-A",
                    "--ignore-not-found=true",
                ],
                timeout=120,
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if first.returncode != 0:
            return self._finish(operation, ok=False, message=(first.stderr or "delete failed").strip())
        return self._finish(operation, ok=True, message="shared Gateway resources deleted")

    async def install_ipp(
        self,
        cluster_id: str,
        *,
        namespace: str | None = None,
        name: str = DEFAULT_IPP_NAME,
        provider: str | None = None,
        gateway_name: str | None = None,
        chart: str | None = None,
        version: str | None = None,
        config: str | None = None,
        actor: str | None = None,
        on_log: OnLog | None = None,
    ) -> GatewayOperation:
        """Install/update the Inference Payload Processor (one per Gateway).

        ``config`` is a ``PayloadProcessorConfig`` body supplied at apply time (it is
        not persisted in Lens); empty uses the chart's built-in default.
        """
        operation = self._record(GatewayOperation(kind="install-ipp", cluster_id=cluster_id, created_by_user_id=actor))
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        namespace = namespace or cluster.gateway_namespace or DEFAULT_GATEWAY_NAMESPACE
        gateway_name = gateway_name or cluster.gateway_name or DEFAULT_GATEWAY_NAME
        provider = provider or cluster.gateway_provider or DEFAULT_PROVIDER
        chart = (chart or inference_payload_processor_chart()).strip()
        version = (version or inference_payload_processor_version()).strip()
        chart_provider = provider if provider in ("istio", "gke") else "none"
        argv = [
            "helm",
            "upgrade",
            "--install",
            name,
            chart,
            "--namespace",
            namespace,
            "--create-namespace",
            "--set",
            f"payloadProcessor.name={name}",
            "--set",
            f"provider.name={chart_provider}",
            "--set",
            f"inferenceGateway.name={gateway_name}",
        ]
        if version:
            argv += ["--version", version]
        # A custom PayloadProcessorConfig is nested YAML; Helm --set cannot express it,
        # so write a values file (the chart adds apiVersion/kind itself).
        custom_config = config
        if custom_config and custom_config.strip():
            try:
                body = yaml.safe_load(custom_config)
            except yaml.YAMLError as error:
                return self._finish(operation, ok=False, message=f"IPP config is not valid YAML: {error}")
            if not isinstance(body, dict):
                return self._finish(operation, ok=False, message="IPP config must be a YAML mapping")
            body.pop("apiVersion", None)
            body.pop("kind", None)
            values = tempfile.NamedTemporaryFile(  # noqa: SIM115 - kept for helm -f
                mode="w", suffix="-ipp-values.yaml", delete=False, prefix="lens-"
            )
            yaml.safe_dump({"payloadProcessor": {"customConfig": body}}, values, sort_keys=False)
            values.close()
            argv += ["-f", values.name]
        await self._log(on_log, f"$ {' '.join(argv)}")
        try:
            result = await self._run(cluster_id, argv, timeout=900)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"helm unavailable: {error}")
        except Exception as error:  # noqa: BLE001 - runner-specific
            return self._finish(operation, ok=False, message=str(error))
        if result.returncode != 0:
            return self._finish(
                operation,
                ok=False,
                message=(result.stderr or result.stdout or "IPP install failed").strip(),
            )
        if (config or "").strip():
            # The IPP hot-reloads its ConfigMap via fsnotify, which fails on
            # inotify-exhausted nodes; restart so a config change is guaranteed
            # to be read instead of silently staying on the old config.
            await self._log(on_log, f"Restarting {namespace}/{name} to load the new config...")
            restarted = await self._kubectl(
                cluster_id, ["rollout", "restart", f"deploy/{name}", "-n", namespace], timeout=30
            )
            if restarted.returncode != 0:
                await self._log(on_log, f"rollout restart failed: {(restarted.stderr or '').strip()}")
        # The chart ships no probes, so a hung payload processor stays Ready and
        # stays in the Service endpoints, silently failing every gateway request.
        # TCP probes on the gRPC port let kubelet restart it and remove it from
        # the Service when it stops listening.
        probe_patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "payload-processor",
                                "livenessProbe": {
                                    "tcpSocket": {"port": IPP_GRPC_PORT},
                                    "initialDelaySeconds": 10,
                                    "periodSeconds": 20,
                                    "failureThreshold": 3,
                                },
                                "readinessProbe": {
                                    "tcpSocket": {"port": IPP_GRPC_PORT},
                                    "initialDelaySeconds": 5,
                                    "periodSeconds": 10,
                                    "failureThreshold": 3,
                                },
                            }
                        ]
                    }
                }
            }
        }
        patched = await self._kubectl(
            cluster_id,
            ["patch", "deploy", name, "-n", namespace, "--type=strategic", "-p", json.dumps(probe_patch)],
            timeout=30,
        )
        if patched.returncode != 0:
            await self._log(on_log, f"could not add IPP probes: {(patched.stderr or '').strip()}")
        await self._log(on_log, "Waiting for the IPP rollout...")
        exists = await self._kubectl(cluster_id, ["get", "deploy", name, "-n", namespace, "-o", "name"], timeout=20)
        if exists.returncode != 0:
            pods = await self._kubectl(
                cluster_id, ["get", "pods", "-n", namespace, "-l", f"app={name}", "-o", "wide"], timeout=20
            )
            return self._finish(
                operation,
                ok=False,
                message=f"IPP Deployment {name} not found in {namespace}: {(exists.stderr or '').strip()}",
                detail={"pods": (pods.stdout or "").strip()[:2000]},
            )
        rollout = await self._kubectl(
            cluster_id, ["rollout", "status", f"deploy/{name}", "-n", namespace, "--timeout=180s"], timeout=200
        )
        pods = await self._kubectl(
            cluster_id, ["get", "pods", "-n", namespace, "-l", f"app={name}", "-o", "wide"], timeout=20
        )
        await self._log(on_log, (pods.stdout or "").strip() or f"no pods for app={name} in {namespace}")
        detail = {"provider": provider, "namespace": namespace, "name": name}
        if rollout.returncode != 0:
            return self._finish(
                operation,
                ok=True,
                message="IPP installed; rollout still converging",
                detail=detail,
            )
        return self._finish(operation, ok=True, message=f"IPP ready ({name})", detail=detail)

    async def set_ipp_config(
        self, cluster_id: str, config: str, *, actor: str | None = None, on_log: OnLog | None = None
    ) -> GatewayOperation:
        """Store the cluster's IPP config and reinstall IPP so it takes effect."""
        if get_cluster(cluster_id) is None:
            operation = self._record(
                GatewayOperation(kind="install-ipp", cluster_id=cluster_id, created_by_user_id=actor)
            )
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        # Applied straight to the cluster (Helm upgrade); not stored in Lens.
        return await self.install_ipp(cluster_id, config=config, actor=actor, on_log=on_log)

    async def get_ipp_config(self, cluster_id: str, *, namespace: str | None = None) -> dict:
        """Read a cluster's live IPP config from its ConfigMap (no Lens storage)."""
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return {"config": "", "custom": False, "default": ""}
        namespace = namespace or cluster.gateway_namespace or DEFAULT_GATEWAY_NAMESPACE
        result = await self._kubectl(
            cluster_id,
            ["get", "configmap", DEFAULT_IPP_NAME, "-n", namespace, "-o", "jsonpath={.data}"],
            timeout=20,
        )
        if result.returncode != 0:
            return {"config": "", "custom": False, "default": ""}
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return {"config": "", "custom": False, "default": ""}
        custom = data.get("custom-ipp-config.yaml", "")
        default = data.get("default-ipp-config.yaml", "")
        return {"config": custom or default, "custom": bool(custom), "default": default}

    async def uninstall_ipp(
        self,
        cluster_id: str,
        *,
        namespace: str | None = None,
        name: str = DEFAULT_IPP_NAME,
        actor: str | None = None,
        on_log: OnLog | None = None,
    ) -> GatewayOperation:
        """Remove the Inference Payload Processor from a cluster."""
        operation = self._record(
            GatewayOperation(kind="uninstall-ipp", cluster_id=cluster_id, created_by_user_id=actor)
        )
        if get_cluster(cluster_id) is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        cluster = get_cluster(cluster_id)
        namespace = namespace or (cluster.gateway_namespace if cluster else None) or DEFAULT_GATEWAY_NAMESPACE
        await self._log(on_log, f"helm uninstall {name} -n {namespace}")
        try:
            result = await self._run(cluster_id, ["helm", "uninstall", name, "--namespace", namespace], timeout=300)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"helm unavailable: {error}")
        if result.returncode != 0:
            return self._finish(operation, ok=False, message=(result.stderr or "IPP uninstall failed").strip())
        # Clean up any experimental EnvoyExtensionPolicy from earlier versions.
        await self._kubectl(
            cluster_id,
            ["delete", "envoyextensionpolicy", name, "-n", namespace, "--ignore-not-found=true"],
            timeout=60,
        )
        return self._finish(operation, ok=True, message=f"IPP removed ({name})")

    async def _ensure_epp_plaintext(
        self, cluster_id: str, namespace: str, epp_name: str, *, on_log: OnLog | None = None
    ) -> bool:
        """Flip a deployment's EPP to plaintext serving if it was created with TLS.

        The cluster Gateway reaches the EPP over plaintext h2c; an EPP left on the
        default ``secure-serving=true`` resets the ext_proc stream (HTTP 500). Older
        deployments predate the gateway-mode router values, so heal them in place.
        """
        try:
            result = await self._kubectl(
                cluster_id, ["get", "deploy", epp_name, "-n", namespace, "-o", "json"], timeout=20
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return False
        if result.returncode != 0:
            return False
        try:
            deployment = json.loads(result.stdout or "{}")
        except ValueError:
            return False
        containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
        for index, container in enumerate(containers):
            args = list(container.get("args") or [])
            # Identify the EPP container (the sidecar has no --pool-name/--grpc-port).
            if not any(a.startswith("--pool-name") or a.startswith("--grpc-port") for a in args):
                continue
            flag_index = next((i for i, a in enumerate(args) if a.startswith("--secure-serving")), None)
            if flag_index is not None:
                if args[flag_index] == "--secure-serving=false":
                    return False
                patch = [
                    {
                        "op": "replace",
                        "path": f"/spec/template/spec/containers/{index}/args/{flag_index}",
                        "value": "--secure-serving=false",
                    }
                ]
            else:
                patch = [
                    {
                        "op": "add",
                        "path": f"/spec/template/spec/containers/{index}/args/-",
                        "value": "--secure-serving=false",
                    }
                ]
            await self._log(on_log, f"EPP {namespace}/{epp_name}: set secure-serving=false (plaintext ext_proc)")
            applied = await self._kubectl(
                cluster_id,
                ["patch", "deploy", epp_name, "-n", namespace, "--type=json", "-p", json.dumps(patch)],
                timeout=30,
            )
            if applied.returncode != 0:
                await self._log(on_log, f"EPP patch failed: {(applied.stderr or '').strip()}")
            return applied.returncode == 0
        return False

    async def reconcile_cluster_gateway(
        self, cluster_id: str, *, actor: str | None = None, on_log: OnLog | None = None
    ) -> GatewayOperation:
        """Ensure the shared Gateway and each model service's HTTPRoute + IPP config."""
        operation = self._record(
            GatewayOperation(kind="reconcile-gateway", cluster_id=cluster_id, created_by_user_id=actor)
        )
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        provider = cluster.gateway_provider or DEFAULT_PROVIDER
        namespace = cluster.gateway_namespace or DEFAULT_GATEWAY_NAMESPACE
        name = cluster.gateway_name or DEFAULT_GATEWAY_NAME
        # Reconcile only (re)applies the Gateway resources; it must not re-run the
        # provider install (helm/istioctl) on every pass — that is slow and fail-prone.
        install = await self.install_inference_gateway(
            cluster_id,
            provider,
            namespace=namespace,
            name=name,
            install_prerequisites=False,
            actor=actor,
            on_log=on_log,
        )
        if install.status != "succeeded":
            return self._finish(operation, ok=False, message=f"gateway install failed: {install.message}")
        documents: list[str] = []
        groups = {group.id: group for group in self.groups.list()}
        by_group: dict[str, list] = {}
        for member in self.members.list_by_cluster(cluster_id):
            by_group.setdefault(member.group_id, []).append(member)
        # Heal members whose EPP predates the plaintext gateway-mode requirement.
        # Only istio/agentgateway reach the EPP over plaintext h2c; envoy-ai-gateway
        # and gke expect TLS, so forcing plaintext there would break the ext_proc.
        if provider in ("istio", "agentgateway"):
            healed: set[tuple[str, str]] = set()
            for member in self.members.list_by_cluster(cluster_id):
                if member.status != "active":
                    continue
                epp_name = member.epp_ref or (f"{member.pool_name}-epp" if member.pool_name else "")
                if not epp_name:
                    continue
                key = (member.target_namespace, epp_name)
                if key in healed:
                    continue
                healed.add(key)
                await self._ensure_epp_plaintext(cluster_id, member.target_namespace, epp_name, on_log=on_log)
        applied_names: set[str] = set()
        for group_id, cluster_members in by_group.items():
            group = groups.get(group_id)
            if group is None:
                continue
            # A model service is served per cluster: render a route from the members
            # that live in THIS cluster (a service may also have members elsewhere).
            # Include every published member, not only healthy ones: the health
            # probe marks a member unhealthy when its route is missing, so routing
            # only "active" members would never create the route that heals it.
            published = [m for m in cluster_members if m.status != "disabled" and member_pool_name(m)]
            if not published:
                continue
            # llm-d's data plane is one EPP per InferencePool and its gateway chart
            # renders one pool per HTTPRoute, so a model service routes to a single
            # pool (the first published member in this cluster).
            member = published[0]
            applied_names.add(resource_name(f"{group.name}-{group.id}"))
            documents.append(
                render_model_route(
                    namespace=namespace,
                    # The same public model name can exist per cluster, so the route /
                    # ConfigMap names must be unique per group.
                    model_name=f"{group.name}-{group.id}",
                    base_model=group.base_model or group.model_ref,
                    pool=PoolBinding(member_pool_name(member), member.target_port, member.target_namespace),
                    gateway_name=name,
                    gateway_namespace=namespace,
                )
            )
        if documents:
            try:
                applied = await self._kubectl(
                    cluster_id, ["apply", "-f", "-"], input="---\n".join(documents), timeout=120
                )
            except (FileNotFoundError, TimeoutError, OSError) as error:
                return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
            if applied.returncode != 0:
                return self._finish(operation, ok=False, message=(applied.stderr or "route apply failed").strip())
        # Remove routes this cluster no longer serves (member deleted/moved away).
        stale = sorted(await self._managed_route_names(cluster_id, namespace) - applied_names)
        for route_name in stale:
            await self._log(on_log, f"Removing stale model route {route_name}...")
            await self._kubectl(
                cluster_id, ["delete", "httproute", route_name, "-n", namespace, "--ignore-not-found=true"], timeout=60
            )
            await self._kubectl(
                cluster_id,
                ["delete", "configmap", f"{route_name}-model-map", "-n", namespace, "--ignore-not-found=true"],
                timeout=60,
            )
        return self._finish(
            operation,
            ok=True,
            message=f"gateway reconciled ({provider}); {len(documents)} model route(s)",
            detail={
                "provider": provider,
                "namespace": namespace,
                "routes": len(documents),
                "removed": len(stale),
            },
        )

    async def scale_component(
        self,
        cluster_id: str,
        namespace: str,
        name: str,
        replicas: int,
        *,
        kind: str = "deployment",
        actor: str | None = None,
    ) -> GatewayOperation:
        """Scale a data-plane component Deployment (Gateway data plane / EPP / model server)."""
        operation = self._record(
            GatewayOperation(kind="component-scale", cluster_id=cluster_id, created_by_user_id=actor)
        )
        try:
            result = await self._kubectl(
                cluster_id, ["scale", kind, name, "-n", namespace, f"--replicas={replicas}"], timeout=60
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if result.returncode != 0:
            return self._finish(operation, ok=False, message=(result.stderr or "scale failed").strip())
        return self._finish(
            operation,
            ok=True,
            message=f"{kind}/{name} scaled to {replicas}",
            detail={"namespace": namespace, "name": name, "replicas": replicas},
        )

    async def reconcile_cluster(self, cluster_id: str, *, actor: str | None = None) -> GatewayOperation:
        """Ensure the cluster's shared llm-d Gateway and each model service's route."""
        return await self.reconcile_cluster_gateway(cluster_id, actor=actor)

    async def probe_members(self) -> int:
        """Probe each non-disabled member's backend and refresh health/status.

        Members whose deployment execution record no longer exists (the
        deployment was deleted) are removed instead of left dangling.
        """
        updated = 0
        heal: set[str] = set()
        for member in self.members.list_all():
            if member.status == "disabled":
                continue
            try:
                self._target_resolver(member.execution_id)
            except ExecutionMissingError:
                self.members.delete(member.id)
                continue
            except ExecutionTargetError:
                pass  # not publishable yet; fall through to record health
            healthy, detail = await self._member_health(member)
            member.health_json = detail
            member.last_health_at = utcnow()
            member.status = "active" if healthy else "unhealthy"
            self.members.save(member)
            updated += 1
            if not healthy and _data_plane_degraded(detail.get("components") or {}):
                heal.add(member.cluster_id)
        for cluster_id in heal:
            await self._heal_data_plane(cluster_id)
        return updated

    async def _heal_data_plane(self, cluster_id: str) -> None:
        """Reconcile the shared Gateway and IPP when the data plane is broken.

        A stale ext_proc connection (for example after an IPP restart) leaves the
        resources Ready while every request fails. Re-applying the Gateway and
        reinstalling the IPP re-establishes it. Cooled down so a permanently
        broken cluster is not reconciled on every probe.
        """
        now = time.monotonic()
        last = self._last_heal.get(cluster_id)
        if last is not None and now - last < HEAL_COOLDOWN_SECONDS:
            return
        self._last_heal[cluster_id] = now
        # `created_by_user_id` is a foreign key into `users`; an automatic run
        # has no human actor, so it must be None (the same convention the
        # background reconcile loop uses), never a literal placeholder string.
        try:
            await self.install_cluster_gateway(cluster_id, actor=None)
        except Exception as error:  # noqa: BLE001 - best effort; the probe retries
            logger.warning("data-plane auto-heal gateway failed for %s: %s", cluster_id, error)
        try:
            await self.install_ipp(cluster_id, actor=None)
        except Exception as error:  # noqa: BLE001 - best effort; the probe retries
            logger.warning("data-plane auto-heal IPP failed for %s: %s", cluster_id, error)

    async def _member_health(self, member) -> tuple[bool, dict]:
        checked = utcnow().isoformat()
        try:
            target = self._target_resolver(member.execution_id)
        except ExecutionTargetError as error:
            return False, {
                "ready": False,
                "addresses": 0,
                "checkedAt": checked,
                "error": f"deployment not ready: {error}",
                "components": {
                    "httpRoute": "unknown",
                    "inferencePool": "unknown",
                    "epp": "missing",
                    "modelServer": "unknown",
                },
            }
        status = str(getattr(target, "status", "")).rsplit(".", 1)[-1].lower()

        # EPP: the member's target Service (the EPP) must have endpoints.
        epp_ready, addresses = await self._service_ready(
            member.cluster_id, member.target_service, member.target_namespace
        )
        components: dict[str, str] = {"epp": "ready" if epp_ready else "missing"}
        if member.endpoint_kind == "llm-d-epp":
            cluster = get_cluster(member.cluster_id)
            gateway_ns = (cluster.gateway_namespace if cluster else None) or DEFAULT_GATEWAY_NAMESPACE
            group = self.groups.get(member.group_id)
            route_name = f"{resource_name(group.name)}-{group.id}" if group else None
            components["httpRoute"] = await self._route_state(member.cluster_id, gateway_ns, route_name)
            pool_ns, pool_name = split_pool_name(member.pool_name) if member.pool_name else (None, None)
            pool_ns = pool_ns or member.target_namespace
            pool_state, selector = await self._pool_state(member.cluster_id, pool_ns, pool_name)
            components["inferencePool"] = pool_state
            components["modelServer"] = await self._model_server_state(member.cluster_id, pool_ns, selector)
            # The shared Gateway reaches the model only through the Inference
            # Payload Processor (ext_proc); a down IPP breaks every request even
            # though the route/pool/model look healthy.
            ipp_ready, _ = await self._service_ready(member.cluster_id, DEFAULT_IPP_NAME, gateway_ns)
            components["ipp"] = "ready" if ipp_ready else "missing"
            components["serving"] = await self._gateway_serving_state(member, group)
        else:
            # A plain vllm endpoint: no HTTPRoute/InferencePool; the target Service
            # itself is the model server.
            components["httpRoute"] = "n/a"
            components["inferencePool"] = "n/a"
            components["modelServer"] = components["epp"]
            components["serving"] = "n/a"

        ready = status == "ready" and all(state in ("ready", "n/a") for state in components.values())
        detail: dict = {
            "ready": ready,
            "addresses": addresses,
            "checkedAt": checked,
            "components": components,
        }
        if status != "ready":
            detail["error"] = f"deployment status is {status or 'unknown'}"
        return ready, detail

    async def _gateway_serving_state(self, member, group) -> str:
        """End-to-end probe of the shared Gateway data plane.

        The Inference Payload Processor (ext_proc) runs before authentication, so
        an unauthenticated probe that returns an auth error (401/403) proves the
        Gateway route, IPP ext_proc, InferencePool and EPP are wired. A 5xx or a
        transport failure means the data plane is broken (for example a stalled
        ext_proc connection after the IPP restarted) even when every resource is
        Ready -- the checks above would otherwise report all-green.
        """
        cluster = get_cluster(member.cluster_id)
        if cluster is None:
            return "n/a"
        global_url = os.environ.get("LENS_MODEL_GATEWAY_PUBLIC_URL", "").strip()
        base = gateway_base_url(cluster, local_public_ip(), global_url)
        if not base:
            return "n/a"
        model = (group.model_ref if group else None) or (group.name if group else None)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{base}/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                )
        except Exception:  # noqa: BLE001 - any transport error means unreachable
            return "unreachable"
        return "ready" if response.status_code < 500 else "degraded"

    async def _service_ready(self, cluster_id: str, service: str, namespace: str) -> tuple[bool, int]:
        """Whether a Service has ready endpoints (its own backing pods)."""
        try:
            result = await self._kubectl(
                cluster_id, ["get", "endpoints", service, "-n", namespace, "-o", "json"], timeout=15
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return False, 0
        if result.returncode != 0:
            return False, 0
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return False, 0
        addresses = sum(len(subset.get("addresses") or []) for subset in payload.get("subsets") or [])
        return addresses > 0, addresses

    async def _route_state(self, cluster_id: str, namespace: str, name: str | None) -> str:
        """``ready`` when the model's HTTPRoute exists and is Accepted."""
        if not name:
            return "unknown"
        try:
            result = await self._kubectl(
                cluster_id, ["get", "httproute", name, "-n", namespace, "-o", "json"], timeout=15
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown"
        if result.returncode != 0:
            return "missing"
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "degraded"
        for parent in (payload.get("status") or {}).get("parents") or []:
            for condition in parent.get("conditions") or []:
                if condition.get("type") == "Accepted" and condition.get("status") == "True":
                    return "ready"
        return "degraded"

    async def _pool_state(self, cluster_id: str, namespace: str, name: str | None) -> tuple[str, str | None]:
        """``(state, selector)`` for the member's InferencePool."""
        if not name:
            return "unknown", None
        try:
            result = await self._kubectl(
                cluster_id, ["get", "inferencepool", name, "-n", namespace, "-o", "json"], timeout=15
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown", None
        if result.returncode != 0:
            return "missing", None
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "degraded", None
        labels = ((payload.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        selector = ",".join(f"{key}={value}" for key, value in labels.items()) or None
        state = "degraded"
        for parent in (payload.get("status") or {}).get("parents") or []:
            if any(
                condition.get("type") == "Accepted" and condition.get("status") == "True"
                for condition in parent.get("conditions") or []
            ):
                state = "ready"
        return state, selector

    async def _model_server_state(self, cluster_id: str, namespace: str, selector: str | None) -> str:
        """``ready`` when the InferencePool's selector has a ready endpoint."""
        if not selector:
            return "missing"
        try:
            result = await self._kubectl(
                cluster_id, ["get", "endpointslices", "-n", namespace, "-l", selector, "-o", "json"], timeout=15
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown"
        if result.returncode != 0:
            return "missing"
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "unknown"
        for item in payload.get("items") or []:
            for endpoint in item.get("endpoints") or []:
                if endpoint.get("addresses") and (endpoint.get("conditions") or {}).get("ready", True):
                    return "ready"
        return "missing"

    # --- status --------------------------------------------------------------
    async def status(self) -> GatewayStatus:
        member_rows = self.members.list_all()
        by_cluster: dict[str, list[GatewayStatusMember]] = {}
        group_names = {group.id: group.name for group in self.groups.list()}
        for member in member_rows:
            by_cluster.setdefault(member.cluster_id, []).append(
                GatewayStatusMember(
                    group_id=member.group_id,
                    group_name=group_names.get(member.group_id, member.group_id),
                    execution_id=member.execution_id,
                    endpoint_kind=member.endpoint_kind,
                    pool_name=member_pool_name(member),
                    target=f"{member.target_service}.{member.target_namespace}:{member.target_port}",
                    status=member.status,
                )
            )
        from llm_d_bench.cluster.registry import list_clusters  # noqa: PLC0415

        all_clusters = list_clusters()
        cluster_names = {cluster.id: cluster.name for cluster in all_clusters}
        # Show a cluster whenever it has members OR a Gateway (a Gateway with no
        # deployments still exists and must stay visible/ operable).
        candidate_ids = set(by_cluster) | {
            cluster.id for cluster in all_clusters if getattr(cluster, "gateway_provider", None)
        }
        clusters: list[GatewayStatusCluster] = []
        for cluster_id in sorted(candidate_ids):
            cluster = get_cluster(cluster_id)
            namespace = (cluster.gateway_namespace if cluster else None) or DEFAULT_GATEWAY_NAMESPACE
            name = (cluster.gateway_name if cluster else None) or DEFAULT_GATEWAY_NAME
            state, ready = await self._gateway_state(cluster_id, namespace, name)
            # Only fill in an unset provider. Never overwrite the operator's
            # explicit choice with whatever Gateway happens to be installed: that
            # silently reverted a pending provider switch (e.g. envoy -> istio)
            # back to the old provider before the new install ran.
            provider = cluster.gateway_provider if cluster else None
            if state == "installed" and not provider:
                actual = await self._actual_gateway_provider(cluster_id, namespace, name)
                if actual:
                    provider = actual
                    if cluster is not None:
                        with suppress(Exception):
                            update_cluster(cluster_id, gateway_provider=actual)
            address = await self._gateway_address(cluster_id, namespace, name) if ready else None
            node_address = (
                await self._node_address(cluster_id)
                if cluster is not None and getattr(cluster, "gateway_port", None)
                else None
            )
            ipp_state, _ = await self._deployment_state(cluster_id, namespace, DEFAULT_IPP_NAME)
            last = self.operations.latest_for_kind("reconcile-gateway", cluster_id=cluster_id)
            clusters.append(
                GatewayStatusCluster(
                    cluster_id=cluster_id,
                    cluster_name=cluster_names.get(cluster_id, ""),
                    members=by_cluster.get(cluster_id, []),
                    gateway_provider=provider,
                    gateway_state=state,
                    gateway_namespace=namespace,
                    gateway_name=name,
                    gateway_ready=ready,
                    gateway_address=address,
                    gateway_public_url=(getattr(cluster, "gateway_public_url", None) if cluster else None),
                    gateway_port=(getattr(cluster, "gateway_port", None) if cluster else None),
                    gateway_node_address=node_address,
                    ipp_state=ipp_state,
                    ipp_name=DEFAULT_IPP_NAME,
                    ipp_namespace=namespace,
                    last_operation=last,
                )
            )
        return GatewayStatus(clusters=clusters)

    async def cluster_gateway_base_url(self, cluster_id: str | None) -> str | None:
        """One cluster's shared-Gateway base URL (``.../v1``), or ``None``.

        In-cluster consumers (Simulation/Evaluate) resolve a deployment's
        benchmark endpoint through this URL so traffic keeps flowing through the
        shared Gateway and the deployment's EPP instead of the disabled
        per-deployment proxy.
        """
        if not cluster_id:
            return None
        global_url = os.environ.get("LENS_MODEL_GATEWAY_PUBLIC_URL", "").strip()
        status = await self.status()
        for cluster in status.clusters:
            if cluster.cluster_id != cluster_id:
                continue
            if not cluster.gateway_ready:
                return None
            return gateway_base_url(cluster, local_public_ip(), global_url) or None
        return None
