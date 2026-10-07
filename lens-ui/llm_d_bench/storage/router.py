"""HTTP API for the Storage module: register/list/delete storage volumes."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from llm_d_bench.auth.access import current_principal, filter_by_cluster, require_cluster_access
from llm_d_bench.storage.contracts import (
    StorageVolume,
    StorageVolumeCreateRequest,
    StorageVolumeStatus,
)
from llm_d_bench.storage.service import (
    StorageVolumeInUseError,
    compute_node_drift,
    default_service,
    discover_nodes,
    discover_storage_classes,
    import_existing_models_for_volume,
    list_execution_refs,
    list_model_cache_refs,
    storage_resource_statuses,
)
from llm_d_bench.storage.store import StorageVolumeStoreError
from llm_d_bench.utils.problems import problem

router = APIRouter(prefix="/api/v1/storage", tags=["storage"])
logger = logging.getLogger(__name__)
_service = default_service()


def _payload(volume: StorageVolume, *, nodes_added: list[str] | None = None) -> dict:
    payload = volume.api_payload(in_use_count=len(list_execution_refs(volume.id)))
    payload["nodesAdded"] = nodes_added or []
    payload["modelCacheCount"] = len(list_model_cache_refs(volume.id))
    return payload


async def _payloads(volumes: list[StorageVolume]) -> list[dict]:
    drift = await compute_node_drift(volumes)
    return [_payload(volume, nodes_added=drift.get(volume.id)) for volume in volumes]


@router.get(
    "/volumes",
    summary="List Lens storage volumes with optional filters for cluster, kind, readiness, purpose, or name query.",
    description="List Lens storage volumes with optional filters for cluster, kind, readiness, purpose, or name query.",
    operation_id="list_storage_volumes",
)
async def list_volumes(
    request: Request = None,
    cluster_id: str = "",
    kind: str = "",
    status: str = "",
    purpose: str = "",
    query: str = "",
) -> dict:
    needle = query.strip().lower()
    volumes = _service.list()
    volumes = filter_by_cluster(volumes, lambda volume: volume.cluster_id, current_principal(request))
    if cluster_id:
        volumes = [volume for volume in volumes if volume.cluster_id == cluster_id]
    if kind:
        volumes = [volume for volume in volumes if volume.kind.value == kind]
    if status:
        volumes = [volume for volume in volumes if volume.status.value == status]
    if purpose:
        volumes = [volume for volume in volumes if purpose in {item.value for item in volume.purposes}]
    if needle:
        volumes = [volume for volume in volumes if needle in volume.name.lower()]
    return {"items": await _payloads(volumes)}


@router.get(
    "/volumes/{volume_id}",
    summary="Get one Lens storage volume by id, including drift and usage metadata.",
    description="Get one Lens storage volume by id, including drift and usage metadata.",
    operation_id="get_storage_volume",
)
async def get_volume(volume_id: str, request: Request = None) -> dict:
    volume = _service.get(volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail=f"storage volume not found: {volume_id}")
    require_cluster_access(current_principal(request), volume.cluster_id)
    drift = await compute_node_drift([volume])
    return _payload(volume, nodes_added=drift.get(volume.id))


@router.get(
    "/volume-resource-status",
    summary="Get resource status for selected storage volumes.",
    description=(
        "Return resource status for registered storage volumes selected by repeated volume_id query parameters. "
        "Duplicate ids are deduplicated and unknown ids are omitted."
    ),
    operation_id="get_storage_volume_resource_status",
)
async def get_volume_resource_status(
    volume_ids: Annotated[list[str], Query(alias="volume_id", default_factory=list)],
) -> dict:
    volumes = [_service.get(volume_id) for volume_id in dict.fromkeys(volume_ids)]
    found = [volume for volume in volumes if volume is not None]
    return {"items": await storage_resource_statuses(found)}


@router.post(
    "/volumes",
    status_code=202,
    summary="Create and provision a new Lens storage volume.",
    description=(
        "Create and provision a new Lens storage volume. Use this before deployments or Model Cache flows that "
        "require a registered shared volume."
    ),
    operation_id="create_storage_volume",
)
async def create_volume(request: StorageVolumeCreateRequest) -> dict:
    requested_name = request.name.strip()
    name = requested_name or f"storage-{request.kind.value}"
    existing_names = {existing.name.strip().lower() for existing in _service.list()}
    if name.lower() in existing_names:
        if requested_name:
            return problem(
                409,
                "Storage name already in use",
                f"A storage volume named '{name}' already exists. Choose a different name.",
                "storage_volume_name_conflict",
            )
        # The auto-generated default name (e.g. a second unnamed
        # local-disk volume) collided -- disambiguate instead of silently
        # colliding, since names are otherwise unique across all volumes.
        suffix = 2
        while f"{name}-{suffix}".lower() in existing_names:
            suffix += 1
        name = f"{name}-{suffix}"
    volume = StorageVolume(
        clusterId=request.cluster_id,
        name=name,
        kind=request.kind,
        status=StorageVolumeStatus.PENDING,
        capacity=request.capacity,
        readOnly=request.read_only,
        purposes=request.purposes,
        localDisk=request.local_disk,
        nfs=request.nfs,
        dynamicPvc=request.dynamic_pvc,
    )
    _service.register_pending(volume)
    logger.info(
        "event=storage_volume_create_requested volume_id=%s kind=%s cluster_id=%s",
        volume.id,
        volume.kind.value,
        volume.cluster_id,
    )
    asyncio.create_task(_provision(volume.id))
    return _payload(volume)


async def _provision(volume_id: str) -> None:
    try:
        await _service.provision(volume_id)
        volume = _service.get(volume_id)
        if volume is not None:
            logger.info("event=storage_volume_provisioned volume_id=%s status=%s", volume_id, volume.status.value)
            if volume.status == StorageVolumeStatus.READY:
                # Best-effort: discover model files already sitting on the
                # volume (pre-existing NFS/hostPath content, or a re-created
                # registration over previously-used storage) and auto-create
                # Model Cache entries for them instead of requiring the user
                # to re-request each download from scratch.
                await import_existing_models_for_volume(volume)
    except Exception:
        logger.exception("event=storage_volume_provision_failed volume_id=%s", volume_id)


@router.post(
    "/volumes/{volume_id}/acknowledge-nodes",
    summary=("Acknowledge newly added nodes for a local-disk storage volume after you have replicated its contents."),
    description=(
        "Acknowledge newly added nodes for a local-disk storage volume after you have replicated its contents. "
        "This clears node-drift warnings in Lens."
    ),
    operation_id="acknowledge_storage_volume_nodes",
)
async def acknowledge_volume_nodes(volume_id: str) -> dict:
    """Clear a ``local-disk`` volume's node-drift warning by rebaselining ``known_nodes``.

    Call after manually confirming/replicating the hostPath content onto the
    node(s) ``nodesAdded`` reported as new.
    """
    try:
        volume = await _service.acknowledge_nodes(volume_id)
    except StorageVolumeStoreError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    logger.info("event=storage_volume_nodes_acknowledged volume_id=%s", volume_id)
    return _payload(volume, nodes_added=[])


@router.post(
    "/volumes/{volume_id}/scan-models",
    status_code=202,
    summary=(
        "Re-scan a Lens storage volume for model files already present on disk and auto-register Model Cache entries "
        "for any that are found."
    ),
    description=(
        "Re-scan a Lens storage volume for model files already present on disk and auto-register Model Cache entries "
        "for any that are found. Use this after copying model files onto the volume outside of Lens."
    ),
    operation_id="scan_storage_volume_models",
)
async def scan_volume_models(volume_id: str) -> dict:
    volume = _service.get(volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail=f"storage volume not found: {volume_id}")
    if volume.status != StorageVolumeStatus.READY:
        return problem(
            409,
            "Storage volume is not ready",
            f"storage volume {volume_id} is not ready (status={volume.status.value})",
            "storage_volume_not_ready",
        )
    logger.info("event=storage_volume_model_scan_requested volume_id=%s", volume_id)
    asyncio.create_task(import_existing_models_for_volume(volume))
    return {"status": "scanning"}


@router.delete(
    "/volumes/{volume_id}",
    status_code=202,
    summary="Delete a Lens storage volume registration and, where relevant, its Kubernetes resources.",
    description=(
        "Delete a Lens storage volume registration and, where relevant, its Kubernetes resources. Any Model Cache "
        "entries on the volume are deleted too -- pass keep_model_files=true to remove only their records and leave "
        "the model files on the underlying NFS/hostPath storage untouched, or omit it (false) to actually delete "
        "those files first. Use this only when the volume is no longer needed."
    ),
    operation_id="delete_storage_volume",
)
async def delete_volume(volume_id: str, keep_model_files: bool = False) -> dict:
    try:
        volume = await _service.delete(volume_id)
    except StorageVolumeStoreError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except StorageVolumeInUseError as error:
        return problem(
            409,
            "Storage volume is in use",
            f"storage volume is referenced by deployment execution(s): {', '.join(error.execution_ids)}",
            "storage_volume_in_use",
        )
    logger.info("event=storage_volume_delete_requested volume_id=%s keep_model_files=%s", volume_id, keep_model_files)
    asyncio.create_task(_finish_delete(volume_id, keep_model_files=keep_model_files))
    return _payload(volume)


async def _finish_delete(volume_id: str, *, keep_model_files: bool) -> None:
    try:
        await _service.finish_delete(volume_id, keep_model_files=keep_model_files)
        logger.info("event=storage_volume_deleted volume_id=%s", volume_id)
    except Exception:
        logger.exception("event=storage_volume_delete_failed volume_id=%s", volume_id)


@router.get(
    "/storage-classes",
    summary="List Kubernetes StorageClasses visible to a cluster.",
    description=(
        "List Kubernetes StorageClasses visible to a cluster. Use this before creating dynamic-pvc storage volumes."
    ),
    operation_id="list_storage_classes",
)
async def list_storage_classes(request: Request = None, cluster_id: str = Query(..., min_length=1)) -> dict:
    require_cluster_access(current_principal(request), cluster_id)
    try:
        classes = await discover_storage_classes(cluster_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"items": [item.model_dump(mode="json", by_alias=True) for item in classes]}


@router.get(
    "/nodes",
    summary="List cluster nodes as seen by the Storage module.",
    description=(
        "List cluster nodes as seen by the Storage module. Use this before creating local-disk storage volumes that "
        "assume replicated host paths across nodes."
    ),
    operation_id="list_storage_nodes",
)
async def list_nodes(request: Request = None, cluster_id: str = Query(..., min_length=1)) -> dict:
    require_cluster_access(current_principal(request), cluster_id)
    try:
        nodes = await discover_nodes(cluster_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"items": [item.model_dump(mode="json", by_alias=True) for item in nodes]}
