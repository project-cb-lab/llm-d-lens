"""In-process Casbin adapter (design section 20.4).

Casbin is embedded in the Python authoritative side only. Rather than the
official ``sqlalchemy-adapter`` (which would bypass our DAO layer), policies
are synthesised from the already-resolved ``Principal`` — itself loaded from
``role_permissions``/``user_role_bindings``/``group_role_bindings`` via DAOs.

The Casbin model is RBAC-with-domains: ``dom`` is the cluster scope; ``obj`` is
``<domain>:<resource>`` and ``act`` is the action. A ``global`` binding is
expanded to the requested domain, so no custom domain-matching function is
needed. Resource-level shares and ownership are intentionally NOT modelled here
(design section 7.5) and stay in ``policy.py``.
"""

from __future__ import annotations

import casbin
from casbin import Model

from llm_d_bench.auth.contracts import Principal, ScopeType

ANY_DOMAIN = "__any__"

# Codes are colon-delimited (``domain:resource:action``), so the matcher uses
# ``keyMatch`` (``*`` wildcard only) and never ``keyMatch2``: keyMatch2 treats
# ``:segment`` as a path parameter, which would let ``deployment:agentic:create``
# accidentally match ``deployment:run:create``.
_MODEL_TEXT = """
[request_definition]
r = sub, dom, obj, act

[policy_definition]
p = sub, dom, obj, act

[role_definition]
g = _, _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub, r.dom) && keyMatch(r.dom, p.dom) && keyMatch(r.obj, p.obj) && keyMatch(r.act, p.act)
"""


def _split_code(code: str) -> tuple[str, str]:
    obj, _, act = code.rpartition(":")
    return obj, act


def _build_enforcer(principal: Principal, domain: str | None) -> casbin.Enforcer:
    model = Model()
    model.load_model_from_text(_MODEL_TEXT)
    enforcer = casbin.Enforcer(model)

    for role, codes in principal.role_permissions.items():
        for code in codes:
            obj, act = _split_code(code)
            enforcer.add_policy(role, "*", obj, act)

    requested = domain or ANY_DOMAIN
    for binding in principal.bindings:
        if binding.scope_type is ScopeType.RESOURCE:
            # Resource shares are evaluated by policy.py, not Casbin.
            continue
        if domain is None:
            # Action gate: union across every global/cluster binding.
            enforcer.add_grouping_policy(principal.user_id, binding.role, requested)
            continue
        if binding.scope_type is ScopeType.GLOBAL or binding.scope_cluster_id == domain:
            enforcer.add_grouping_policy(principal.user_id, binding.role, requested)
    return enforcer


def role_has_permission(principal: Principal, required: str, *, domain: str | None = None) -> bool:
    """Casbin-backed action gate: does an applicable role grant ``required``.

    ``domain`` is the target cluster id; ``None`` means "any cluster" (list
    endpoints, where scope filtering is handled separately).
    """
    enforcer = _build_enforcer(principal, domain)
    obj, act = _split_code(required)
    return bool(enforcer.enforce(principal.user_id, domain or ANY_DOMAIN, obj, act))
