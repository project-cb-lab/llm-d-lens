"""Tests for the model-service domain: tokens, selection, routing and usage."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from llm_d_bench.api.main import app
from llm_d_bench.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_bench.auth.records import UserRecord
from llm_d_bench.db.dao.user import UserDao
from llm_d_bench.model_service.contracts import (
    AuthorizeRequest,
    GroupCreateRequest,
    GroupUpdateRequest,
    MemberCreateRequest,
    MemberUpdateRequest,
    UsageRecord,
    UsageRecordRequest,
)
from llm_d_bench.model_service.selection import (
    RandomSelection,
    RoundRobinSelection,
    WeightedSelection,
    available_policies,
    get_strategy,
    select_member,
)
from llm_d_bench.model_service.service import (
    ModelService,
    ModelServiceConflictError,
    ModelServiceNotFoundError,
    ModelServiceUnauthorizedError,
)
from llm_d_bench.model_service.tokens import ModelTokenService, generate_model_token, hash_model_token


def _create_user(username: str) -> UserRecord:
    return UserDao().create(UserRecord(username=username))


def _patch_target(
    monkeypatch, *, execution_id: str = "exec-1", cluster_id: str = "cluster-a", kind: str = "vllm"
) -> None:
    from llm_d_bench.model_service import service as service_module
    from llm_d_bench.model_service.targets import ExecutionTarget

    monkeypatch.setattr(
        service_module,
        "resolve_execution_target",
        lambda _eid: ExecutionTarget(
            execution_id=execution_id,
            cluster_id=cluster_id,
            namespace="ns",
            service="vllm",
            port=8000,
            endpoint_kind=kind,
            model_ref="Qwen/Qwen3-8B",
            status="ready",
            display_name="Qwen3 8B",
            name="qwen-qwen3-8b-202609210558",
            model="Qwen/Qwen3-8B",
        ),
    )


def _admin_principal(user_id: str, username: str) -> Principal:
    return Principal(
        user_id=user_id,
        username=username,
        bindings=(RoleBinding(role="admin", scope_type=ScopeType.GLOBAL),),
    )


class _StubAuth:
    def __init__(self) -> None:
        self.user_dao = UserDao()

    def principal_for(self, user: UserRecord) -> Principal:
        return _admin_principal(user.id, user.username)


def _service() -> ModelService:
    return ModelService(auth_service=_StubAuth())


# --- tokens ------------------------------------------------------------------


def test_generated_token_has_prefix_and_hash_is_stable():
    raw = generate_model_token()
    assert raw.startswith("lens-mk-")
    assert len(hash_model_token(raw)) == 64
    assert hash_model_token(raw) == hash_model_token(raw)


def test_token_create_validate_and_revoke():
    user = _create_user("token-owner")
    service = ModelTokenService()
    token, raw = service.create(user.id, name="laptop")
    assert token.token_hint == raw[-4:]

    validated = service.validate(raw)
    assert validated is not None and validated.id == token.id

    assert service.revoke(user.id, token.id) is True
    assert service.validate(raw) is None
    assert service.revoke(user.id, "missing") is False


def test_token_regenerate_revokes_previous_active():
    user = _create_user("regen-owner")
    service = ModelTokenService()
    first, first_raw = service.create(user.id)
    second, second_raw = service.regenerate(user.id)
    assert second.id != first.id
    assert service.validate(first_raw) is None
    assert service.validate(second_raw) is not None


# --- selection ---------------------------------------------------------------


def test_selection_registry_lists_implemented_policies():
    assert available_policies() == ["random", "round_robin", "weighted"]
    assert get_strategy("random").name == "random"
    assert get_strategy("round_robin").name == "round_robin"
    assert get_strategy("weighted").name == "weighted"
    with pytest.raises(ValueError):
        get_strategy("affinity")


def test_weighted_selection_prefers_heavier_candidates():
    class Candidate:
        def __init__(self, name, weight):
            self.name = name
            self.weight = weight

    strategy = WeightedSelection(random.Random(0))  # noqa: S311 - deterministic test seed
    light = Candidate("light", 1)
    heavy = Candidate("heavy", 9)
    picks = [strategy.select([light, heavy], user_id="u", group_id="g").name for _ in range(200)]
    assert picks.count("heavy") > picks.count("light")
    with pytest.raises(ValueError):
        strategy.select([], user_id="u", group_id="g")


def test_random_selection_returns_a_candidate():
    candidates = ["a", "b", "c"]
    assert select_member(candidates, "random", user_id="u", group_id="g") in candidates
    assert RandomSelection().select(candidates, user_id="u", group_id="g") in candidates
    with pytest.raises(ValueError):
        RandomSelection().select([], user_id="u", group_id="g")


def test_round_robin_selection_cycles_in_order():
    strategy = RoundRobinSelection()
    candidates = ["a", "b", "c"]
    picks = [strategy.select(candidates, user_id="u", group_id="g") for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]
    # Counters are per group.
    assert strategy.select(candidates, user_id="u", group_id="other") == "a"
    with pytest.raises(ValueError):
        strategy.select([], user_id="u", group_id="g")


# --- groups / members / routing ---------------------------------------------


def test_authorize_request_selects_authorized_member(monkeypatch):
    _patch_target(monkeypatch)
    user = _create_user("router-owner")
    service = _service()
    group = service.create_group(
        GroupCreateRequest(name="qwen3-8b", model_ref="Qwen/Qwen3-8B", clusterId="cluster-a"), created_by=user.id
    )
    service.create_member(
        MemberCreateRequest(group_id=group.id, execution_id="exec-1"),
        published_by=user.id,
    )
    _, raw = service.tokens.create(user.id)

    result = service.authorize_request(AuthorizeRequest(token=raw, model="qwen3-8b"))
    assert result.user_id == user.id
    assert result.group_id == group.id
    assert result.cluster_id == "cluster-a"
    assert result.execution_id == "exec-1"
    assert result.target_service == "vllm"
    assert result.target_port == 8000

    assert [g.id for g in service.list_models_for_user(user.id)] == [group.id]


def test_authorize_request_unknown_model_and_bad_token():
    user = _create_user("router-owner-2")
    service = _service()
    service.create_group(
        GroupCreateRequest(name="known", model_ref="Known/Model", clusterId="cluster-a"), created_by=user.id
    )
    _, raw = service.tokens.create(user.id)

    with pytest.raises(ModelServiceNotFoundError):
        service.authorize_request(AuthorizeRequest(token=raw, model="unknown"))
    with pytest.raises(ModelServiceUnauthorizedError):
        service.authorize_request(AuthorizeRequest(token="lens-mk-bogus", model="known"))  # noqa: S106


def test_membership_conflict_and_update_delete(monkeypatch):
    _patch_target(monkeypatch, execution_id="exec-x", cluster_id="c1")
    user = _create_user("member-owner")
    service = _service()
    group = service.create_group(GroupCreateRequest(name="g1", model_ref="M", clusterId="c1"), created_by=user.id)
    request = MemberCreateRequest(group_id=group.id, execution_id="exec-x")
    member = service.create_member(request, published_by=user.id)
    with pytest.raises(ModelServiceConflictError):
        service.create_member(request, published_by=user.id)

    updated = service.update_member(member.id, MemberUpdateRequest(status="unhealthy"))
    assert updated.status == "unhealthy"

    service.delete_member(member.id)
    assert service.list_members(group.id) == []

    renamed = service.update_group(group.id, GroupUpdateRequest(display_name="Friendly"))
    assert renamed.display_name == "Friendly"
    service.delete_group(group.id)
    assert service.get_group(group.id) is None


# --- usage -------------------------------------------------------------------


def test_record_usage_is_idempotent_by_request_id():
    user = _create_user("usage-owner")
    service = _service()
    token, _ = service.tokens.create(user.id)
    request = UsageRecordRequest(
        request_id="req-1",
        token_id=token.id,
        group_id="g1",
        group_name="g1",
        cluster_id="c1",
        execution_id="exec-1",
        model_ref="M",
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=5,
    )
    first = service.record_usage(request)
    second = service.record_usage(request)
    assert first.id == second.id
    assert first.user_id == user.id
    assert first.input_tokens == 10 and first.output_tokens == 5
    assert service.usage.count_for_user(user.id) == 1


# --- OpenAI-compatible public surface ----------------------------------------


def test_public_models_endpoint_lists_only_authorized(monkeypatch):
    _patch_target(monkeypatch)
    user = _create_user("public-models-owner")
    service = _service()
    group = service.create_group(
        GroupCreateRequest(name="pub-model", model_ref="Pub/Model", clusterId="cluster-a"), created_by=user.id
    )
    service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-1"), published_by=user.id)
    _, raw = service.tokens.create(user.id)
    from llm_d_bench.model_service import router as router_module

    monkeypatch.setattr(router_module, "_service", service)
    client = TestClient(app)

    ok = client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"})
    assert ok.status_code == 200
    assert [item["id"] for item in ok.json()["data"]] == ["pub-model"]

    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer lens-mk-bogus"}).status_code == 401


def test_list_model_entries_for_user_includes_cluster_name(monkeypatch):
    _patch_target(monkeypatch)
    monkeypatch.setattr(
        "llm_d_bench.cluster.registry.list_clusters",
        lambda: [SimpleNamespace(id="cluster-a", name="Cluster A")],
    )
    user = _create_user("entries-owner")
    service = _service()
    group = service.create_group(
        GroupCreateRequest(name="entries-model", model_ref="Entries/Model", clusterId="cluster-a"),
        created_by=user.id,
    )
    service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-1"), published_by=user.id)

    entries = service.list_model_entries_for_user(user.id)

    assert [entry["clusterName"] for entry in entries] == ["Cluster A"]


def test_ext_authz_accepts_forwarded_methods_and_models_path():
    user = _create_user("ext-authz-owner")
    service = _service()
    _, raw = service.tokens.create(user.id)
    client = TestClient(app)

    # Envoy forwards the client method to ext_authz; GET must not be a 405.
    listed = client.get(
        "/api/v1/internal/model-gateway/authorize/v1/models",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert listed.status_code == 200
    assert listed.headers.get("x-lens-user-id")

    denied = client.get(
        "/api/v1/internal/model-gateway/authorize/v1/models",
        headers={"Authorization": "Bearer lens-mk-bogus"},
    )
    assert denied.status_code == 401


def test_usage_analytics_buckets_and_groups():
    from datetime import datetime

    from llm_d_bench.model_service.usage_analytics import build_usage_analytics

    def record(day, hour, model, user, cluster, inp, out, cached=0, request_id=None):
        return UsageRecord(
            request_id=request_id or f"r-{day}-{hour}-{model}-{user}-{inp}",
            group_id=f"g-{model}",
            group_name=model,
            user_id=user,
            cluster_id=cluster,
            created_at=datetime(2026, 9, day, hour, 0, 0),
            input_tokens=inp,
            cached_input_tokens=cached,
            output_tokens=out,
        )

    records = [
        record(21, 9, "m1", "u1", "c1", 10, 5, cached=2),
        record(21, 10, "m1", "u2", "c1", 3, 1),
        record(22, 9, "m2", "u1", "c2", 4, 0),
    ]
    by_model = build_usage_analytics(records, records, interval="day", group_by="model")
    assert by_model["totals"]["requests"] == 3
    assert by_model["totals"]["tokens"] == (10 + 5 + 2) + (3 + 1) + 4
    assert [row["bucket"] for row in by_model["series"]] == ["2026-09-21", "2026-09-22"]
    groups = {group["key"]: group for group in by_model["groups"]}
    assert groups["m1"]["totals"]["requests"] == 2 and groups["m1"]["totals"]["tokens"] == 21
    assert {opt["key"] for opt in by_model["facets"]["models"]} == {"g-m1", "g-m2"}
    assert {opt["key"] for opt in by_model["facets"]["users"]} == {"u1", "u2"}

    by_hour = build_usage_analytics(records, [], interval="hour", group_by="user")
    assert [row["bucket"] for row in by_hour["series"]] == ["2026-09-21T09", "2026-09-21T10", "2026-09-22T09"]
    assert {group["key"] for group in by_hour["groups"]} == {"u1", "u2"}


# --- cluster-scoped RBAC on admin gateway endpoints --------------------------


def _cluster_principal(cluster_id: str, user_id: str = "maintainer", *, with_permissions: bool = False) -> Principal:
    from llm_d_bench.auth.permissions import BUILTIN_ROLE_MAINTAINER, BUILTIN_ROLE_PERMISSIONS

    role_permissions = {"maintainer": BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_MAINTAINER]} if with_permissions else {}
    return Principal(
        user_id=user_id,
        username=user_id,
        bindings=(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id=cluster_id),),
        role_permissions=role_permissions,
    )


def _fake_request(principal: Principal | None):
    from starlette.requests import Request as StarletteRequest

    request = StarletteRequest({"type": "http", "headers": []})
    request.state.principal = principal
    return request


def test_admin_list_members_filters_by_reachable_cluster(monkeypatch):
    from llm_d_bench.model_service import router as router_module

    service = _service()
    group_mine = service.create_group(
        GroupCreateRequest(name="rbac-members-mine", clusterId="cluster-mine"), created_by=None
    )
    _patch_target(monkeypatch, execution_id="exec-mine", cluster_id="cluster-mine")
    service.create_member(MemberCreateRequest(group_id=group_mine.id, execution_id="exec-mine"), published_by=None)
    group_theirs = service.create_group(
        GroupCreateRequest(name="rbac-members-theirs", clusterId="cluster-theirs"), created_by=None
    )
    _patch_target(monkeypatch, execution_id="exec-theirs", cluster_id="cluster-theirs")
    service.create_member(MemberCreateRequest(group_id=group_theirs.id, execution_id="exec-theirs"), published_by=None)
    monkeypatch.setattr(router_module, "_service", service)

    request = _fake_request(_cluster_principal("cluster-mine"))
    import asyncio

    result = asyncio.run(router_module.admin_list_members(request))
    cluster_ids = {item["clusterId"] for item in result["items"]}
    assert cluster_ids == {"cluster-mine"}


def test_admin_gateway_status_filters_by_reachable_cluster(monkeypatch):
    import asyncio

    from llm_d_bench.model_service import router as router_module
    from llm_d_bench.model_service.gateway_contracts import GatewayStatus, GatewayStatusCluster

    async def fake_status():
        return GatewayStatus(
            clusters=[
                GatewayStatusCluster(cluster_id="cluster-mine", cluster_name="mine", members=[]),
                GatewayStatusCluster(cluster_id="cluster-theirs", cluster_name="theirs", members=[]),
            ],
        )

    monkeypatch.setattr(router_module._gateway_ops, "status", fake_status)

    request = _fake_request(_cluster_principal("cluster-mine"))
    payload = asyncio.run(router_module.admin_gateway_status(request))
    cluster_ids = {cluster["clusterId"] for cluster in payload["clusters"]}
    assert cluster_ids == {"cluster-mine"}


def test_admin_gateway_operations_reject_unreachable_cluster(monkeypatch):
    import asyncio

    from llm_d_bench.core.exceptions import ForbiddenError
    from llm_d_bench.model_service import router as router_module
    from llm_d_bench.model_service.gateway_contracts import ClusterGatewayRequest

    request = _fake_request(_cluster_principal("cluster-mine"))
    body = ClusterGatewayRequest(clusterId="cluster-theirs")

    for coro_factory in (
        lambda: router_module.admin_gateway_preflight(body, request),
        lambda: router_module.admin_gateway_install(body, request),
    ):
        with pytest.raises(ForbiddenError):
            asyncio.run(coro_factory())


def test_admin_member_operations_reject_unreachable_cluster(monkeypatch):
    from llm_d_bench.core.exceptions import ForbiddenError
    from llm_d_bench.model_service.contracts import MemberCreateRequest, MemberUpdateRequest

    service = _service()
    group = service.create_group(
        GroupCreateRequest(name="rbac-scope", model_ref="Rbac/Model", clusterId="cluster-theirs"), created_by=None
    )
    _patch_target(monkeypatch, execution_id="exec-theirs", cluster_id="cluster-theirs")
    member = service.create_member(
        MemberCreateRequest(group_id=group.id, execution_id="exec-theirs"), published_by=None
    )

    scoped = _cluster_principal("cluster-mine")
    with pytest.raises(ForbiddenError):
        service.create_member(
            MemberCreateRequest(group_id=group.id, execution_id="exec-theirs"), published_by=None, principal=scoped
        )
    with pytest.raises(ForbiddenError):
        service.update_member(member.id, MemberUpdateRequest(status="unhealthy"), principal=scoped)
    with pytest.raises(ForbiddenError):
        service.delete_member(member.id, principal=scoped)

    # A maintainer scoped to the member's own cluster may still operate on it.
    allowed = _cluster_principal("cluster-theirs")
    updated = service.update_member(member.id, MemberUpdateRequest(status="active"), principal=allowed)
    assert updated.status == "active"
    service.delete_member(member.id, principal=allowed)
    assert service.members.get(member.id) is None


def test_usage_analytics_scopes_data_and_cascades_cluster_facets(monkeypatch):
    from llm_d_bench.db.dao.usage_record import UsageRecordDao
    from llm_d_bench.model_service.contracts import UsageRecord

    dao = UsageRecordDao()
    mine = _create_user("usage-mine")
    theirs = _create_user("usage-theirs")
    dao.create(
        UsageRecord(
            request_id="req-mine", user_id=mine.id, group_id="g-mine", group_name="Mine", cluster_id="cluster-mine"
        )
    )
    dao.create(
        UsageRecord(
            request_id="req-theirs",
            user_id=theirs.id,
            group_id="g-theirs",
            group_name="Theirs",
            cluster_id="cluster-theirs",
        )
    )
    service = _service()

    # A cluster-scoped maintainer never sees another cluster's records or facets.
    scoped = _cluster_principal("cluster-mine")
    result = service.usage_analytics(principal=scoped)
    cluster_keys = {opt["key"] for opt in result["facets"]["clusters"]}
    user_keys = {opt["key"] for opt in result["facets"]["users"]}
    model_keys = {opt["key"] for opt in result["facets"]["models"]}
    assert cluster_keys == {"cluster-mine"}
    assert user_keys == {mine.id}
    assert model_keys == {"g-mine"}
    assert result["totals"]["requests"] == 1

    # An unrestricted (global) caller sees everything, and the cluster facet
    # keeps listing every cluster even once a cluster filter narrows the data.
    unrestricted = service.usage_analytics(principal=None, cluster_ids=["cluster-mine"])
    assert unrestricted["totals"]["requests"] == 1
    assert {opt["key"] for opt in unrestricted["facets"]["clusters"]} == {"cluster-mine", "cluster-theirs"}
    assert {opt["key"] for opt in unrestricted["facets"]["users"]} == {mine.id}

    # Requesting a cluster the maintainer cannot reach yields no data (not a
    # silent fall-through to unrestricted results).
    denied = service.usage_analytics(principal=scoped, cluster_ids=["cluster-theirs"])
    assert denied["totals"]["requests"] == 0
    assert denied["facets"]["users"] == []


def test_model_service_is_scoped_to_one_cluster(monkeypatch):
    """A model service is per-cluster; providers must be in that same cluster."""
    service = _service()
    group = service.create_group(GroupCreateRequest(name="Qwen/Qwen3-0.6B", clusterId="cluster-a"), created_by=None)
    assert group.cluster_id == "cluster-a" and group.model_ref == "Qwen/Qwen3-0.6B"

    # The same public name may exist in a different cluster...
    other = service.create_group(GroupCreateRequest(name="Qwen/Qwen3-0.6B", clusterId="cluster-b"), created_by=None)
    assert other.cluster_id == "cluster-b"
    # ...but not twice in the same cluster.
    with pytest.raises(ModelServiceConflictError):
        service.create_group(GroupCreateRequest(name="Qwen/Qwen3-0.6B", clusterId="cluster-a"), created_by=None)

    # A provider from another cluster is rejected.
    _patch_target(monkeypatch, execution_id="exec-b", cluster_id="cluster-b")
    with pytest.raises(ModelServiceConflictError):
        service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-b"), published_by=None)

    # A provider in the service's own cluster is accepted.
    _patch_target(monkeypatch, execution_id="exec-a", cluster_id="cluster-a")
    member = service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-a"), published_by=None)
    assert member.group_id == group.id and member.cluster_id == "cluster-a"


def test_authorize_is_scoped_to_the_request_cluster(monkeypatch):
    """A model name in cluster A is not reachable through cluster B's gateway."""
    _patch_target(monkeypatch, execution_id="exec-a", cluster_id="cluster-a")
    user = _create_user("cluster-scoped-owner")
    service = _service()
    group = service.create_group(GroupCreateRequest(name="scoped", clusterId="cluster-a"), created_by=user.id)
    service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-a"), published_by=user.id)
    _, raw = service.tokens.create(user.id)

    ok = service.authorize_request(AuthorizeRequest(token=raw, model="scoped"), cluster_id="cluster-a")
    assert ok.group_id == group.id and ok.cluster_id == "cluster-a" and ok.execution_id == "exec-a"

    with pytest.raises(ModelServiceNotFoundError):
        service.authorize_request(AuthorizeRequest(token=raw, model="scoped"), cluster_id="cluster-b")
