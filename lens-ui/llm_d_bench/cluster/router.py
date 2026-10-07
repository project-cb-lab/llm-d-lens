"""HTTP routes for cluster management (kubeconfig upload) and overview."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, Request, Response, UploadFile
from pydantic import ValidationError

from llm_d_bench.auth.access import current_principal, filter_by_cluster
from llm_d_bench.cluster import gateway_crds, images, registry, repo_downloads, service, sessions
from llm_d_bench.cluster.deployment_source import resolve_cluster_benchmark_source, resolve_cluster_deployment_source
from llm_d_bench.cluster.errors import ClusterOverviewError
from llm_d_bench.cluster.models import (
    ClusterRecord,
    ClusterSessionResponse,
    ClusterSettingsUpdateRequest,
    ClustersResponse,
    CreateClusterResponse,
    HfTokenSecretCreateRequest,
    HfTokenSecretRef,
    ImagePrepullRequest,
    KubernetesSummary,
    NodeMaintenanceRequest,
    NodeMaintenanceResponse,
    OverviewResponse,
    ProxyConfigDTO,
    RepoDownloadStatus,
    SoftwareDownloadRequest,
    SoftwareDownloadStatusResponse,
)
from llm_d_bench.cluster.sdk_discovery import router as planning_discovery_router
from llm_d_bench.utils.hostinfo import local_host_addresses
from llm_d_bench.versions import stack as stack_profile

router = APIRouter(prefix="/api/cluster", tags=["cluster"])
router.include_router(planning_discovery_router)
legacy_router = APIRouter(prefix="/api/cluster-overview", tags=["cluster-overview"])

logger = logging.getLogger(__name__)


@router.get(
    "/lens-hosts",
    summary="Candidate Lens host addresses a cluster can use for Gateway ext_authz.",
    description=(
        "Returns this Lens host's candidate IP addresses/hostnames so the cluster create/edit form can offer and "
        "auto-fill a reachable address for per-request identity injection. The port is Lens' own live serving "
        "port, added automatically."
    ),
    operation_id="list_lens_hosts",
)
async def list_lens_hosts() -> dict:
    """Candidate host/IP for the cluster's Lens address setting."""
    return {"items": local_host_addresses()}


def _default_lens_host() -> str | None:
    """The first reachable host/IP for this Lens backend, auto-filled into the
    per-cluster "Lens address" when the create/edit form (or an API caller)
    leaves it empty. The authz port is added by Lens from its own serving port."""
    hosts = local_host_addresses()
    return hosts[0] if hosts else None


@router.get(
    "/clusters/{cluster_id}/gateway-port-check",
    summary="Check a Gateway exposed port (NodePort) is free.",
    operation_id="check_gateway_port",
)
async def check_gateway_port(cluster_id: str, port: int = Query(ge=0, le=65535)) -> dict:
    """Whether ``port`` can be used as the cluster Gateway's NodePort."""
    if not (30000 <= port <= 32767):
        return {"available": False, "owner": None, "reason": "port must be between 30000 and 32767"}
    from llm_d_bench.model_service.gateway_ops import GatewayOpsService  # noqa: PLC0415

    owner = await GatewayOpsService().node_port_owner(cluster_id, port)
    return {"available": owner is None, "owner": f"{owner[0]}/{owner[1]}" if owner else None}


async def _install_cluster_gateway(cluster_id: str, provider: str, *, install_prerequisites: bool = True) -> None:
    """Install/reconcile the shared llm-d Gateway for a cluster (best effort).

    The data plane is installed once at cluster creation; later edits (provider,
    exposed port, Lens address) re-render it. ``install_prerequisites`` is skipped
    for changes that only touch the Gateway service (e.g. the exposed port).
    """
    from llm_d_bench.model_service.gateway_ops import GatewayOpsService  # noqa: PLC0415

    try:
        await GatewayOpsService().install_inference_gateway(
            cluster_id, provider, install_prerequisites=install_prerequisites
        )
    except Exception:  # noqa: BLE001 - background best effort; failures are recorded as operations
        logger.exception("event=cluster_gateway_install_failed cluster_id=%s provider=%s", cluster_id, provider)


