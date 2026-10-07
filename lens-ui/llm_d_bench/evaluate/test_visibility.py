"""Owner/cluster-aware visibility for evaluate workflow and benchmark records."""

from __future__ import annotations

import importlib
from uuid import uuid4

from llm_d_bench.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_bench.db.dao.cluster import ClusterDao

router = importlib.import_module("llm_d_bench.evaluate.router")

#: Synthetic self-scoped workload role: the built-in ``end-user`` is now
#: model-service-only (D15), so self-scoped evaluate visibility is exercised
#: through this role.
_SELF_SCOPED = frozenset({"evaluate:run:read"})


def _self_scoped_principal(cluster_id: str, user_id: str) -> Principal:
    return Principal(
        user_id=user_id,
        username=user_id,
        bindings=(RoleBinding("workload", ScopeType.CLUSTER, scope_cluster_id=cluster_id),),
        role_permissions={"workload": _SELF_SCOPED},
        role_self_scoped={"workload": _SELF_SCOPED},
    )


def test_self_scoped_user_sees_own_evaluate_record_but_not_another_user():
    cluster_id = ClusterDao().create(f"eval-{uuid4().hex[:6]}", "", "apiVersion: v1\nkind: Config").id
    owner = _self_scoped_principal(cluster_id, "owner")
    other = _self_scoped_principal(cluster_id, "other")
    record = {"id": "wf-1", "cluster_id": cluster_id, "owner_user_id": "owner"}

    assert router._evaluate_record_readable(
        owner,
        record,
        permission="evaluate:run:read",
        resource_type="evaluate_workflow",
    )
    # A same-cluster self-scoped user cannot see another user's record.
    assert not router._evaluate_record_readable(
        other,
        record,
        permission="evaluate:run:read",
        resource_type="evaluate_workflow",
    )
