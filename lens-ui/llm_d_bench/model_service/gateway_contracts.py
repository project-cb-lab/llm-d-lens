"""Contracts for model-service gateway operations.

Gateway Mode only: one shared Gateway per cluster (provider-selectable) plus
per-model HTTPRoute/IPP reconciliation. Design reference:
docs/design/model-service-llmd-routing-design.zh-CN.md sections 5/10.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

GatewayOperationStatus = Literal["running", "succeeded", "failed"]
#: Shared-Gateway presence on a cluster, read from the Gateway resource.
GatewayRuntimeState = Literal["installed", "missing", "unknown"]

#: Operation kinds the current code emits. ``kind`` is stored as a plain string so
#: historical rows still deserialize instead of failing validation.
KNOWN_OPERATION_KINDS = (
    "preflight",
    "install-gateway",
    "uninstall-gateway",
    "reconcile-gateway",
    "install-ipp",
    "uninstall-ipp",
    "component-scale",
)
LEGACY_OPERATION_KINDS = (
    "install-leaf",
    "uninstall-leaf",
    "reload-edge",
    "edge-start",
    "edge-stop",
    "edge-configure",
    "router-start",
    "router-stop",
    "install-agent-router",
    "uninstall-agent-router",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class GatewayOperation(StrictModel):
    """A recorded gateway operation (audit + UI status)."""

    id: str = Field(default_factory=lambda: f"gop-{uuid4().hex[:12]}")
    kind: str
    status: GatewayOperationStatus = "running"
    cluster_id: str | None = Field(default=None, alias="clusterId")
    message: str = ""
    detail_json: dict[str, Any] | None = Field(default=None, alias="detailJson")
    created_by_user_id: str | None = Field(default=None, alias="createdByUserId")
    created_at: datetime = Field(default_factory=utcnow, alias="createdAt")
    finished_at: datetime | None = Field(default=None, alias="finishedAt")

    def api_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ComponentLogsRequest(StrictModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    cluster_id: str = Field(alias="clusterId")
    #: ``gateway`` / ``ipp`` let the backend resolve the component's coordinates.
    component: Literal["gateway", "ipp"] | None = None
    namespace: str | None = Field(default=None, max_length=253)
    name: str | None = Field(default=None, max_length=253)
    kind: str = Field(default="deployment", max_length=32)
    container: str | None = Field(default=None, max_length=253)
    tail: int = Field(default=200, ge=1, le=2000)


class ClusterGatewayRequest(StrictModel):
    """Install/reconcile the shared Gateway on a cluster."""

    cluster_id: str = Field(alias="clusterId")
    provider: str | None = None
    namespace: str = Field(default="lens-gateway", max_length=253)
    name: str = Field(default="lens-inference-gateway", max_length=253)
    install_prerequisites: bool = Field(default=True, alias="installPrerequisites")


class ComponentScaleRequest(StrictModel):
    """Scale a data-plane component Deployment (Gateway data plane / EPP / model server)."""

    cluster_id: str = Field(alias="clusterId")
    namespace: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=253)
    replicas: int = Field(ge=0, le=100)
    kind: str = Field(default="deployment", max_length=32)


class IppConfigRequest(StrictModel):
    """Set a cluster's Inference Payload Processor ``PayloadProcessorConfig`` body."""

    cluster_id: str = Field(alias="clusterId")
    config: str = ""


class GatewayStreamRequest(StrictModel):
    """A streamed data-plane operation; its log lines are relayed as SSE."""

    op: Literal[
        "install-gateway",
        "uninstall-gateway",
        "reconcile",
        "scale",
        "install-ipp",
        "uninstall-ipp",
        "gateway-start",
        "gateway-stop",
    ]
    cluster_id: str = Field(alias="clusterId")
    provider: str | None = None
    namespace: str | None = None
    name: str | None = None
    replicas: int | None = Field(default=None, ge=0, le=100)
    kind: str = Field(default="deployment", max_length=32)
    gateway_name: str | None = Field(default=None, alias="gatewayName")
    ipp_chart: str | None = Field(default=None, alias="ippChart")
    ipp_version: str | None = Field(default=None, alias="ippVersion")


class GatewayStatusMember(StrictModel):
    group_id: str = Field(alias="groupId")
    group_name: str = Field(alias="groupName")
    execution_id: str = Field(alias="executionId")
    endpoint_kind: str = Field(alias="endpointKind")
    pool_name: str | None = Field(default=None, alias="poolName")
    target: str
    status: str


class GatewayStatusCluster(StrictModel):
    cluster_id: str = Field(alias="clusterId")
    cluster_name: str = Field(default="", alias="clusterName")
    members: list[GatewayStatusMember] = Field(default_factory=list)
    gateway_provider: str | None = Field(default=None, alias="gatewayProvider")
    gateway_state: GatewayRuntimeState = Field(default="unknown", alias="gatewayState")
    gateway_namespace: str | None = Field(default=None, alias="gatewayNamespace")
    gateway_name: str | None = Field(default=None, alias="gatewayName")
    gateway_ready: bool | None = Field(default=None, alias="gatewayReady")
    #: The Gateway's published address (node IP / LoadBalancer hostname), when assigned.
    gateway_address: str | None = Field(default=None, alias="gatewayAddress")
    #: Operator-set externally reachable URL (overrides the in-cluster address).
    gateway_public_url: str | None = Field(default=None, alias="gatewayPublicUrl")
    #: Configured public NodePort the Gateway is exposed on (clients use nodeIP:port).
    gateway_port: int | None = Field(default=None, alias="gatewayPort")
    #: A node InternalIP to pair with ``gatewayPort`` when the Gateway address is a hostname.
    gateway_node_address: str | None = Field(default=None, alias="gatewayNodeAddress")
    #: Inference Payload Processor (per Gateway) runtime state; ``missing`` when
    #: no IPP is deployed for the cluster's Gateway.
    ipp_state: GatewayRuntimeState = Field(default="unknown", alias="ippState")
    ipp_name: str | None = Field(default=None, alias="ippName")
    ipp_namespace: str | None = Field(default=None, alias="ippNamespace")
    last_operation: GatewayOperation | None = Field(default=None, alias="lastOperation")


class GatewayStatus(StrictModel):
    clusters: list[GatewayStatusCluster] = Field(default_factory=list)

    def api_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)