async def _apply_node_sysctl(cluster_id: str, limits: int | None) -> None:
    """Apply the cluster's node inotify limit on every node (best effort)."""
    if not limits:
        return
    from llm_d_bench.model_service.gateway_ops import GatewayOpsService  # noqa: PLC0415

    try:
        await GatewayOpsService().apply_node_inotify_limit(cluster_id, limits)
    except Exception:  # noqa: BLE001 - background best effort
        logger.exception("event=cluster_node_sysctl_failed cluster_id=%s limit=%s", cluster_id, limits)


def _record(cluster: registry.Cluster, *, ready: bool = False) -> ClusterRecord:
    try:
        source = resolve_cluster_deployment_source(cluster)
    except ValueError as error:
        source = {"resolved_from": "unavailable", "error": str(error)}
    try:
        benchmark_source = resolve_cluster_benchmark_source(cluster)
    except ValueError as error:
        benchmark_source = {"resolved_from": "unavailable", "error": str(error)}
    return ClusterRecord(
        id=cluster.id,
        name=cluster.name,
        description=cluster.description,
        created_at=cluster.created_at,
        ready=ready,
        proxy=ProxyConfigDTO(
            mode=cluster.proxy_mode,
            httpProxy=cluster.http_proxy,
            httpsProxy=cluster.https_proxy,
            noProxy=cluster.no_proxy,
        ),
        llmDRef=cluster.llm_d_ref,
        llmDBenchmarkRef=cluster.llm_d_benchmark_ref,
        llmDRepoPath=cluster.llm_d_repo_path,
        llmDBenchmarkRepoPath=cluster.llm_d_benchmark_repo_path,
        gatewayProvider=cluster.gateway_provider,
        gatewayNamespace=cluster.gateway_namespace,
        gatewayName=cluster.gateway_name,
        gatewayPublicUrl=cluster.gateway_public_url,
        gatewayPort=cluster.gateway_port,
        gatewayAuthzHost=cluster.gateway_authz_host,
        inotifyMaxUserInstances=cluster.inotify_max_user_instances,
        routerVersion=cluster.router_version,
        gieVersion=cluster.gie_version,
        ippVersion=cluster.ipp_version,
        draft=cluster.draft,
        deploymentSource=source,
        benchmarkSource=benchmark_source,
    )


def _inactive() -> ClusterOverviewError:
    return ClusterOverviewError("Cluster session is no longer active", status_code=404, code="cluster_session_inactive")


@router.get(
    "/clusters",
    response_model=ClustersResponse,
    summary=("List Lens-managed clusters, optionally including draft records that have not yet been finalized."),
    description=(
        "List Lens-managed clusters, optionally including draft records that have not yet been finalized. "
        "Use this to discover cluster ids before other cluster-scoped calls."
    ),
    operation_id="list_clusters",
)
async def list_clusters(
    request: Request, include_drafts: bool = Query(default=False, alias="includeDrafts")
) -> ClustersResponse:
    clusters = service.list_clusters(include_drafts=include_drafts)
    clusters = filter_by_cluster(clusters, lambda cluster: cluster.id, current_principal(request))
    ready_flags = await asyncio.gather(*(service.cluster_is_ready(cluster) for cluster in clusters))
    return ClustersResponse(
        items=[_record(cluster, ready=is_ready) for cluster, is_ready in zip(clusters, ready_flags, strict=False)],
    )


