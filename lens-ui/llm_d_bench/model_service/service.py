"""Model-service domain service: groups, members, usage and gateway authorize.

Design reference: docs/design/model-service-v2-design.md sections 4-9.
"""

from __future__ import annotations

from llm_d_bench.auth.contracts import Principal, ResourceRef
from llm_d_bench.auth.policy import authorize
from llm_d_bench.auth.service import default_service as default_auth_service
from llm_d_bench.db.dao.model_service_group import ModelServiceGroupDao
from llm_d_bench.db.dao.model_service_member import ModelServiceMemberDao
from llm_d_bench.db.dao.usage_record import UsageRecordDao
from llm_d_bench.model_service.contracts import (
    AuthorizeRequest,
    AuthorizeResult,
    GroupCreateRequest,
    GroupUpdateRequest,
    MemberCreateRequest,
    MemberUpdateRequest,
    ModelAccessToken,
    ModelServiceGroup,
    ModelServiceMember,
    PublishableDeployment,
    UsageRecord,
    UsageRecordRequest,
    utcnow,
)
from llm_d_bench.model_service.gateway_providers import default_pool_name, qualified_pool_name
from llm_d_bench.model_service.selection import select_member
from llm_d_bench.model_service.targets import ExecutionTargetError, resolve_execution_target
from llm_d_bench.model_service.tokens import ModelTokenService
from llm_d_bench.model_service.usage_analytics import build_usage_analytics

INFERENCE_PERMISSION = "model-service:inference:use"


class ModelServiceError(Exception):
    """Base class for model-service domain failures."""


class ModelServiceNotFoundError(ModelServiceError):
    """The model name has no authorized service (deliberately merged with 'none')."""


class ModelServiceUnauthorizedError(ModelServiceError):
    """The token is missing, expired, revoked or the user is not active."""


class ModelServiceConflictError(ModelServiceError):
    """A name or membership already exists."""


