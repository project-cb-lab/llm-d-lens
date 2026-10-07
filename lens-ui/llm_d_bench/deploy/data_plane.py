"""Deployment data-plane selection (design: docs/design/deployment-data-plane-design.md).

A deployment's data plane decides how a benchmark or a model service reaches it:

- ``shared_gateway``: the cluster's shared Gateway is the data plane; the
  deployment runs no proxy and the EPP is reached as its ext_proc.
- ``standalone_router``: the deployment's router-chart proxy fronts the EPP.
- ``direct``: no router; the model server Service is called directly.
- ``external``: the provider owns its data plane; Lens renders no router.

Guides declare a static ``data_plane_kind`` (``llm-d-router`` for guides built on
the llm-d router chart, ``direct`` for a plain vLLM Service, ``external`` for
provider-owned resources). The framework combines that kind with whether the
deployment is evaluation-owned to pick the concrete data plane; providers never
branch on guide names.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml

#: The concrete data plane assigned to a deployment.
DataPlane = Literal["shared_gateway", "standalone_router", "direct", "external"]
#: The static data plane a guide declares.
DataPlaneKind = Literal["llm-d-router", "direct", "external"]

#: Helm values that switch the llm-d router chart from its own proxy to the
#: cluster's shared Gateway. Shared by every ``llm-d-router`` guide.
GATEWAY_MODE_VALUES_PATH = (
    Path(__file__).resolve().parent / "providers" / "router_values" / "router-gateway-mode.values.yaml"
)

#: Helm values that force the EPP's ext_proc to plaintext h2c. Applied to every
#: ``llm-d-router`` deployment: the chart renders both the EPP flag and its own
#: proxy's ext_proc cluster from it, so the deployment's proxy and the shared
#: Gateway stay consistent (an EPP flipped to plaintext for the Gateway would
#: otherwise leave the proxy on TLS and 503 every request).
PLAINTEXT_EPP_VALUES_PATH = (
    Path(__file__).resolve().parent / "providers" / "router_values" / "router-plaintext-epp.values.yaml"
)


def _resolved_data_plane(execution_context: Mapping[str, Any] | None) -> DataPlane:
    """The concrete data plane for a Helm context, or the evaluation-owned default.

    Uses the framework-resolved ``data_plane`` when present; otherwise falls back
    to the evaluation-owned check for callers that only pass a provenance.
    """
    data_plane = _data_plane_from_context(execution_context)
    if data_plane is not None:
        return data_plane
    return "standalone_router" if evaluation_owned((execution_context or {}).get("provenance")) else "shared_gateway"


def evaluation_owned(provenance: Mapping[str, Any] | None) -> bool:
    """True for ephemeral deployments an evaluation created for benchmarking.

    Those always keep their own router proxy (``standalone_router``) so several
    same-model deployments can be benchmarked without colliding on the shared
    Gateway's single base-model route key.
    """
    provenance = provenance or {}
    return bool(
        provenance.get("evaluate_workflow")
        or provenance.get("evaluation_id")
        or provenance.get("evaluation_case_id")
    )


def declared_data_plane_kind(execution: object) -> DataPlaneKind:
    """The ``data_plane_kind`` a deployment's provider declared.

    Falls back to legacy contracts that only carry ``shares_gateway`` (and, older
    still, the ``optimized-baseline`` provider ref, the only guide that ever
    disabled its own proxy).
    """
    artifact = getattr(execution, "artifact", None)
    payload = getattr(artifact, "rendered_payload", None)
    value = getattr(payload, "value", None) or {}
    contract = value.get("deployment_contract") or {}
    kind = contract.get("data_plane_kind")
    if kind in {"llm-d-router", "direct", "external"}:
        return kind
    if contract.get("shares_gateway"):
        return "llm-d-router"
    if value.get("provider_ref") == "optimized-baseline":
        return "llm-d-router"
    return "external"


def deployment_uses_shared_gateway(execution: object) -> bool:
    """True when a benchmark reaches this deployment through the shared Gateway.

    Evaluation-owned deployments keep their own proxy even for ``llm-d-router``
    guides, so they are always reached directly -- several same-model
    deployments created for a multi-deployment comparison would otherwise
    collide on the shared Gateway's single base-model route key. This is the
    Design Configuration path: it intentionally keeps the deployment's own
    router-chart proxy rather than the shared Gateway, even when the
    deployment also happens to be published as a Model Service elsewhere (that
    separate "existing endpoint" path is the one that goes through the shared
    Gateway, with its own access-token flow).

    Prefers the execution's recorded ``data_plane`` (the concrete data plane
    ``resolve_data_plane`` actually rendered at deploy time -- e.g. a
    ``llm-d-router`` deployment that fell back to ``standalone_router``
    because the cluster had no ready shared Gateway yet). Falls back to the
    guide's static ``data_plane_kind`` only for executions recorded before
    that field existed, where every ``llm-d-router`` guide did render the
    shared Gateway values.
    """
    provenance = getattr(execution, "provenance", {}) or {}
    if evaluation_owned(provenance):
        return False
    recorded = getattr(execution, "data_plane", None)
    if recorded is not None:
        return recorded == "shared_gateway"
    return declared_data_plane_kind(execution) == "llm-d-router"


async def resolve_data_plane(
    kind: DataPlaneKind,
    *,
    provenance: Mapping[str, Any] | None,
    cluster_id: str | None,
) -> tuple[DataPlane, str | None]:
    """Pick the concrete data plane for a deployment and any fallback warning.

    A ``llm-d-router`` deployment uses the shared Gateway only when the cluster
    actually has a ready one; otherwise it falls back to its own router proxy so
    it stays reachable (returning an operator-visible warning). Evaluation-owned
    deployments always keep their own proxy.
    """
    if kind == "direct":
        return "direct", None
    if kind != "llm-d-router":
        return "external", None
    if evaluation_owned(provenance):
        return "standalone_router", None
    if not cluster_id:
        return "standalone_router", "no cluster is bound to this deployment; using its own router proxy"
    from llm_d_bench.model_service.gateway_ops import GatewayOpsService  # noqa: PLC0415

    try:
        gateway_url = await GatewayOpsService().cluster_gateway_base_url(cluster_id)
    except Exception as error:  # noqa: BLE001 - deploy must not fail on a Gateway lookup
        return (
            "standalone_router",
            f"shared Gateway status could not be resolved ({error}); using the deployment's own router proxy",
        )
    if gateway_url:
        return "shared_gateway", None
    return (
        "standalone_router",
        "the cluster has no ready shared Gateway; using the deployment's own router proxy",
    )


def _data_plane_from_context(execution_context: Mapping[str, Any] | None) -> DataPlane | None:
    value = (execution_context or {}).get("data_plane")
    return value if value in {"shared_gateway", "standalone_router", "direct", "external"} else None


def router_data_plane_args(execution_context: Mapping[str, Any] | None) -> list[str]:
    """Extra Helm ``--values`` args for the llm-d router chart.

    Always forces the EPP's ext_proc to plaintext (see
    ``PLAINTEXT_EPP_VALUES_PATH``). Additionally disables the deployment's own
    proxy only for ``shared_gateway`` (``router-gateway-mode.values.yaml``);
    evaluation-owned deployments and the no-ready-Gateway fallback keep it.
    """
    data_plane = _resolved_data_plane(execution_context)
    if data_plane in {"direct", "external"}:
        return []
    args = ["--values", str(PLAINTEXT_EPP_VALUES_PATH)]
    if data_plane == "shared_gateway":
        args += ["--values", str(GATEWAY_MODE_VALUES_PATH)]
    return args


def router_data_plane_effective_values(content: str, execution_context: Mapping[str, Any] | None) -> str:
    """Merge the data-plane values into saved effective router values.

    The saved-bundle install path renders one effective values file, so the
    values are merged into it rather than passed as separate ``--values``. The
    plaintext EPP flag is always applied; the shared-Gateway proxy disable only
    for ``shared_gateway`` (see :func:`router_data_plane_args`).
    """
    data_plane = _resolved_data_plane(execution_context)
    if data_plane in {"direct", "external"}:
        return content
    base_values = yaml.safe_load(content) or {}
    plaintext_values = yaml.safe_load(PLAINTEXT_EPP_VALUES_PATH.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(base_values, plaintext_values)
    if data_plane == "shared_gateway":
        gateway_values = yaml.safe_load(GATEWAY_MODE_VALUES_PATH.read_text(encoding="utf-8")) or {}
        merged = _deep_merge(merged, gateway_values)
    return yaml.safe_dump(merged, sort_keys=False)


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