@router.post(
    "/clusters",
    response_model=CreateClusterResponse,
    status_code=201,
    summary="Register a cluster from an uploaded kubeconfig.",
    description=(
        "Register a cluster using multipart form data with a name and UTF-8 kubeconfig file, plus optional "
        "description, proxy settings, software refs, and draft flag. Returns the cluster record and a session id "
        "when a session is created."
    ),
    operation_id="create_cluster",
)
async def create_cluster(
    background: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    name: str = Form(..., min_length=1, max_length=128),
    description: str = Form(default="", max_length=4096),
    proxy: str | None = Form(default=None),
    llm_d_ref: str | None = Form(default=None, alias="llmDRef"),
    llm_d_benchmark_ref: str | None = Form(default=None, alias="llmDBenchmarkRef"),
    gateway_provider: str | None = Form(default=None, alias="gatewayProvider"),
    gateway_public_url: str | None = Form(default=None, alias="gatewayPublicUrl"),
    gateway_port: int | None = Form(default=None, alias="gatewayPort"),
    gateway_authz_host: str | None = Form(default=None, alias="gatewayAuthzHost"),
    inotify_max_user_instances: int | None = Form(default=None, alias="inotifyMaxUserInstances"),
    draft: bool = Form(default=False),
) -> CreateClusterResponse:
    raw = await file.read()
    try:
        kubeconfig_text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ClusterOverviewError(
            "Kubeconfig must be UTF-8 text", status_code=422, code="invalid_kubeconfig"
        ) from error
    proxy_config = _parse_proxy_form_field(proxy)
    cluster, session = service.create_cluster(
        name,
        description,
        kubeconfig_text,
        proxy_mode=proxy_config.mode,
        http_proxy=proxy_config.http_proxy,
        https_proxy=proxy_config.https_proxy,
        no_proxy=proxy_config.no_proxy,
        llm_d_ref=(llm_d_ref or "").strip() or None,
        llm_d_benchmark_ref=(llm_d_benchmark_ref or "").strip() or None,
        gateway_provider=(gateway_provider or "").strip() or None,
        gateway_public_url=(gateway_public_url or "").strip() or None,
        gateway_port=gateway_port,
    gateway_authz_host=(gateway_authz_host or "").strip() or _default_lens_host(),
        inotify_max_user_instances=(inotify_max_user_instances if inotify_max_user_instances is not None else 8192),
        draft=draft,
    )
    if cluster.gateway_provider:
        background.add_task(_install_cluster_gateway, cluster.id, cluster.gateway_provider)
    background.add_task(_apply_node_sysctl, cluster.id, cluster.inotify_max_user_instances)
    return CreateClusterResponse(cluster=_record(cluster), session_id=session.id if session else None)


def _parse_proxy_form_field(raw: str | None) -> ProxyConfigDTO:
    if not raw or not raw.strip():
        return ProxyConfigDTO()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ClusterOverviewError("proxy must be valid JSON", status_code=422, code="invalid_proxy_config") from error
    try:
        return ProxyConfigDTO.model_validate(payload)
    except ValidationError as error:
        raise ClusterOverviewError(str(error), status_code=422, code="invalid_proxy_config") from error