class ModelService:
    def __init__(
        self,
        *,
        tokens: ModelTokenService | None = None,
        groups: ModelServiceGroupDao | None = None,
        members: ModelServiceMemberDao | None = None,
        usage: UsageRecordDao | None = None,
        auth_service=None,
    ) -> None:
        self.tokens = tokens or ModelTokenService()
        self.groups = groups or ModelServiceGroupDao()
        self.members = members or ModelServiceMemberDao()
        self.usage = usage or UsageRecordDao()
        self._auth = auth_service or default_auth_service()

    # --- groups --------------------------------------------------------------
    def list_groups(self) -> list[ModelServiceGroup]:
        return self.groups.list()

    def get_group(self, group_id: str) -> ModelServiceGroup | None:
        return self.groups.get(group_id)

    def create_group(self, request: GroupCreateRequest, *, created_by: str | None) -> ModelServiceGroup:
        # Only the public name is user-facing; derive the internal model ref and the
        # routing base model from it when the caller did not supply them.
        model_ref = request.model_ref or request.name
        group = ModelServiceGroup(
            name=request.name,
            model_ref=model_ref,
            display_name=(request.display_name or request.name),
            description=request.description,
            selection_policy=request.selection_policy,
            cluster_id=request.cluster_id,
            served_name=request.served_name,
            base_model=request.base_model or model_ref,
            created_by_user_id=created_by,
        )
        try:
            return self.groups.create(group)
        except Exception as error:  # noqa: BLE001 - map DAO conflict to domain error
            raise ModelServiceConflictError(str(error)) from error

    def update_group(self, group_id: str, request: GroupUpdateRequest) -> ModelServiceGroup:
        group = self.groups.get(group_id)
        if group is None:
            raise ModelServiceNotFoundError(f"group not found: {group_id}")
        if (
            request.cluster_id is not None
            and request.cluster_id != group.cluster_id
            and self.members.list_by_group(group_id)
        ):
            raise ModelServiceConflictError("cannot move a model service to another cluster while it has providers")
        for field in (
            "name",
            "model_ref",
            "display_name",
            "description",
            "selection_policy",
            "status",
            "cluster_id",
            "served_name",
            "base_model",
        ):
            value = getattr(request, field)
            if value is not None:
                setattr(group, field, value)
        group.updated_at = utcnow()
        try:
            return self.groups.save(group)
        except Exception as error:  # noqa: BLE001
            raise ModelServiceConflictError(str(error)) from error

    def delete_group(self, group_id: str) -> None:
        self.groups.delete(group_id)

    # --- members -------------------------------------------------------------
    def list_members(self, group_id: str | None = None) -> list[ModelServiceMember]:
        return self.members.list_by_group(group_id) if group_id else self.members.list_all()

    def create_member(
        self, request: MemberCreateRequest, *, published_by: str | None, principal: Principal | None = None
    ) -> ModelServiceMember:
        group = self.groups.get(request.group_id)
        if group is None:
            raise ModelServiceNotFoundError(f"group not found: {request.group_id}")
        try:
            target = resolve_execution_target(request.execution_id)
        except ExecutionTargetError as error:
            raise ModelServiceNotFoundError(str(error)) from error
        if target.cluster_id != group.cluster_id:
            raise ModelServiceConflictError(f"provider must be in the model service's cluster ({group.cluster_id})")
        if principal is not None:
            from llm_d_bench.auth.access import require_cluster_access  # noqa: PLC0415

            require_cluster_access(principal, target.cluster_id)
        member = ModelServiceMember(
            group_id=request.group_id,
            execution_id=target.execution_id,
            cluster_id=target.cluster_id,
            target_namespace=target.namespace,
            target_service=target.service,
            target_port=target.port,
            endpoint_kind=target.endpoint_kind,
            pool_name=request.pool_name or qualified_pool_name(default_pool_name(target.service), target.namespace),
            epp_ref=target.service if target.endpoint_kind == "llm-d-epp" else None,
            owner_user_id=published_by,
            published_by_user_id=published_by,
        )
        try:
            return self.members.create(member)
        except Exception as error:  # noqa: BLE001
            raise ModelServiceConflictError(str(error)) from error

    def list_publishable_deployments(self, principal=None) -> list[PublishableDeployment]:
        """Ready executions with a derivable target, filtered to visible clusters."""
        from llm_d_bench.auth.access import visible_cluster_ids  # noqa: PLC0415
        from llm_d_bench.cluster.registry import list_clusters  # noqa: PLC0415
        from llm_d_bench.deploy.application import deployment_run_manager  # noqa: PLC0415

        allowed = visible_cluster_ids(principal)
        cluster_names = {cluster.id: cluster.name for cluster in list_clusters()}
        items: list[PublishableDeployment] = []
        for execution in deployment_run_manager.store.list_executions():
            try:
                target = resolve_execution_target(execution.execution_id)
            except ExecutionTargetError:
                continue
            if allowed is not None and target.cluster_id not in allowed:
                continue
            items.append(
                PublishableDeployment(
                    execution_id=target.execution_id,
                    name=target.name,
                    display_name=target.display_name,
                    model=target.model,
                    cluster_id=target.cluster_id,
                    cluster_name=cluster_names.get(target.cluster_id, ""),
                    namespace=target.namespace,
                    service=target.service,
                    port=target.port,
                    endpoint_kind=target.endpoint_kind,
                    model_ref=target.model_ref,
                    status=target.status,
                )
            )
        return items

    def update_member(
        self, member_id: str, request: MemberUpdateRequest, *, principal: Principal | None = None
    ) -> ModelServiceMember:
        member = self.members.get(member_id)
        if member is None:
            raise ModelServiceNotFoundError(f"member not found: {member_id}")
        if principal is not None:
            from llm_d_bench.auth.access import require_cluster_access  # noqa: PLC0415

            require_cluster_access(principal, member.cluster_id)
        for field in ("status",):
            value = getattr(request, field)
            if value is not None:
                setattr(member, field, value)
        member.updated_at = utcnow()
        return self.members.save(member)

    def delete_member(self, member_id: str, *, principal: Principal | None = None) -> None:
        if principal is not None:
            from llm_d_bench.auth.access import require_cluster_access  # noqa: PLC0415

            member = self.members.get(member_id)
            if member is not None:
                require_cluster_access(principal, member.cluster_id)
        self.members.delete(member_id)

    # --- authorization / routing --------------------------------------------
    def _principal_for_token(self, token: ModelAccessToken):
        user = self._auth.user_dao.get(token.user_id)
        if user is None or user.status != "active":
            raise ModelServiceUnauthorizedError("token owner is not an active user")
        return user, self._auth.principal_for(user)

    def _member_authorized(self, principal, member: ModelServiceMember) -> bool:
        resource = ResourceRef(
            resource_type="deployment_execution",
            resource_id=member.execution_id,
            cluster_id=member.cluster_id or "",
            owner_user_id=member.owner_user_id,
            owner_group_id=member.owner_group_id,
        )
        return authorize(principal, INFERENCE_PERMISSION, resource=resource).allowed

    def authorized_members(self, group: ModelServiceGroup, principal) -> list[ModelServiceMember]:
        members = self.members.list_by_group(group.id, status="active")
        return [member for member in members if self._member_authorized(principal, member)]

    def list_models_for_user(self, user_id: str) -> list[ModelServiceGroup]:
        """Active groups with at least one member this user may call."""
        user = self._auth.user_dao.get(user_id)
        if user is None or user.status != "active":
            return []
        principal = self._auth.principal_for(user)
        visible: list[ModelServiceGroup] = []
        for group in self.groups.list():
            if group.status != "active":
                continue
            if self.authorized_members(group, principal):
                visible.append(group)
        return visible

    def list_model_entries_for_user(self, user_id: str) -> list[dict]:
        """One model entry per model service the caller may call.

        A model service is scoped to a single cluster, and its providers live in
        that cluster, so the caller sees it once with its own cluster's connection.
        Each entry carries ``clusterName`` (same enrichment as
        ``list_publishable_deployments``) so callers choosing between several
        model services -- often sharing the same public name across clusters --
        can tell them apart without a separate cluster lookup.
        """
        from llm_d_bench.cluster.registry import list_clusters  # noqa: PLC0415

        user = self._auth.user_dao.get(user_id)
        if user is None or user.status != "active":
            return []
        principal = self._auth.principal_for(user)
        cluster_names = {cluster.id: cluster.name for cluster in list_clusters()}
        entries: list[dict] = []
        for group in self.groups.list():
            if group.status != "active":
                continue
            if not self.authorized_members(group, principal):
                continue
            payload = group.api_payload()
            payload["clusterName"] = cluster_names.get(group.cluster_id, "")
            entries.append(payload)
        return entries

    def authorize_request(self, request: AuthorizeRequest, *, cluster_id: str | None = None) -> AuthorizeResult:
        token = self.tokens.validate(request.token, ip=request.client_ip)
        if token is None:
            raise ModelServiceUnauthorizedError("token is missing, expired or revoked")
        user, principal = self._principal_for_token(token)
        # A model service is scoped to one cluster, so the request's cluster
        # selects the group. Decision D2: never reveal that a model exists but is
        # forbidden.
        groups = [
            group
            for group in self.groups.list()
            if group.name == request.model
            and group.status == "active"
            and (cluster_id is None or group.cluster_id == cluster_id)
        ]
        group = None
        candidates: list = []
        for candidate in groups:
            accessible = self.authorized_members(candidate, principal)
            if accessible:
                group, candidates = candidate, accessible
                break
        if group is None:
            raise ModelServiceNotFoundError(f"no model service for '{request.model}'")
        selected = select_member(candidates, group.selection_policy, user_id=user.id, group_id=group.id)
        return AuthorizeResult(
            user_id=user.id,
            token_id=token.id,
            group_id=group.id,
            group_name=group.name,
            model_ref=group.model_ref,
            cluster_id=selected.cluster_id,
            execution_id=selected.execution_id,
            target_namespace=selected.target_namespace,
            target_service=selected.target_service,
            target_port=selected.target_port,
            endpoint_kind=selected.endpoint_kind,
        )

    # --- usage ---------------------------------------------------------------
    def usage_analytics(
        self,
        *,
        since=None,
        until=None,
        interval: str = "day",
        group_by: str = "model",
        user_ids: list[str] | None = None,
        group_ids: list[str] | None = None,
        cluster_ids: list[str] | None = None,
        principal: Principal | None = None,
    ) -> dict:
        """Aggregated usage for the admin dashboard (server-side, uncapped by page size).

        ``principal`` restricts everything (data, and the cluster/user/model
        dropdown facets) to the caller's reachable clusters -- a cluster-scoped
        maintainer never sees another cluster's usage. ``cluster_ids`` narrows
        further to the clusters selected in the UI; when set, the user/model
        facets cascade to only what's available on those clusters, while the
        cluster dropdown itself always lists every cluster the caller can reach.
        """
        from llm_d_bench.auth.access import effective_cluster_filter  # noqa: PLC0415

        reachable_cluster_ids = effective_cluster_filter(principal, cluster_ids)
        records = self.usage.list_range(
            since=since,
            until=until,
            user_ids=user_ids,
            group_ids=group_ids,
            cluster_ids=reachable_cluster_ids,
        )
        # Cascading facets: users/models are scoped to the selected (or all
        # reachable) clusters; the cluster facet itself always lists every
        # reachable cluster, regardless of the current cluster selection.
        facets = self.usage.list_range(since=since, until=until, cluster_ids=reachable_cluster_ids)
        cluster_facets = (
            facets
            if not cluster_ids
            else self.usage.list_range(since=since, until=until, cluster_ids=effective_cluster_filter(principal, None))
        )
        user_labels: dict[str, str] = {}
        cluster_labels: dict[str, str] = {}
        for record in [*records, *facets, *cluster_facets]:
            if record.user_id and record.user_id not in user_labels:
                user = self._auth.user_dao.get(record.user_id)
                user_labels[record.user_id] = (user.username if user is not None else "") or record.user_id
        if any(record.cluster_id for record in [*records, *facets, *cluster_facets]):
            from llm_d_bench.cluster.registry import list_clusters  # noqa: PLC0415

            cluster_labels = {cluster.id: (cluster.name or cluster.id) for cluster in list_clusters()}
        return build_usage_analytics(
            records,
            facets,
            interval=interval,
            group_by=group_by,
            user_labels=user_labels,
            cluster_labels=cluster_labels,
            cluster_facets=cluster_facets,
        )

    def record_usage(self, request: UsageRecordRequest) -> UsageRecord:
        existing = self.usage.get_by_request_id(request.request_id)
        if existing is not None:
            return existing
        user_id = None
        if request.token_id is not None:
            token = self.tokens.get(request.token_id)
            user_id = token.user_id if token is not None else None
        record = UsageRecord(
            request_id=request.request_id,
            user_id=user_id,
            token_id=request.token_id,
            group_id=request.group_id,
            group_name=request.group_name,
            cluster_id=request.cluster_id,
            execution_id=request.execution_id,
            model_ref=request.model_ref,
            provider=request.provider,
            input_tokens=request.input_tokens,
            cached_input_tokens=request.cached_input_tokens,
            cache_write_tokens=request.cache_write_tokens,
            output_tokens=request.output_tokens,
            requests=request.requests,
            usage_source=request.usage_source,
            status=request.status,
            error_code=request.error_code,
            streaming=request.streaming,
            ttft_ms=request.ttft_ms,
            duration_ms=request.duration_ms,
            client_ip=request.client_ip,
            user_agent=request.user_agent,
        )
        return self.usage.create(record)


_default_service: ModelService | None = None


def default_service() -> ModelService:
    global _default_service
    if _default_service is None:
        _default_service = ModelService()
    return _default_service
