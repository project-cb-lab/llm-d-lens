"""HTTP API for the model service domain.

Three surfaces:
- user (self-scoped): tokens, visible models, own usage
- admin: routing groups/members, usage reports
- internal: Edge Envoy ext_authz authorize + usage recording

Design reference: docs/design/model-service-v2-design.md sections 11-13.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from llm_d_bench.auth.access import (
    current_principal,
    effective_cluster_filter,
    filter_by_cluster,
    owner_for_create,
    require_cluster_access,
    visible_cluster_ids,
)
from llm_d_bench.model_service.contracts import (
    AuthorizeRequest,
    GroupCreateRequest,
    GroupUpdateRequest,
    MemberCreateRequest,
    MemberUpdateRequest,
    TokenCreateRequest,
    UsageRecordRequest,
)
from llm_d_bench.model_service.gateway_contracts import (
    ClusterGatewayRequest,
    ComponentLogsRequest,
    ComponentScaleRequest,
    GatewayStreamRequest,
    IppConfigRequest,
)
from llm_d_bench.model_service.gateway_ops import (
    GatewayOpsService,
    gateway_base_url,
    local_public_ip,
)
from llm_d_bench.model_service.service import (
    ModelServiceConflictError,
    ModelServiceNotFoundError,
    ModelServiceUnauthorizedError,
    default_service,
)
from llm_d_bench.model_service.targets import execution_name
from llm_d_bench.model_service.usage_sync import GatewayUsageSync
from llm_d_bench.utils.problems import problem

logger = logging.getLogger(__name__)
_service = default_service()
_gateway_ops = GatewayOpsService()

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _stream_gateway_operation(run) -> StreamingResponse:
    """Run ``run(on_log)`` in the background and relay its log lines + final
    operation payload as Server-Sent Events (``log``/``complete``/``error``)."""

    async def events():
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def on_log(message: str) -> None:
            await queue.put({"event": "log", "data": {"message": message}})

        async def worker() -> None:
            try:
                operation = await run(on_log)
                await queue.put({"event": "complete", "data": operation.api_payload()})
            except Exception:  # noqa: BLE001 - relay as an SSE error event
                logger.exception("gateway operation stream failed")
                await queue.put({"event": "error", "data": {"message": "Operation failed unexpectedly."}})

        task = asyncio.create_task(worker())
        try:
            while True:
                item = await queue.get()
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                if item["event"] in {"complete", "error"}:
                    break
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)


router = APIRouter(prefix="/api/v1/model-service", tags=["model-service"])
admin_router = APIRouter(prefix="/api/v1/model-service/admin", tags=["model-service-admin"])
internal_router = APIRouter(prefix="/api/v1/internal/model-gateway", tags=["model-service-internal"])
#: OpenAI-compatible public surface served to model tokens (Edge forwards /v1/*).
public_router = APIRouter(tags=["model-service-public"])


def _user_id(request: Request | None) -> str:
    principal = current_principal(request)
    if principal is None or principal.user_id == "system":
        raise HTTPException(status_code=401, detail="authentication required")
    return principal.user_id


def _request_token(request: Request) -> str:
    """Read a model token from ``Authorization: Bearer`` or ``x-api-key``."""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-api-key", "").strip()


async def _reconcile_clusters(cluster_ids: list[str]) -> None:
    """Auto-sync the shared Gateway and model routes after member changes."""
    for cluster_id in sorted(set(cluster_ids)):
        await _gateway_ops.reconcile_cluster(cluster_id)


# --- user: tokens ------------------------------------------------------------


@router.get("/tokens", summary="List the caller's active model access tokens (never plaintext).")
async def list_tokens(request: Request) -> dict:
    user_id = _user_id(request)
    return {"items": [token.api_payload() for token in _service.tokens.list_for_user(user_id, include_revoked=False)]}


@router.post("/tokens", status_code=201, summary="Create a model access token; plaintext returned once.")
async def create_token(request: Request, body: TokenCreateRequest) -> dict:
    user_id = _user_id(request)
    token, raw = _service.tokens.create(user_id, name=body.name)
    payload = token.api_payload()
    payload["token"] = raw
    logger.info("event=model_token_created user_id=%s token_id=%s", user_id, token.id)
    return payload


@router.post("/tokens/regenerate", summary="Revoke the active token(s) and issue a new one.")
async def regenerate_token(request: Request, body: TokenCreateRequest | None = None) -> dict:
    user_id = _user_id(request)
    name = body.name if body is not None else "default"
    token, raw = _service.tokens.regenerate(user_id, name=name)
    payload = token.api_payload()
    payload["token"] = raw
    logger.info("event=model_token_regenerated user_id=%s token_id=%s", user_id, token.id)
    return payload


@router.delete("/tokens/{token_id}", status_code=204, summary="Revoke a model access token.")
async def revoke_token(token_id: str, request: Request) -> Response:
    user_id = _user_id(request)
    if not _service.tokens.revoke(user_id, token_id):
        raise HTTPException(status_code=404, detail=f"token not found: {token_id}")
    logger.info("event=model_token_revoked user_id=%s token_id=%s", user_id, token_id)
    return Response(status_code=204)


# --- user: models + usage ----------------------------------------------------


@router.get("/models", summary="List the model names the caller may call.")
async def list_models(request: Request) -> dict:
    user_id = _user_id(request)
    return {"items": _service.list_model_entries_for_user(user_id)}


@router.get("/connection", summary="Gateway connection info for clients (per cluster).")
async def model_service_connection(request: Request) -> dict:
    """Per-cluster Gateway base URLs a client copies into its OpenAI client.

    Each cluster runs its own shared Gateway with its own address, so callers get
    one connection per cluster they can access. ``LENS_MODEL_GATEWAY_PUBLIC_URL``,
    when set, is a single externally reachable override that applies to every cluster.
    """
    global_url = os.environ.get("LENS_MODEL_GATEWAY_PUBLIC_URL", "").strip()
    allowed = visible_cluster_ids(current_principal(request))
    host_header = (request.headers.get("host") or "").split(":")[0]
    public_host = (
        os.environ.get("LENS_PUBLIC_HOST", "").strip()
        or local_public_ip()
        or host_header
        or (request.url.hostname or "")
    )
    status = await _gateway_ops.status()
    clusters: list[dict] = []
    for cluster in status.clusters:
        if allowed is not None and cluster.cluster_id not in allowed:
            continue
        base_url = gateway_base_url(cluster, public_host, global_url)
        clusters.append(
            {
                "clusterId": cluster.cluster_id,
                "clusterName": cluster.cluster_name,
                "provider": cluster.gateway_provider,
                "gatewayName": cluster.gateway_name,
                "address": cluster.gateway_address,
                "publicUrl": cluster.gateway_public_url,
                "port": cluster.gateway_port,
                "baseUrl": base_url,
                "ready": bool(cluster.gateway_ready),
            }
        )
    return {"baseUrl": global_url, "clusters": clusters}


@public_router.get("/v1/models", summary="OpenAI-compatible model list for a model token.")
async def public_list_models(request: Request) -> Response:
    """OpenAI ``GET /v1/models`` served by the control plane (decision D13).

    Edge Envoy routes ``/v1/models`` to Python; the token is validated here and
    the response lists only the model names this token may call (the same
    visibility rule as ext_authz).
    """
    token = _request_token(request)
    record = _service.tokens.validate(token, ip=request.client.host if request.client else None)
    if record is None:
        return problem(401, "Invalid token", "token is missing, expired or revoked", "token_invalid")
    data = [
        {
            "id": group.name,
            "object": "model",
            "created": int(group.created_at.timestamp()),
            "owned_by": "lens",
        }
        for group in _service.list_models_for_user(record.user_id)
    ]
    return JSONResponse({"object": "list", "data": data})


@router.get("/usage/records", summary="List the caller's recent usage records.")
async def usage_records(request: Request, limit: int = 100, offset: int = 0) -> dict:
    user_id = _user_id(request)
    records = _service.usage.list_for_user(user_id, limit=min(max(limit, 1), 500), offset=max(offset, 0))
    return {"items": [record.api_payload() for record in records]}


@router.get("/usage/timeseries", summary="Daily token totals for the caller.")
async def usage_timeseries(request: Request, days: int = 30) -> dict:
    user_id = _user_id(request)
    since = datetime.now(tz=None) - timedelta(days=max(1, min(days, 365)))
    records = _service.usage.list_for_user(user_id, since=since.replace(tzinfo=None), limit=10000)
    return {"items": _daily_totals(records)}


def _daily_totals(records) -> list[dict]:
    buckets: dict[str, dict] = {}
    for record in records:
        day = record.created_at.date().isoformat()
        bucket = buckets.setdefault(
            day,
            {
                "date": day,
                "requests": 0,
                "inputTokens": 0,
                "cachedInputTokens": 0,
                "cacheWriteTokens": 0,
                "outputTokens": 0,
            },
        )
        bucket["requests"] += 1
        bucket["inputTokens"] += record.input_tokens
        bucket["cachedInputTokens"] += record.cached_input_tokens
        bucket["cacheWriteTokens"] += record.cache_write_tokens
        bucket["outputTokens"] += record.output_tokens
    return [buckets[key] for key in sorted(buckets)]


# --- admin: groups / members / usage -----------------------------------------


@admin_router.get("/groups", summary="List model routing groups.")
async def admin_list_groups() -> dict:
    return {"items": [group.api_payload() for group in _service.list_groups()]}


@admin_router.post("/groups", status_code=201, summary="Create a model routing group.")
async def admin_create_group(request: Request, body: GroupCreateRequest) -> dict:
    try:
        group = _service.create_group(body, created_by=owner_for_create(current_principal(request)))
    except ModelServiceConflictError as error:
        return problem(409, "Model group conflict", str(error), "model_group_conflict")
    return group.api_payload()


@admin_router.get("/groups/{group_id}", summary="Get a model routing group.")
async def admin_get_group(group_id: str) -> dict:
    group = _service.get_group(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"group not found: {group_id}")
    return group.api_payload()


@admin_router.patch("/groups/{group_id}", summary="Update a model routing group.")
async def admin_update_group(group_id: str, body: GroupUpdateRequest) -> dict:
    try:
        group = _service.update_group(group_id, body)
    except ModelServiceNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ModelServiceConflictError as error:
        return problem(409, "Model group conflict", str(error), "model_group_conflict")
    return group.api_payload()


@admin_router.delete("/groups/{group_id}", status_code=204, summary="Delete a model routing group.")
async def admin_delete_group(group_id: str, background: BackgroundTasks) -> Response:
    cluster_ids = [member.cluster_id for member in _service.list_members(group_id)]
    _service.delete_group(group_id)
    background.add_task(_reconcile_clusters, cluster_ids)
    return Response(status_code=204)


@admin_router.get("/members", summary="List published model service members.")
async def admin_list_members(request: Request, group_id: str | None = None) -> dict:
    principal = current_principal(request)
    members = filter_by_cluster(_service.list_members(group_id), lambda member: member.cluster_id, principal)
    items = []
    for member in members:
        payload = member.api_payload()
        payload["name"] = execution_name(member.execution_id) or member.execution_id
        items.append(payload)
    return {"items": items}


@admin_router.get("/deployments", summary="Ready deployments that can be published as members.")
async def admin_publishable_deployments(request: Request) -> dict:
    items = _service.list_publishable_deployments(current_principal(request))
    return {"items": [item.api_payload() for item in items]}


@admin_router.post("/members", status_code=201, summary="Publish a deployment as a group member.")
async def admin_create_member(request: Request, body: MemberCreateRequest, background: BackgroundTasks) -> dict:
    principal = current_principal(request)
    try:
        member = _service.create_member(body, published_by=owner_for_create(principal), principal=principal)
    except ModelServiceNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ModelServiceConflictError as error:
        return problem(409, "Model member conflict", str(error), "model_member_conflict")
    background.add_task(_reconcile_clusters, [member.cluster_id])
    return member.api_payload()


@admin_router.patch("/members/{member_id}", summary="Update a member (status).")
async def admin_update_member(
    member_id: str, body: MemberUpdateRequest, background: BackgroundTasks, request: Request
) -> dict:
    try:
        member = _service.update_member(member_id, body, principal=current_principal(request))
    except ModelServiceNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    background.add_task(_reconcile_clusters, [member.cluster_id])
    return member.api_payload()


@admin_router.delete("/members/{member_id}", status_code=204, summary="Remove a member.")
async def admin_delete_member(member_id: str, background: BackgroundTasks, request: Request) -> Response:
    member = _service.members.get(member_id)
    _service.delete_member(member_id, principal=current_principal(request))
    if member is not None:
        background.add_task(_reconcile_clusters, [member.cluster_id])
    return Response(status_code=204)


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


@admin_router.get("/usage/analytics", summary="Aggregated usage analytics (totals, series, per-dimension).")
async def admin_usage_analytics(
    request: Request,
    since: str | None = None,
    until: str | None = None,
    interval: str = "day",
    group_by: str = "model",
    user_id: Annotated[list[str] | None, Query()] = None,
    group_id: Annotated[list[str] | None, Query()] = None,
    cluster_id: Annotated[list[str] | None, Query()] = None,
) -> dict:
    return _service.usage_analytics(
        since=_parse_dt(since),
        until=_parse_dt(until),
        interval=interval,
        group_by=group_by,
        user_ids=user_id,
        group_ids=group_id,
        cluster_ids=cluster_id,
        principal=current_principal(request),
    )


@admin_router.get("/usage", summary="Recent usage records across users/models/clusters.")
async def admin_usage(
    request: Request,
    group_id: Annotated[list[str] | None, Query()] = None,
    cluster_id: Annotated[list[str] | None, Query()] = None,
    limit: int = 100,
) -> dict:
    effective_cluster_ids = effective_cluster_filter(current_principal(request), cluster_id)
    records = _service.usage.list_all(
        group_ids=group_id, cluster_ids=effective_cluster_ids, limit=min(max(limit, 1), 500)
    )
    return {"items": [record.api_payload() for record in records]}


# --- admin: gateway operations ----------------------------------------------


@admin_router.get("/gateway/status", summary="Gateway data-plane status across clusters.")
async def admin_gateway_status(request: Request) -> dict:
    status = await _gateway_ops.status()
    status.clusters = filter_by_cluster(status.clusters, lambda cluster: cluster.cluster_id, current_principal(request))
    return status.api_payload()


@admin_router.post(
    "/gateway/reconcile",
    summary="Sync the shared Gateway and model routes for every cluster with members.",
)
async def admin_gateway_reconcile(background: BackgroundTasks) -> dict:
    cluster_ids = sorted({member.cluster_id for member in _service.members.list_all() if member.status == "active"})
    background.add_task(_reconcile_clusters, cluster_ids)
    return {"scheduled": True, "clusters": cluster_ids}


@admin_router.post("/gateway/probe", summary="Probe members' backends now and refresh health/status.")
async def admin_gateway_probe() -> dict:
    updated = await _gateway_ops.probe_members()
    return {"updated": updated}


@admin_router.get(
    "/gateway/ipp-config",
    summary="Read a cluster's live Inference Payload Processor config.",
)
async def admin_gateway_get_ipp_config(request: Request, cluster_id: str = Query(alias="clusterId")) -> dict:
    require_cluster_access(current_principal(request), cluster_id)
    return await _gateway_ops.get_ipp_config(cluster_id)


@admin_router.post(
    "/gateway/ipp-config",
    summary="Set a cluster's Inference Payload Processor config and reinstall it.",
)
async def admin_gateway_ipp_config(body: IppConfigRequest, request: Request) -> dict:
    require_cluster_access(current_principal(request), body.cluster_id)
    operation = await _gateway_ops.set_ipp_config(
        body.cluster_id, body.config, actor=owner_for_create(current_principal(request))
    )
    return operation.api_payload()


@admin_router.post(
    "/gateway/usage/sync",
    summary="Scrape EPP token counters and record usage deltas (per user/API key).",
)
async def admin_gateway_usage_sync() -> dict:
    sync = GatewayUsageSync(service=_service)
    written = await sync.sync()
    return {"written": written}


@admin_router.post("/gateway/preflight", summary="Preflight a cluster (kubectl reachable, active members).")
async def admin_gateway_preflight(body: ClusterGatewayRequest, request: Request) -> dict:
    require_cluster_access(current_principal(request), body.cluster_id)
    operation = await _gateway_ops.preflight(body.cluster_id, actor=owner_for_create(current_principal(request)))
    return operation.api_payload()


@admin_router.post("/gateway/install", summary="Install or reuse the cluster's shared llm-d Gateway.")
async def admin_gateway_install(body: ClusterGatewayRequest, request: Request) -> dict:
    require_cluster_access(current_principal(request), body.cluster_id)
    operation = await _gateway_ops.install_cluster_gateway(
        body.cluster_id, provider=body.provider, actor=owner_for_create(current_principal(request))
    )
    return operation.api_payload()


@admin_router.post(
    "/gateway/scale",
    summary="Scale a data-plane component Deployment (Gateway data plane / EPP / model server).",
)
async def admin_gateway_scale(body: ComponentScaleRequest, request: Request) -> dict:
    require_cluster_access(current_principal(request), body.cluster_id)
    operation = await _gateway_ops.scale_component(
        body.cluster_id,
        body.namespace,
        body.name,
        body.replicas,
        kind=body.kind,
        actor=owner_for_create(current_principal(request)),
    )
    return operation.api_payload()


@admin_router.post("/gateway/stream", summary="Run a data-plane operation and stream its log lines (SSE).")
async def admin_gateway_stream(body: GatewayStreamRequest, request: Request) -> StreamingResponse:
    principal = current_principal(request)
    require_cluster_access(principal, body.cluster_id)
    actor = owner_for_create(principal)

    async def run(on_log):
        if body.op == "install-gateway":
            return await _gateway_ops.install_cluster_gateway(
                body.cluster_id, provider=body.provider, actor=actor, on_log=on_log
            )
        if body.op == "uninstall-gateway":
            return await _gateway_ops.uninstall_inference_gateway(body.cluster_id, actor=actor)
        if body.op == "reconcile":
            return await _gateway_ops.reconcile_cluster_gateway(body.cluster_id, actor=actor, on_log=on_log)
        if body.op == "scale":
            if not body.namespace or not body.name or body.replicas is None:
                raise ValueError("scale requires namespace, name and replicas")
            return await _gateway_ops.scale_component(
                body.cluster_id, body.namespace, body.name, body.replicas, kind=body.kind, actor=actor
            )
        if body.op in ("gateway-start", "gateway-stop"):
            return await _gateway_ops.scale_gateway(
                body.cluster_id, 0 if body.op == "gateway-stop" else 1, actor=actor, on_log=on_log
            )
        if body.op == "install-ipp":
            return await _gateway_ops.install_ipp(
                body.cluster_id,
                namespace=body.namespace,
                name=body.name or "lens-ipp",
                provider=body.provider,
                gateway_name=body.gateway_name,
                chart=body.ipp_chart,
                version=body.ipp_version,
                actor=actor,
                on_log=on_log,
            )
        if body.op == "uninstall-ipp":
            return await _gateway_ops.uninstall_ipp(
                body.cluster_id,
                namespace=body.namespace,
                name=body.name or "lens-ipp",
                actor=actor,
                on_log=on_log,
            )
        raise ValueError(f"unknown operation: {body.op}")

    return _stream_gateway_operation(run)


@admin_router.post("/gateway/logs", summary="Stream a data-plane component's logs (SSE).")
async def admin_gateway_logs(body: ComponentLogsRequest, request: Request) -> StreamingResponse:
    require_cluster_access(current_principal(request), body.cluster_id)

    async def events():
        try:
            namespace, name = await _gateway_ops.resolve_log_target(
                body.cluster_id, component=body.component, namespace=body.namespace, name=body.name
            )
            async for line in _gateway_ops.stream_component_logs(
                body.cluster_id,
                namespace,
                name,
                kind=body.kind,
                container=body.container,
                tail=body.tail,
            ):
                yield f"event: log\ndata: {json.dumps({'message': line})}\n\n"
        except Exception as error:  # noqa: BLE001 - relay as an SSE error event
            yield f"event: error\ndata: {json.dumps({'message': str(error)})}\n\n"
        yield "event: complete\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


# --- internal: ext_authz + usage ---------------------------------------------


@internal_router.post(
    "/authorize",
    summary="ext_authz: validate token, resolve model, select backend.",
)
async def internal_authorize(request: Request) -> Response:
    return await _authorize(request, None)


@internal_router.api_route(
    "/authorize",
    methods=["GET", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def internal_authorize_other(request: Request) -> Response:
    return await _authorize(request, None)


@internal_router.api_route(
    "/authorize/{rest:path}",
    methods=["POST", "PUT", "PATCH", "DELETE", "GET"],
    include_in_schema=False,
)
async def internal_authorize_rest(request: Request, rest: str) -> Response:
    return await _authorize(request, rest)


async def _authorize(request: Request, rest: str | None) -> Response:
    """Envoy ext_authz entry point.

    Envoy forwards the client's HTTP method to the authz service, so every method
    is accepted here. Accepts either a JSON body (``{token, model}``) or the raw
    OpenAI request: the token is read from ``Authorization`` / ``x-api-key`` and
    the model from the body (or ``x-lens-model``). On success returns 200 with
    routing headers (``x-lens-*`` and ``x-ai-eg-model``) that Edge Envoy forwards
    upstream. ``/v1/models`` has no model and is only token-checked; Edge routes
    it to the control plane.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - body may be empty/non-JSON
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    authorization = request.headers.get("authorization", "")
    header_token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    token = str(payload.get("token") or header_token or request.headers.get("x-api-key", "") or "")
    model = str(
        payload.get("model")
        or request.headers.get("x-lens-model", "")
        or request.headers.get("x-gateway-base-model-name", "")
        or ""
    )
    target = (rest or "").strip("/")
    # The Gateway's ext_authz path carries its cluster id (".../authorize/cluster/<id>/...").
    cluster_match = re.search(r"(?:^|/)cluster/([A-Za-z0-9_-]+)", target)
    cluster_id = cluster_match.group(1) if cluster_match else None
    if target.endswith("v1/models"):
        record = _service.tokens.validate(token)
        if record is None:
            return problem(
                401,
                "Invalid token",
                "token is missing, expired or revoked",
                "token_invalid",
                headers={"x-lens-error": "invalid token"},
            )
        return JSONResponse(
            {"object": "list"},
            headers={"x-lens-user-id": hashlib.sha256(record.user_id.encode()).hexdigest()[:32]},
        )
    if not token or not model:
        return problem(
            401,
            "Invalid token",
            "missing token or model",
            "token_invalid",
            headers={"x-lens-error": "missing token or model"},
        )
    client_ip = request.client.host if request.client else None
    try:
        result = _service.authorize_request(
            AuthorizeRequest(token=token, model=model, client_ip=client_ip), cluster_id=cluster_id
        )
    except ModelServiceUnauthorizedError as error:
        return problem(401, "Invalid token", str(error), "token_invalid", headers={"x-lens-error": "invalid token"})
    except ModelServiceNotFoundError as error:
        return problem(
            404, "Model not found", str(error), "model_not_found", headers={"x-lens-error": "model not found"}
        )
    headers = {
        "x-lens-user-id": hashlib.sha256(result.user_id.encode()).hexdigest()[:32],
        "x-lens-token": result.token_id,
        "x-lens-group": result.group_id,
        "x-lens-group-name": result.group_name,
        "x-lens-model-ref": result.model_ref,
        "x-lens-cluster": result.cluster_id,
        "x-lens-member": result.execution_id,
        "x-lens-model": result.model_ref or model,
        "x-ai-eg-model": model,
        # llm-d EPP reads this as the per-request `fairness_id` metric label, which
        # the usage sync maps back to this token/user (see usage_sync.py).
        "x-llm-d-inference-fairness-id": result.token_id,
    }
    return JSONResponse(content=result.model_dump(mode="json", by_alias=True), headers=headers)


@internal_router.post("/record-usage", summary="Record a completed inference's token usage (idempotent).")
async def internal_record_usage(body: UsageRecordRequest) -> dict:
    record = _service.record_usage(body)
    return {"recorded": True, "id": record.id}


@internal_router.get("/models", summary="Model names a specific user may call (for /v1/models).")
async def internal_models(user_id: str) -> dict:
    return {"items": [group.api_payload() for group in _service.list_models_for_user(user_id)]}