@router.patch(
    "/clusters/{cluster_id}",
    response_model=CreateClusterResponse,
    summary=(
        "Patch a cluster\\'s editable settings such as name, description, proxy config, or pinned llm-d revisions."
    ),
    description=(
        "Patch a cluster\\'s editable settings such as name, description, proxy config, or pinned llm-d revisions. "
        "Use this for draft completion or later cluster maintenance."
    ),
    operation_id="update_cluster",
)
async def update_cluster(
    cluster_id: str, payload: ClusterSettingsUpdateRequest, background: BackgroundTasks
) -> CreateClusterResponse:
    """Patch a cluster's name/description/proxy/version-pin settings (wizard Step 2/3,
    or the Clusters overview page's "Edit cluster" action)."""
    prior = registry.get_cluster(cluster_id)
    # An explicitly empty Lens address means "auto": fill in this host's
    # reachable IP. The authz port is added later from Lens' own serving port.
    # ``None`` (omitted) leaves the stored value untouched.
    authz_host = payload.gateway_authz_host
    if authz_host is not None and not authz_host.strip():
        authz_host = _default_lens_host()
    provider_changed = payload.gateway_provider is not None and payload.gateway_provider != (
        prior.gateway_provider if prior else None
    )
    gateway_changed = provider_changed or any(
        field is not None for field in (payload.gateway_namespace, payload.gateway_name, authz_host)
    )
    # A changed exposed port must take effect immediately: validate it, free the old
    # tunnel, then re-render the Gateway Service (NodePort) and re-expose it.
    if payload.gateway_port is not None:
        if not (30000 <= payload.gateway_port <= 32767):
            raise ClusterOverviewError(
                "Gateway port must be between 30000 and 32767", status_code=422, code="invalid_gateway_port"
            )
        if prior is None or payload.gateway_port != prior.gateway_port:
            from llm_d_bench.model_service.gateway_ops import GatewayOpsService  # noqa: PLC0415

            ops = GatewayOpsService()
            owner = await ops.node_port_owner(cluster_id, payload.gateway_port)
            if owner:
                raise ClusterOverviewError(
                    f"Gateway port {payload.gateway_port} is already used by {owner[0]}/{owner[1]}",
                    status_code=422,
                    code="gateway_port_in_use",
                )
            if prior is not None:
                await ops.release_gateway_port(cluster_id, prior.gateway_port)
        gateway_changed = True
    cluster = service.update_cluster_settings(
        cluster_id,
        name=payload.name,
        description=payload.description,
        proxy_mode=payload.proxy.mode if payload.proxy is not None else None,
        http_proxy=payload.proxy.http_proxy if payload.proxy is not None else None,
        https_proxy=payload.proxy.https_proxy if payload.proxy is not None else None,
        no_proxy=payload.proxy.no_proxy if payload.proxy is not None else None,
        llm_d_ref=payload.llm_d_ref,
        llm_d_benchmark_ref=payload.llm_d_benchmark_ref,
        gateway_provider=payload.gateway_provider,
        gateway_namespace=payload.gateway_namespace,
        gateway_name=payload.gateway_name,
        gateway_public_url=payload.gateway_public_url,
        gateway_port=payload.gateway_port,
        gateway_authz_host=authz_host,
        inotify_max_user_instances=payload.inotify_max_user_instances,
        router_version=payload.router_version,
        gie_version=payload.gie_version,
        ipp_version=payload.ipp_version,
        draft=payload.draft,
    )
    # Re-render/reconcile the shared Gateway when a data-plane setting changed.
    if gateway_changed and cluster.gateway_provider:
        background.add_task(
            _install_cluster_gateway,
            cluster.id,
            cluster.gateway_provider,
            install_prerequisites=provider_changed,
        )
    # Applying the node inotify limit is idempotent, so re-apply whenever the
    # client sends it (also lets an existing cluster pick it up on any save).
    if payload.inotify_max_user_instances is not None:
        background.add_task(_apply_node_sysctl, cluster.id, cluster.inotify_max_user_instances)
    is_ready = await service.cluster_is_ready(cluster)
    return CreateClusterResponse(cluster=_record(cluster, ready=is_ready))


def _download_status_response(cluster_id: str) -> SoftwareDownloadStatusResponse:
    llm_d = repo_downloads.get_status(cluster_id, "llm-d")
    llm_d_benchmark = repo_downloads.get_status(cluster_id, "llm-d-benchmark")
    return SoftwareDownloadStatusResponse(
        llmD=RepoDownloadStatus(ref=llm_d.ref, state=llm_d.state, path=llm_d.path, error=llm_d.error),
        llmDBenchmark=RepoDownloadStatus(
            ref=llm_d_benchmark.ref, state=llm_d_benchmark.state, path=llm_d_benchmark.path, error=llm_d_benchmark.error
        ),
    )


async def _download_and_record(cluster_id: str, repo: repo_downloads.RepoName, ref: str) -> None:
    try:
        path = await repo_downloads.ensure_downloaded(cluster_id, repo, ref)
    except Exception:
        # Failure is already captured in repo_downloads' in-memory status
        # (polled via GET below); nothing further to persist on the cluster.
        return
    if repo == "llm-d":
        service.record_repo_download(cluster_id, llm_d_ref=ref, llm_d_repo_path=str(path))
    else:
        service.record_repo_download(cluster_id, llm_d_benchmark_ref=ref, llm_d_benchmark_repo_path=str(path))


