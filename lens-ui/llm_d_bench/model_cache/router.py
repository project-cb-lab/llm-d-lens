"""HTTP API for the Model Cache module: request/list/retry/delete model downloads."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from llm_d_bench.auth.access import current_principal, filter_by_cluster, require_cluster_access
from llm_d_bench.model_cache import huggingface_hub
from llm_d_bench.model_cache.contracts import (
    HuggingFaceModelDetail,
    HuggingFaceModelSummary,
    ModelCacheCreateRequest,
    ModelCacheEntry,
    ModelCacheEntryStatus,
)
from llm_d_bench.model_cache.service import (
    ModelCacheInvalidStateError,
    ModelCacheNotFoundError,
    ModelCatalogNotSupportedError,
    default_service,
)
from llm_d_bench.utils.problems import problem

router = APIRouter(prefix="/api/v1/model-cache", tags=["model-cache"])
logger = logging.getLogger(__name__)
_service = default_service()


def _payload(entry: ModelCacheEntry, *, pending_sync_nodes: list[str] | None = None) -> dict:
    payload = entry.api_payload()
    payload["pendingSyncNodes"] = pending_sync_nodes or []
    return payload


async def _payloads(entries: list[ModelCacheEntry]) -> list[dict]:
    pending = await _service.compute_pending_sync(entries)
    return [_payload(entry, pending_sync_nodes=pending.get(entry.id)) for entry in entries]


@router.get(
    "/entries",
    summary="List Model Cache entries, optionally filtered by cluster or storage volume.",
    description=(
        "List Model Cache entries, optionally filtered by cluster or storage volume. Use this to discover cached "
        "model ids, download state, and node sync drift."
    ),
    operation_id="list_model_cache_entries",
)
async def list_entries(request: Request = None, cluster_id: str = "", storage_volume_id: str = "") -> dict:
    entries = await _service.list(cluster_id=cluster_id or None, storage_volume_id=storage_volume_id or None)
    entries = filter_by_cluster(entries, lambda entry: entry.cluster_id, current_principal(request))
    return {"items": await _payloads(entries)}


@router.get(
    "/entries/{entry_id}",
    summary=("Get one Model Cache entry by id, including source, token mode, node progress, and pending sync nodes."),
    description=(
        "Get one Model Cache entry by id, including source, token mode, node progress, and pending sync nodes."
    ),
    operation_id="get_model_cache_entry",
)
async def get_entry(entry_id: str, request: Request = None) -> dict:
    try:
        entry = await _service.get(entry_id)
    except ModelCacheNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    require_cluster_access(current_principal(request), entry.cluster_id)
    pending = await _service.compute_pending_sync([entry])
    return _payload(entry, pending_sync_nodes=pending.get(entry.id))


@router.post(
    "/entries",
    status_code=202,
    summary="Request a model download into Lens Model Cache.",
    description=(
        "Request a model download into Lens Model Cache. Use this to seed a ready shared model cache before "
        "deployments or benchmarks."
    ),
    operation_id="create_model_cache_entry",
)
async def create_entry(request: ModelCacheCreateRequest) -> dict:
    try:
        entry = await _service.request_download(request)
    except ModelCatalogNotSupportedError as error:
        return problem(501, "Model Catalog not supported", str(error), "model_catalog_not_supported")
    except ValueError as error:
        return problem(409, "Storage volume not usable", str(error), "storage_volume_not_ready")
    logger.info(
        "event=model_cache_download_requested entry_id=%s volume_id=%s cluster_id=%s",
        entry.id,
        entry.storage_volume_id,
        entry.cluster_id,
    )
    asyncio.create_task(_provision(entry.id))
    return _payload(entry)


async def _provision(entry_id: str) -> None:
    try:
        await _service.provision(entry_id)
    except Exception:
        logger.exception("event=model_cache_provision_failed entry_id=%s", entry_id)


@router.post(
    "/entries/{entry_id}/retry",
    status_code=202,
    summary="Retry a failed or retryable Model Cache entry download.",
    description="Retry a failed or retryable Model Cache entry download.",
    operation_id="retry_model_cache_entry",
)
async def retry_entry(entry_id: str) -> dict:
    try:
        entry = _service.retry(entry_id)
    except ModelCacheNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ModelCacheInvalidStateError as error:
        return problem(409, "Entry cannot be retried", str(error), "model_cache_invalid_state")
    logger.info("event=model_cache_retry_requested entry_id=%s", entry_id)
    asyncio.create_task(_provision(entry_id))
    return _payload(entry)


async def _provision_new_nodes(entry_id: str) -> None:
    try:
        await _service.provision_new_nodes(entry_id)
        logger.info("event=model_cache_node_sync_completed entry_id=%s", entry_id)
    except Exception:
        logger.exception("event=model_cache_node_sync_failed entry_id=%s", entry_id)


@router.post(
    "/entries/sync-nodes",
    status_code=202,
    summary="Trigger sync for all local-disk Model Cache entries that are missing on newly added nodes.",
    description=(
        "Trigger sync for all local-disk Model Cache entries that are missing on newly added nodes. "
        "Optionally scope to one cluster."
    ),
    operation_id="sync_model_cache_nodes",
)
async def sync_all_entry_nodes(cluster_id: str = "") -> dict:
    """One-click fix: (re)download onto every cluster node any ``local-disk``
    entry hasn't reached yet, across all entries (optionally scoped to a
    cluster). Meant for the Model Cache page's "Sync new nodes" action once
    cluster scale-out leaves some cached models incomplete on new nodes.
    """
    changed = await _service.sync_all_nodes(cluster_id=cluster_id or None)
    for entry in changed:
        asyncio.create_task(_provision_new_nodes(entry.id))
    if changed:
        logger.info("event=model_cache_sync_all_requested count=%d cluster_id=%s", len(changed), cluster_id or "*")
    return {"items": [_payload(entry) for entry in changed]}


@router.post(
    "/entries/{entry_id}/sync-nodes",
    status_code=202,
    summary="Trigger node sync for one specific Model Cache entry.",
    description=(
        "Trigger node sync for one specific Model Cache entry. Use this when a single cached model needs to be "
        "replicated onto newly added nodes."
    ),
    operation_id="sync_model_cache_entry_nodes",
)
async def sync_entry_nodes(entry_id: str) -> dict:
    try:
        entry = await _service.sync_nodes(entry_id)
    except ModelCacheNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if entry.status == ModelCacheEntryStatus.DOWNLOADING:
        logger.info("event=model_cache_sync_requested entry_id=%s", entry_id)
        asyncio.create_task(_provision_new_nodes(entry_id))
    return _payload(entry)


@router.delete(
    "/entries/{entry_id}",
    status_code=202,
    summary=("Delete a Model Cache entry and, when required, schedule cleanup of its downloaded files."),
    description=(
        "Delete a Model Cache entry and, when required, schedule cleanup of its downloaded files. "
        "Set keep_files=true to remove the cache record only, leaving model files on the underlying storage untouched."
    ),
    operation_id="delete_model_cache_entry",
)
async def delete_entry(entry_id: str, keep_files: bool = False) -> dict:
    try:
        entry = await _service.request_delete(entry_id, keep_files=keep_files)
    except ModelCacheNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if entry is None:
        # Removed synchronously: it never reached ``ready``, so there was
        # nothing on the shared volume that needed a mount-dependent cleanup
        # Job (see ``service.request_delete``). Nothing left to provision.
        logger.info("event=model_cache_deleted_immediately entry_id=%s", entry_id)
        return {"id": entry_id, "status": "deleted"}
    logger.info("event=model_cache_delete_requested entry_id=%s", entry_id)
    asyncio.create_task(_provision_delete(entry_id))
    return _payload(entry)


@router.get(
    "/entries/{entry_id}/logs",
    summary="Get logs for one Model Cache entry, optionally scoped to a specific node.",
    description=(
        "Get logs for one Model Cache entry, optionally scoped to a specific node. "
        "Use this to debug download or cleanup failures."
    ),
    operation_id="get_model_cache_entry_logs",
)
async def get_entry_logs(entry_id: str, node: str = "") -> dict:
    try:
        logs = await _service.get_logs(entry_id, node=node or None)
    except ModelCacheNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"logs": logs}


async def _provision_delete(entry_id: str) -> None:
    try:
        await _service.provision_delete(entry_id)
        logger.info("event=model_cache_deleted entry_id=%s", entry_id)
    except Exception:
        logger.exception("event=model_cache_delete_failed entry_id=%s", entry_id)


@router.get(
    "/huggingface/search",
    summary="Search Hugging Face model repositories from Lens Model Cache.",
    description=(
        "Search Hugging Face model repositories from Lens Model Cache. "
        "Use this to discover repo ids before creating a cache entry."
    ),
    operation_id="search_huggingface_models",
)
async def search_huggingface_models(query: str = "", limit: int = 20) -> dict:
    needle = query.strip()
    if not needle:
        return {"items": []}
    try:
        raw_items = await huggingface_hub.search_models(needle, limit=limit)
    except huggingface_hub.HuggingFaceHubError as error:
        return problem(502, "HuggingFace Hub unavailable", str(error), "huggingface_hub_unavailable")
    items = [HuggingFaceModelSummary.from_hub_json(item).model_dump(mode="json", by_alias=True) for item in raw_items]
    return {"items": items}


@router.get(
    "/huggingface/models/{repo_id:path}",
    summary=(
        "Get detailed Hugging Face metadata for one model repository, including the README snapshot Lens fetched."
    ),
    description=(
        "Get detailed Hugging Face metadata for one model repository, including the README snapshot Lens fetched."
    ),
    operation_id="get_huggingface_model",
)
async def get_huggingface_model(repo_id: str) -> dict:
    try:
        payload = await huggingface_hub.get_model_detail(repo_id)
    except huggingface_hub.HuggingFaceModelNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except huggingface_hub.HuggingFaceHubError as error:
        return problem(502, "HuggingFace Hub unavailable", str(error), "huggingface_hub_unavailable")
    return HuggingFaceModelDetail.from_hub_payload(payload).model_dump(mode="json", by_alias=True)
