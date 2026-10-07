"""Install the pinned Gateway API and Gateway API Inference Extension CRDs.

The llm-d gateway recipes apply two upstream CRD bundles before installing a
Gateway. Lens pins their versions in the stack profile and applies the same
manifests, so the CRD versions follow the Lens release instead of whatever a
provider chart happens to bundle.
"""

from __future__ import annotations

from typing import Any

from llm_d_bench.utils.kubernetes import scoped_runner
from llm_d_bench.versions import stack

_APPLY_TIMEOUT = 300.0
_QUERY_TIMEOUT = 60.0

#: A representative CRD required by each bundle.
_REQUIRED_CRDS = {
    "gateway_api": ("gateways.gateway.networking.k8s.io", "httproutes.gateway.networking.k8s.io"),
    "gateway_api_inference_extension": ("inferencepools.inference.networking.k8s.io",),
}


def manifest_urls() -> dict[str, str]:
    """The pinned upstream CRD manifest URLs."""
    current = stack()
    return {
        "gateway_api": (
            "https://github.com/kubernetes-sigs/gateway-api/releases/download/"
            f"{current.k8s_gateway_api}/standard-install.yaml"
        ),
        "gateway_api_inference_extension": (
            "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/"
            f"{current.k8s_gateway_api_inference_extension}/v1-manifests.yaml"
        ),
    }


async def apply_gateway_crds(cluster_id: str) -> dict[str, Any]:
    """Apply both pinned CRD bundles; returns a per-bundle result.

    A bundle can contain optional resources a given Kubernetes version rejects
    (e.g. Gateway API's TLS-routes CRD uses the CEL ``isIP`` function, which
    needs a newer control plane). The bundle is therefore judged by whether the
    *required* CRDs are present after the apply, not by ``kubectl``'s exit code.
    """
    runner = scoped_runner(cluster_id)
    results: dict[str, Any] = {}
    for key, url in manifest_urls().items():
        # Server-side apply handles CRDs that already exist from an earlier
        # (client-side) install, which plain `kubectl apply` rejects with a
        # missing-resourceVersion update error.
        result = await runner.run(
            ["kubectl", "apply", "--server-side", "--force-conflicts", "-f", url], timeout=_APPLY_TIMEOUT
        )
        message = (result.stderr or result.stdout or "").strip()
        results[key] = {"ok": result.returncode == 0, "message": message[:500]}
    status = await gateway_crds_status(cluster_id)
    for key, presence in status.items():
        results[key]["ok"] = presence == "ready"
    return results


async def gateway_crds_status(cluster_id: str) -> dict[str, str]:
    """Report ``ready``/``missing`` per CRD bundle by listing the CRDs."""
    runner = scoped_runner(cluster_id)
    result = await runner.run(["kubectl", "get", "crd", "-o", "name"], timeout=_QUERY_TIMEOUT)
    names = result.stdout.split() if result.returncode == 0 else []
    return {
        key: "ready" if all(any(name.endswith(crd) for name in names) for crd in required) else "missing"
        for key, required in _REQUIRED_CRDS.items()
    }