@router.post(
    "/clusters/{cluster_id}/software-downloads",
    response_model=SoftwareDownloadStatusResponse,
    status_code=202,
    summary="Start downloading the llm-d and/or llm-d-benchmark source refs attached to a cluster.",
    description=(
        "Start downloading the llm-d and/or llm-d-benchmark source refs attached to a cluster. "
        "Use this after pinning software versions on a cluster record."
    ),
    operation_id="create_cluster_software_downloads",
)
async def create_software_downloads(
    cluster_id: str, payload: SoftwareDownloadRequest
) -> SoftwareDownloadStatusResponse:
    """Kick off real downloads of the llm-d / llm-d-benchmark refs selected in the
    wizard's Software Versions step (cached under ``~/.cache/lens/repos``;
    see ``llm_d_bench.cluster.repo_downloads``). Poll via the GET below."""
    registry.require_cluster(cluster_id)
    # The downloaded revisions always follow the Lens stack profile; the request
    # body is ignored so a caller cannot pin another revision.
    current = stack_profile()
    asyncio.create_task(_download_and_record(cluster_id, "llm-d", current.llm_d))
    asyncio.create_task(_download_and_record(cluster_id, "llm-d-benchmark", current.llm_d_benchmark))
    return _download_status_response(cluster_id)


@router.get(
    "/clusters/{cluster_id}/software-downloads",
    response_model=SoftwareDownloadStatusResponse,
    summary="Get the current llm-d and llm-d-benchmark download status for a cluster.",
    description=(
        "Get the current llm-d and llm-d-benchmark download status for a cluster. "
        "Use this to poll cluster software preparation progress."
    ),
    operation_id="get_cluster_software_downloads",
)
async def read_software_downloads(cluster_id: str) -> SoftwareDownloadStatusResponse:
    registry.require_cluster(cluster_id)
    return _download_status_response(cluster_id)


@router.post(
    "/clusters/{cluster_id}/images/prepull",
    summary="Pre-pull the llm-d images for the selected accelerators onto every node.",
    description=(
        "Start a privileged DaemonSet that pulls the pinned llm-d-router (EPP, P/D sidecar) and "
        "model-server images for the selected accelerators onto every cluster node. Poll the GET "
        "below for per-node readiness."
    ),
    operation_id="create_cluster_image_prepull",
)
async def create_cluster_image_prepull(cluster_id: str, payload: ImagePrepullRequest) -> dict:
    cluster = registry.require_cluster(cluster_id)
    return await images.start_image_prepull(cluster_id, payload.accelerators, cluster.gateway_provider)


@router.get(
    "/clusters/{cluster_id}/images/prepull",
    summary="Get the current image pre-pull status for a cluster.",
    description=(
        "Get the current image pre-pull status for a cluster: overall state, ready/desired node count "
        "and each puller pod's node readiness."
    ),
    operation_id="get_cluster_image_prepull",
)
async def read_cluster_image_prepull(cluster_id: str) -> dict:
    registry.require_cluster(cluster_id)
    return await images.image_prepull_status(cluster_id)


@router.post(
    "/clusters/{cluster_id}/crds",
    summary="Apply the pinned Gateway API / Gateway API Inference Extension CRDs.",
    description=(
        "Apply the Gateway API and Gateway API Inference Extension CRD bundles pinned by the Lens "
        "stack profile. Returns a per-bundle result."
    ),
    operation_id="create_cluster_crds",
)
async def create_cluster_crds(cluster_id: str) -> dict:
    registry.require_cluster(cluster_id)
    return await gateway_crds.apply_gateway_crds(cluster_id)


@router.get(
    "/clusters/{cluster_id}/crds",
    summary="Get Gateway API / Gateway API Inference Extension CRD status.",
    description=(
        "Report whether the pinned Gateway API and Gateway API Inference Extension CRDs are present "
        "on the cluster (ready/missing per bundle)."
    ),
    operation_id="get_cluster_crds",
)
async def read_cluster_crds(cluster_id: str) -> dict:
    registry.require_cluster(cluster_id)
    return await gateway_crds.gateway_crds_status(cluster_id)


