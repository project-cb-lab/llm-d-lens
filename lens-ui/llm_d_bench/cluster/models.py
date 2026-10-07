"""Request and response DTOs for the cluster management and overview API."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- Cluster registry DTOs ---


class ProxyConfigDTO(StrictModel):
    """See docs/design/cluster-creation-wizard-design.md section 4.2.

    ``mode="auto"`` today falls back to whatever proxy env vars the Prism
    backend process itself sees (auto-detection of the target cluster's own
    proxy is not implemented yet -- see the design doc's Phase 2). ``custom``
    uses the three values below verbatim for this cluster only.
    """

    mode: Literal["auto", "custom"] = "auto"
    http_proxy: str | None = Field(default=None, alias="httpProxy")
    https_proxy: str | None = Field(default=None, alias="httpsProxy")
    no_proxy: str | None = Field(default=None, alias="noProxy")


class ClusterRecord(StrictModel):
    id: str
    name: str
    description: str
    created_at: str = Field(alias="createdAt")
    ready: bool = False
    proxy: ProxyConfigDTO = Field(default_factory=ProxyConfigDTO)
    llm_d_ref: str | None = Field(default=None, alias="llmDRef")
    llm_d_benchmark_ref: str | None = Field(default=None, alias="llmDBenchmarkRef")
    llm_d_repo_path: str | None = Field(default=None, alias="llmDRepoPath")
    llm_d_benchmark_repo_path: str | None = Field(default=None, alias="llmDBenchmarkRepoPath")
    benchmark_source: dict | None = Field(default=None, alias="benchmarkSource")
    deployment_source: dict | None = Field(default=None, alias="deploymentSource")
    gateway_provider: str | None = Field(default=None, alias="gatewayProvider")
    gateway_namespace: str | None = Field(default=None, alias="gatewayNamespace")
    gateway_name: str | None = Field(default=None, alias="gatewayName")
    gateway_public_url: str | None = Field(default=None, alias="gatewayPublicUrl")
    gateway_port: int | None = Field(default=None, alias="gatewayPort")
    gateway_authz_host: str | None = Field(default=None, alias="gatewayAuthzHost")
    inotify_max_user_instances: int = Field(default=8192, alias="inotifyMaxUserInstances")
    router_version: str | None = Field(default=None, alias="routerVersion")
    gie_version: str | None = Field(default=None, alias="gieVersion")
    ipp_version: str | None = Field(default=None, alias="ippVersion")
    draft: bool = False


class ImagePrepullRequest(StrictModel):
    """Accelerators whose llm-d images should be pre-pulled onto every node."""

    accelerators: list[str] = Field(default_factory=list)


class ClusterSettingsUpdateRequest(StrictModel):
    """``PATCH /api/cluster/clusters/{id}`` payload; every field is optional,
    only supplied keys are changed. Covers both the wizard's Step 2/3 settings
    (plus the wizard's final "Finish" step flipping ``draft`` to ``False``)
    and the Clusters overview page's "Edit cluster" action (name/description/
    proxy/version refs). ``kubeconfig`` stays immutable for now."""

    name: str | None = Field(default=None, min_length=1, max_length=253)
    description: str | None = None
    proxy: ProxyConfigDTO | None = None
    llm_d_ref: str | None = Field(default=None, alias="llmDRef")
    llm_d_benchmark_ref: str | None = Field(default=None, alias="llmDBenchmarkRef")
    gateway_provider: str | None = Field(default=None, alias="gatewayProvider")
    gateway_namespace: str | None = Field(default=None, alias="gatewayNamespace")
    gateway_name: str | None = Field(default=None, alias="gatewayName")
    gateway_public_url: str | None = Field(default=None, alias="gatewayPublicUrl")
    gateway_port: int | None = Field(default=None, alias="gatewayPort")
    gateway_authz_host: str | None = Field(default=None, alias="gatewayAuthzHost")
    inotify_max_user_instances: int | None = Field(default=None, ge=1, le=10_000_000, alias="inotifyMaxUserInstances")
    router_version: str | None = Field(default=None, alias="routerVersion")
    gie_version: str | None = Field(default=None, alias="gieVersion")
    ipp_version: str | None = Field(default=None, alias="ippVersion")
    draft: bool | None = None


class HfTokenSecretCreateRequest(StrictModel):
    """Standalone "create an HF_TOKEN secret" request, decoupled from any
    Model Cache entry -- see docs/design/cluster-creation-wizard-design.md
    section 4.4. The resulting secret is then referenced by a normal Model
    Cache ``TokenSourceMode.EXISTING_SECRET`` token source; no new token
    source mode is introduced."""

    namespace: str = Field(min_length=1, max_length=253)
    name: str = Field(default_factory=lambda: f"hf-token-{uuid4().hex[:8]}", max_length=253)
    token: str = Field(min_length=1)


class HfTokenSecretRef(StrictModel):
    namespace: str
    name: str


class SoftwareDownloadRequest(StrictModel):
    """``POST /api/cluster/clusters/{id}/software-downloads`` payload.

    Kicks off (or reuses a cached) download for whichever refs are supplied;
    a ``None``/blank ref leaves that repo's download untouched."""

    llm_d_ref: str | None = Field(default=None, alias="llmDRef")
    llm_d_benchmark_ref: str | None = Field(default=None, alias="llmDBenchmarkRef")


class RepoDownloadStatus(StrictModel):
    ref: str = ""
    state: Literal["idle", "downloading", "ready", "failed"] = "idle"
    path: str | None = None
    error: str | None = None


class SoftwareDownloadStatusResponse(StrictModel):
    llm_d: RepoDownloadStatus = Field(alias="llmD", default_factory=RepoDownloadStatus)
    llm_d_benchmark: RepoDownloadStatus = Field(alias="llmDBenchmark", default_factory=RepoDownloadStatus)


class ClustersResponse(StrictModel):
    items: list[ClusterRecord] = Field(default_factory=list)


class ClusterSessionResponse(StrictModel):
    cluster: ClusterRecord
    session_id: str = Field(alias="sessionId")


# --- Node maintenance (cordon/uncordon) DTOs ---


class NodeMaintenanceRequest(StrictModel):
    names: list[str] = Field(min_length=1, max_length=256)


class NodeMaintenanceResult(StrictModel):
    name: str
    ok: bool
    scheduling_disabled: bool | None = Field(default=None, alias="schedulingDisabled")
    error: str | None = None


class NodeMaintenanceResponse(StrictModel):
    items: list[NodeMaintenanceResult] = Field(default_factory=list)


class CreateClusterResponse(StrictModel):
    cluster: ClusterRecord
    session_id: str | None = Field(default=None, alias="sessionId")


# --- Kubernetes overview DTOs ---


class ComponentSummary(StrictModel):
    installed: bool
    ready: int
    desired: int
    status: str


class ComponentsSummary(StrictModel):
    intel_device_plugin: ComponentSummary = Field(alias="intelDevicePlugin")
    monitoring: ComponentSummary
    accelerator_device_plugins: dict[str, ComponentSummary] = Field(
        default_factory=dict, alias="acceleratorDevicePlugins"
    )


class GpuDeviceSummary(StrictModel):
    id: str
    index: int = 0
    name: str
    pci_address: str = Field(alias="pciAddress")
    compute_usage_percent: float = Field(default=0.0, alias="computeUsagePercent")
    vram_total_bytes: int = Field(default=0, alias="vramTotalBytes")
    vram_used_bytes: int = Field(default=0, alias="vramUsedBytes")
    vram_usage_percent: float = Field(default=0.0, alias="vramUsagePercent")
    vram_read_throughput_bytes: int = Field(default=0, alias="vramReadThroughputBytes")
    vram_write_throughput_bytes: int = Field(default=0, alias="vramWriteThroughputBytes")
    vram_total_throughput_bytes: int = Field(default=0, alias="vramTotalThroughputBytes")
    vram_bandwidth_percent: float = Field(default=0.0, alias="vramBandwidthPercent")


class NodeSummary(StrictModel):
    name: str
    version: str
    os_image: str = Field(alias="osImage")
    ready: bool
    scheduling_disabled: bool = Field(default=False, alias="schedulingDisabled")
    cpu: float = 0.0
    cpu_usage_percent: float | None = Field(default=None, alias="cpuUsagePercent")
    memory_bytes: int = Field(default=0, alias="memoryBytes")
    memory_usage_percent: float | None = Field(default=None, alias="memoryUsagePercent")
    gpu: bool = False
    gpu_count: int = Field(default=0, alias="gpuCount")
    gpu_by_profile: dict[str, int] = Field(default_factory=dict, alias="gpuByProfile")
    gpu_usage_percent: float | None = Field(default=None, alias="gpuUsagePercent")
    vram_bytes: int = Field(default=0, alias="vramBytes")
    vram_usage_percent: float | None = Field(default=None, alias="vramUsagePercent")
    role: str = "worker"
    gpus: list[GpuDeviceSummary] = Field(default_factory=list)
    disk_bytes: int = Field(default=0, alias="diskBytes")
    disk_used_bytes: int | None = Field(default=None, alias="diskUsedBytes")
    disk_usage_percent: float | None = Field(default=None, alias="diskUsagePercent")
    cached_images: list[str] = Field(default_factory=list, alias="cachedImages")


class AcceleratorBucket(StrictModel):
    """Per-vendor accelerator count in the cluster hardware summary."""

    id: str
    gpu_count: int = Field(default=0, alias="gpuCount")


class ClusterHardwareSummary(StrictModel):
    nodes: int = 0
    cpu_cores: float = Field(default=0.0, alias="cpuCores")
    cpu_usage_percent: float | None = Field(default=None, alias="cpuUsagePercent")
    memory_bytes: int = Field(default=0, alias="memoryBytes")
    memory_usage_percent: float | None = Field(default=None, alias="memoryUsagePercent")
    gpu_nodes: int = Field(default=0, alias="gpuNodes")
    gpu_count: int = Field(default=0, alias="gpuCount")
    total_gpu_count: int = Field(default=0, alias="totalGpuCount")
    accelerators: list[AcceleratorBucket] = Field(default_factory=list)
    # Per-GPU payload keys the cluster's hardware profiles actually report;
    # the UI hides sections for metrics absent from this list.
    device_metrics: list[str] = Field(default_factory=list, alias="deviceMetrics")
    gpu_usage_percent: float | None = Field(default=None, alias="gpuUsagePercent")
    vram_bytes: int = Field(default=0, alias="vramBytes")
    vram_usage_percent: float | None = Field(default=None, alias="vramUsagePercent")
    disk_bytes: int = Field(default=0, alias="diskBytes")
    disk_usage_percent: float | None = Field(default=None, alias="diskUsagePercent")
    allocated_gpu_count: int = Field(default=0, alias="allocatedGpuCount")
    available_gpu_count: int = Field(default=0, alias="availableGpuCount")
    usable_gpu_count: int = Field(default=0, alias="usableGpuCount")
    configured_gpu_limit: int | None = Field(default=None, alias="configuredGpuLimit")


class DeploymentSummary(StrictModel):
    name: str
    desired: int
    ready: int
    available: int


class NamespaceSummary(StrictModel):
    name: str
    deployments: list[DeploymentSummary] = Field(default_factory=list)
    ready: int


class KubernetesSummary(StrictModel):
    components: ComponentsSummary
    nodes: list[NodeSummary] = Field(default_factory=list)
    namespaces: list[NamespaceSummary] = Field(default_factory=list)
    hardware: ClusterHardwareSummary = Field(default_factory=ClusterHardwareSummary)


class OverviewResponse(StrictModel):
    cluster: ClusterRecord
    fetched_at: str = Field(alias="fetchedAt")
    kubernetes: KubernetesSummary