@router.get(
    "/clusters/{cluster_id}/kubernetes-version",
    summary="Check a cluster's reachability and Kubernetes version against the stack minimum.",
    description=(
        "Probe the cluster's Kubernetes API for reachability and its server version, and compare it "
        "with the minimum Kubernetes version pinned by the Lens stack profile."
    ),
    operation_id="get_cluster_kubernetes_version",
)
async def read_cluster_kubernetes_version(cluster_id: str) -> dict:
    from llm_d_bench.hardware.version import server_kubernetes_version  # noqa: PLC0415
    from llm_d_bench.versions import k8s_version_supports, min_k8s_version  # noqa: PLC0415

    registry.require_cluster(cluster_id)
    minimum = min_k8s_version()
    try:
        parsed = await server_kubernetes_version(cluster_id)
    except Exception as error:  # noqa: BLE001 - report any probe failure to the wizard
        return {"reachable": False, "error": str(error), "min_k8s_version": minimum}
    if parsed is None:
        return {"reachable": False, "min_k8s_version": minimum}
    version = f"{parsed[0]}.{parsed[1]}"
    return {
        "reachable": True,
        "version": version,
        "min_k8s_version": minimum,
        "supported": k8s_version_supports(version),
    }


@router.post(
    "/clusters/{cluster_id}/hf-token-secrets",
    response_model=HfTokenSecretRef,
    status_code=201,
    summary="Create a standalone HF_TOKEN Kubernetes Secret on a Lens-managed cluster.",
    description=(
        "Create a standalone HF_TOKEN Kubernetes Secret on a Lens-managed cluster. Use this when a later Model "
        "Cache or deployment flow should reference an existing secret."
    ),
    operation_id="create_cluster_hf_token_secret",
)
async def create_hf_token_secret(cluster_id: str, payload: HfTokenSecretCreateRequest) -> HfTokenSecretRef:
    """Create a standalone HF_TOKEN Secret (wizard Step 4); reference it afterwards
    via a normal Model Cache ``existing-secret`` token source."""
    cluster = registry.require_cluster(cluster_id)
    try:
        ref = await service.create_hf_token_secret(cluster, payload.namespace, payload.name, payload.token)
    except ClusterOverviewError:
        raise
    except FileNotFoundError as error:
        raise ClusterOverviewError(str(error), status_code=503, code="kubectl_unavailable") from error
    except TimeoutError as error:
        raise ClusterOverviewError(str(error), status_code=504, code="kubectl_timeout") from error
    return HfTokenSecretRef(**ref)


@router.delete(
    "/clusters/{cluster_id}",
    status_code=204,
    summary="Delete a Lens-managed cluster record and clean up related state.",
    description=(
        "Delete a Lens-managed cluster record and clean up related state. "
        "Use this only when the cluster should be removed from Lens entirely."
    ),
    operation_id="delete_cluster",
)
async def delete_cluster(cluster_id: str) -> Response:
    try:
        await service.delete_cluster(cluster_id)
    except ClusterOverviewError:
        raise
    return Response(status_code=204)


@router.get(
    "/session",
    response_model=ClusterSessionResponse,
    summary="Get or create the active Lens session metadata for a cluster id.",
    description=(
        "Get or create the active Lens session metadata for a cluster id. "
        "Use this when another API requires a cluster session id instead of a cluster id."
    ),
    operation_id="get_cluster_session",
)
async def cluster_session(
    cluster_id: str = Query(..., alias="clusterId", min_length=1, max_length=128),
) -> ClusterSessionResponse:
    cluster, session = service.get_cluster_session(cluster_id)
    return ClusterSessionResponse(cluster=_record(cluster, ready=True), session_id=session.id)


@router.get(
    "/overview",
    response_model=OverviewResponse,
    summary=(
        "Get the live Kubernetes overview for a Lens-managed cluster, including node, GPU, cached image, and "
        "component health summaries."
    ),
    description=(
        "Get the live Kubernetes overview for a Lens-managed cluster, including node, GPU, cached container image, "
        "and component health summaries. Each worker node reports cachedImages from its Kubernetes node status; "
        "clients can use images shared by eligible workers to avoid an image pull at deployment time."
    ),
    operation_id="get_cluster_overview",
)
async def overview(
    cluster_id: str = Query(..., alias="clusterId", min_length=1, max_length=128),
) -> OverviewResponse:
    cluster = registry.require_cluster(cluster_id)
    try:
        kubernetes = await service.build_overview(cluster)
    except ClusterOverviewError:
        raise
    except FileNotFoundError as error:
        raise ClusterOverviewError(str(error), status_code=503, code="kubectl_unavailable") from error
    except TimeoutError as error:
        raise ClusterOverviewError(str(error), status_code=504, code="kubectl_timeout") from error
    # ``build_overview`` already issued successful Kubernetes reads against this
    # cluster, so it is reachable. A second ``/readyz`` probe here only added
    # latency to a request the Configure-deployment cluster field waits on.
    return OverviewResponse(
        cluster=_record(cluster, ready=True),
        fetched_at=datetime.now(UTC).isoformat(),
        kubernetes=KubernetesSummary.model_validate(kubernetes),
    )


@router.get(
    "/model-secrets",
    summary=(
        "List model-related secrets visible to a cluster, including whether host-managed Hugging Face credentials "
        "are available."
    ),
    description=(
        "List model-related secrets visible to a cluster, including whether host-managed Hugging Face credentials "
        "are available."
    ),
    operation_id="list_cluster_model_secrets",
)
async def model_secrets(
    cluster_id: str = Query(..., alias="clusterId", min_length=1, max_length=128),
) -> dict[str, object]:
    cluster = registry.require_cluster(cluster_id)
    return {
        "hostManagedAvailable": (Path.home() / ".cache" / "huggingface" / "token").is_file(),
        "items": await service.list_model_secrets(cluster),
    }


async def _apply_node_maintenance(
    cluster_id: str, payload: NodeMaintenanceRequest, *, disabled: bool
) -> NodeMaintenanceResponse:
    cluster = registry.require_cluster(cluster_id)
    try:
        items = await service.set_node_maintenance(cluster, payload.names, disabled=disabled)
    except ClusterOverviewError:
        raise
    except FileNotFoundError as error:
        raise ClusterOverviewError(str(error), status_code=503, code="kubectl_unavailable") from error
    except TimeoutError as error:
        raise ClusterOverviewError(str(error), status_code=504, code="kubectl_timeout") from error
    return NodeMaintenanceResponse(items=items)


@router.post(
    "/clusters/{cluster_id}/nodes/cordon",
    response_model=NodeMaintenanceResponse,
    summary="Mark one or more cluster worker nodes unschedulable so new pods stop landing there.",
    description=(
        "Mark one or more cluster worker nodes unschedulable so new pods stop landing there. "
        "Use this for maintenance windows or targeted isolation."
    ),
    operation_id="cordon_cluster_nodes",
)
async def cordon_nodes(cluster_id: str, payload: NodeMaintenanceRequest) -> NodeMaintenanceResponse:
    """Enter maintenance mode: mark the given worker nodes unschedulable.

    This only stops *new* Pods from being scheduled onto these nodes; Pods
    already running there keep running (no eviction/drain is performed).
    """
    return await _apply_node_maintenance(cluster_id, payload, disabled=True)


@router.post(
    "/clusters/{cluster_id}/nodes/uncordon",
    response_model=NodeMaintenanceResponse,
    summary="Mark one or more cluster worker nodes schedulable again.",
    description=(
        "Mark one or more cluster worker nodes schedulable again. "
        "Use this to return previously cordoned nodes to normal service."
    ),
    operation_id="uncordon_cluster_nodes",
)
async def uncordon_nodes(cluster_id: str, payload: NodeMaintenanceRequest) -> NodeMaintenanceResponse:
    """Exit maintenance mode: mark the given worker nodes schedulable again."""
    return await _apply_node_maintenance(cluster_id, payload, disabled=False)


@legacy_router.get("/sessions/{session_id}")
async def get_overview_session(session_id: str) -> dict[str, object]:
    try:
        session = sessions.require_active_session(session_id)
    except ValueError:
        raise _inactive() from None
    return {
        "id": session.id,
        "serverId": session.server_id,
        "createdAt": session.created_at,
        "status": "connected",
    }


@legacy_router.post("/sessions/{session_id}/disconnect", status_code=204)
async def disconnect(session_id: str) -> Response:
    if not sessions.close_session(session_id):
        raise _inactive()
    return Response(status_code=204)
