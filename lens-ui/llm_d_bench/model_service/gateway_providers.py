"""Gateway Mode (llm-d) resource rendering for the model-service data plane.

Replaces the Agent Router renderer (Envoy AI Gateway ``AIServiceBackend`` /
``AIGatewayRoute``). Under the target design the cluster installs ONE shared
Gateway (provider-selectable), each deployment owns its ``InferencePool`` + EPP,
and each model service publishes an ``HTTPRoute`` -> ``InferencePool`` plus an
IPP model-mapping ``ConfigMap``.

Design reference: docs/design/model-service-llmd-routing-design.zh-CN.md
sections 5, 10.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from llm_d_bench.versions import gateway_provider_version

GATEWAY_API = "gateway.networking.k8s.io/v1"
#: Bare API group: ``targetRefs[].group`` / ``parentRefs[].group`` must be the
#: group only (never version-qualified), unlike ``apiVersion``.
GATEWAY_API_GROUP = "gateway.networking.k8s.io"
ENVOY_GATEWAY_API = "gateway.envoyproxy.io/v1alpha1"
#: Bare API group for ``EnvoyProxy`` parametersRef ``group``.
ENVOY_GATEWAY_API_GROUP = "gateway.envoyproxy.io"
#: Bare API group for ``backendRefs[].group`` / ``ReferenceGrant.to[].group``.
INFERENCE_POOL_API_GROUP = "inference.networking.k8s.io"
BASE_MODEL_HEADER = "X-Gateway-Base-Model-Name"
MANAGED_LABEL = "app.kubernetes.io/part-of"
MANAGED_VALUE = "lens-inference-gateway"
#: Records which Gateway provider (istio / gke / agentgateway / envoy-ai-gateway) a
#: resource belongs to, so the chosen implementation is visible on the cluster.
GATEWAY_PROVIDER_LABEL = "lens.ai/gateway-provider"
IPP_MANAGED_LABEL = "inference.llm-d.ai/ipp-managed"

GatewayProvider = Literal["istio", "gke", "agentgateway", "envoy-ai-gateway"]

#: Identity headers Lens' ``/authorize`` returns and the Gateway must inject upstream.
#: ``x-llm-d-inference-fairness-id`` is what the EPP turns into the ``fairness_id``
#: metric label the usage sync attributes back to a user/API key.
AUTHZ_HEADERS = ("x-llm-d-inference-fairness-id", "x-lens-user-id")
#: Header the authz server sets on 401/404 so istio can surface the reason downstream.
AUTHZ_ERROR_HEADER = "x-lens-error"
AUTHZ_SERVICE_NAME = "lens-authz"
#: Fixed in-cluster port of the ``lens-authz`` Service. The provider's ext_authz
#: config is baked at install time, so this port is stable; its Endpoints point at
#: Lens' live serving port, which can move without reinstalling the Gateway.
AUTHZ_SERVICE_PORT = 8081
AUTHZ_PATH = "/api/v1/internal/model-gateway/authorize"


def authz_path(cluster_id: str | None) -> str:
    """Authz path carrying the Gateway's cluster id.

    The same public model name can be served from several clusters, so Lens must
    resolve the model within the cluster the request arrived at; the ext_authz
    path is the one hook every provider forwards intact.
    """
    return f"{AUTHZ_PATH}/cluster/{cluster_id}" if cluster_id else AUTHZ_PATH


@dataclass(frozen=True)
class ProviderSpec:
    """How to install one supported Gateway provider and reference its GatewayClass."""

    key: GatewayProvider
    display_name: str
    gateway_class: str
    controller_name: str
    install_commands: tuple[tuple[str, ...], ...]
    envoy_client_traffic_policy: bool = False


#: Install commands/classes mirror the official llm-d gateway recipes
#: (``llm-d/guides/recipes/gateway/*``) and the Envoy AI Gateway guide.
#: Envoy AI Gateway base + InferencePool addon values shipped with Lens.
ENVOY_AI_GATEWAY_VALUES = Path(__file__).resolve().parent / "gateway_values" / "envoy-ai-gateway.values.yaml"
PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "envoy-ai-gateway": ProviderSpec(
        key="envoy-ai-gateway",
        display_name="Envoy AI Gateway",
        gateway_class="envoy-ai-gateway",
        controller_name="gateway.envoyproxy.io/gatewayclass-controller",
        install_commands=(
            (
                "helm",
                "upgrade",
                "--install",
                "eg",
                "oci://docker.io/envoyproxy/gateway-helm",
                "--version",
                gateway_provider_version("envoy-gateway") or "",
                "-n",
                "envoy-gateway-system",
                "--create-namespace",
                "-f",
                str(ENVOY_AI_GATEWAY_VALUES),
            ),
            (
                "helm",
                "upgrade",
                "--install",
                "aieg-crd",
                "oci://docker.io/envoyproxy/ai-gateway-crds-helm",
                "--version",
                gateway_provider_version("envoy-ai-gateway") or "",
                "-n",
                "envoy-ai-gateway-system",
                "--create-namespace",
            ),
            (
                "helm",
                "upgrade",
                "--install",
                "aieg",
                "oci://docker.io/envoyproxy/ai-gateway-helm",
                "--version",
                gateway_provider_version("envoy-ai-gateway") or "",
                "-n",
                "envoy-ai-gateway-system",
                "--create-namespace",
            ),
        ),
        envoy_client_traffic_policy=True,
    ),
    "istio": ProviderSpec(
        key="istio",
        display_name="Istio",
        gateway_class="istio",
        controller_name="istio.io/gateway-controller",
        install_commands=(
            ("istioctl", "install", "-y", "--set", "values.pilot.env.ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true"),
        ),
    ),
    "agentgateway": ProviderSpec(
        key="agentgateway",
        display_name="Agentgateway",
        gateway_class="agentgateway",
        controller_name="agentgateway.dev/agentgateway",
        install_commands=(
            (
                "helm",
                "upgrade",
                "--install",
                "agentgateway-crds",
                "oci://cr.agentgateway.dev/charts/agentgateway-crds",
                "--namespace",
                "agentgateway-system",
                "--create-namespace",
                "--version",
                gateway_provider_version("agentgateway") or "",
            ),
            (
                "helm",
                "upgrade",
                "--install",
                "agentgateway",
                "oci://cr.agentgateway.dev/charts/agentgateway",
                "--namespace",
                "agentgateway-system",
                "--create-namespace",
                "--version",
                gateway_provider_version("agentgateway") or "",
                "--set",
                "inferenceExtension.enabled=true",
            ),
        ),
    ),
    "gke": ProviderSpec(
        key="gke",
        display_name="GKE Gateway",
        gateway_class="gke-l7-regional-external-managed",
        controller_name="networking.gke.io/gateway",
        install_commands=(),
    ),
}

DEFAULT_PROVIDER: GatewayProvider = "envoy-ai-gateway"


def provider_spec(provider: str) -> ProviderSpec:
    """Resolve a provider key to its spec; raises ``ValueError`` when unsupported."""
    try:
        return PROVIDER_SPECS[provider]
    except KeyError as error:
        raise ValueError(f"unsupported gateway provider: {provider}") from error


def available_providers() -> list[str]:
    return sorted(PROVIDER_SPECS)


def default_pool_name(service: str | None) -> str | None:
    """Recover the deployment's InferencePool name from its EPP Service name.

    The llm-d router chart names the InferencePool after the release and the EPP
    Service ``<release>-epp``; strip the suffix so a route can be rendered.
    """
    if not service:
        return None
    return service[:-4] if service.endswith("-epp") else service


def qualified_pool_name(pool_name: str | None, namespace: str | None) -> str | None:
    """Return ``<namespace>/<pool>`` so a default pool name is unique.

    Two deployments in different namespaces can own pools with the same bare name;
    the qualified form is what a member stores (and the topology shows), while the
    HTTPRoute is rendered from the split ``(namespace, pool)`` pair.
    """
    if not pool_name:
        return None
    if "/" in pool_name or not namespace:
        return pool_name
    return f"{namespace}/{pool_name}"


def split_pool_name(value: str | None) -> tuple[str | None, str | None]:
    """Split a stored pool value into ``(namespace, pool_name)``."""
    if not value:
        return None, None
    namespace, sep, name = value.partition("/")
    if sep and name:
        return namespace or None, name
    return None, value


def resource_name(model_name: str, *, fallback: str = "model") -> str:
    """Return a DNS-1123 resource name for a model id (e.g. ``Qwen/Qwen3-0.6B``).

    Model ids contain ``/`` and dots, neither valid in ``metadata.name``; route,
    ConfigMap and ReferenceGrant names must therefore be slugged while the model
    id itself stays in the header match / ``baseModel`` value.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", model_name.lower()).strip("-")
    return slug[:253] or fallback


@dataclass(frozen=True)
class PoolBinding:
    """One ``InferencePool`` backend of a model service's HTTPRoute."""

    pool_name: str
    port: int
    namespace: str | None = None


def _envoy_service(service_type: str, service_port: int | None) -> dict:
    """EnvoyProxy ``envoyService`` block, pinning the exposed NodePort when set."""
    service: dict = {"type": service_type}
    if service_port:
        # Strategic-merge patch onto the generated Service; Service ports merge by
        # ``port``, so this only sets the nodePort on the Gateway's http port.
        service["patch"] = {
            "type": "StrategicMerge",
            "value": {"spec": {"ports": [{"port": 80, "nodePort": service_port}]}},
        }
    return service


def render_inference_gateway(
    *,
    namespace: str,
    gateway_name: str,
    provider: str,
    gateway_class: str | None = None,
    buffer_limit: str = "50Mi",
    service_type: str | None = None,
    service_port: int | None = None,
    authz_host: str | None = None,
    authz_port: int = AUTHZ_SERVICE_PORT,
    authz_cluster_id: str | None = None,
) -> str:
    """Render the cluster-level shared Gateway (GatewayClass + Gateway).

    The whole cluster shares one Gateway; deployments and model routes reference it.
    ``service_type`` (e.g. ``NodePort`` on a cluster with no LoadBalancer) sets the
    generated data-plane Service type through the provider's own mechanism: an
    ``EnvoyProxy`` for Envoy Gateway, or a ``ConfigMap`` ``infrastructure.parametersRef``
    for Istio (matching llm-d's gateway recipes).
    """
    spec = provider_spec(provider)
    labels = {MANAGED_LABEL: MANAGED_VALUE, GATEWAY_PROVIDER_LABEL: spec.key}
    gateway_labels = dict(labels)
    if spec.key == "istio":
        # llm-d's Istio recipe requires this label for the EPP inference ext_proc.
        gateway_labels["istio.io/enable-inference-extproc"] = "true"
    gateway_class_name = gateway_class or spec.gateway_class
    gateway_spec: dict = {
        "gatewayClassName": gateway_class_name,
        "listeners": [{"name": "http", "protocol": "HTTP", "port": 80}],
    }
    documents: list[dict] = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": labels}},
        {
            "apiVersion": GATEWAY_API,
            "kind": "GatewayClass",
            "metadata": {"name": gateway_class_name, "labels": labels},
            "spec": {"controllerName": spec.controller_name},
        },
    ]
    if service_type and spec.envoy_client_traffic_policy:
        proxy_name = f"{gateway_name}-proxy"
        gateway_spec["infrastructure"] = {
            "parametersRef": {
                "group": ENVOY_GATEWAY_API_GROUP,
                "kind": "EnvoyProxy",
                "name": proxy_name,
            }
        }
        documents.append(
            {
                "apiVersion": ENVOY_GATEWAY_API,
                "kind": "EnvoyProxy",
                "metadata": {"name": proxy_name, "namespace": namespace, "labels": labels},
                "spec": {
                    "provider": {
                        "type": "Kubernetes",
                        "kubernetes": {"envoyService": _envoy_service(service_type, service_port)},
                    }
                },
            }
        )
    elif service_type and spec.key == "istio":
        config_name = f"{gateway_name}-config"
        gateway_spec["infrastructure"] = {"parametersRef": {"group": "", "kind": "ConfigMap", "name": config_name}}
        service_spec = ["spec:", f"  type: {service_type}"]
        if service_port:
            service_spec += ["  ports:", "  - name: http", "    port: 80", f"    nodePort: {service_port}"]
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": config_name, "namespace": namespace, "labels": labels},
                "data": {"service": "\n".join(service_spec) + "\n"},
            }
        )
    documents.append(
        {
            "apiVersion": GATEWAY_API,
            "kind": "Gateway",
            "metadata": {"name": gateway_name, "namespace": namespace, "labels": gateway_labels},
            "spec": gateway_spec,
        }
    )
    if spec.envoy_client_traffic_policy:
        documents.append(
            {
                "apiVersion": ENVOY_GATEWAY_API,
                "kind": "ClientTrafficPolicy",
                "metadata": {"name": f"{gateway_name}-buffer", "namespace": namespace, "labels": labels},
                "spec": {
                    "targetRefs": [{"group": GATEWAY_API_GROUP, "kind": "Gateway", "name": gateway_name}],
                    "connection": {"bufferLimit": buffer_limit},
                },
            }
        )
    if authz_host:
        documents.extend(
            render_gateway_ext_auth(
                provider=spec.key,
                namespace=namespace,
                gateway_name=gateway_name,
                authz_host=authz_host,
                authz_port=authz_port,
                authz_cluster_id=authz_cluster_id,
            )
        )
    return yaml.safe_dump_all(documents, sort_keys=False)


def render_gateway_ext_auth(
    *,
    provider: str,
    namespace: str,
    gateway_name: str,
    authz_host: str,
    authz_port: int = AUTHZ_SERVICE_PORT,
    authz_cluster_id: str | None = None,
) -> list[dict]:
    """Render ext_authz resources that inject Lens' identity headers upstream.

    The Gateway calls Lens' ``/internal/model-gateway/authorize`` for every request
    and copies the returned identity headers (notably
    ``x-llm-d-inference-fairness-id``) onto the upstream request, which the EPP then
    records as its ``fairness_id`` metric label. All three HTTP-capable providers
    share an in-cluster ``lens-authz`` Service/Endpoints that points at the Lens
    backend (``authz_host``), so the Gateway does not need to reach the Lens host
    directly. ``authz_port`` is the *destination* port on the Lens host (Lens' own
    live serving port); the Service itself listens on the stable
    :data:`AUTHZ_SERVICE_PORT` that the provider config targets. GKE's managed
    Gateway has no Gateway-API ext_authz hook (it needs Cloud Service Mesh/IAP),
    so it renders nothing.
    """
    if provider == "gke":
        return []
    labels = {MANAGED_LABEL: MANAGED_VALUE, GATEWAY_PROVIDER_LABEL: provider}
    target = [{"group": GATEWAY_API_GROUP, "kind": "Gateway", "name": gateway_name}]
    authz_route = authz_path(authz_cluster_id)
    documents: list[dict] = [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": AUTHZ_SERVICE_NAME, "namespace": namespace, "labels": labels},
            "spec": {
                # Stable provider-facing port; the Endpoints below route it to the
                # Lens host's live serving port.
                "ports": [
                    {"name": "http", "port": AUTHZ_SERVICE_PORT, "targetPort": authz_port, "protocol": "TCP"}
                ]
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Endpoints",
            "metadata": {"name": AUTHZ_SERVICE_NAME, "namespace": namespace, "labels": labels},
            "subsets": [
                {
                    "addresses": [{"ip": authz_host}],
                    "ports": [{"name": "http", "port": authz_port, "protocol": "TCP"}],
                }
            ],
        },
    ]
    policy_name = f"{gateway_name}-authz"
    if provider == "envoy-ai-gateway":
        documents.append(
            {
                "apiVersion": ENVOY_GATEWAY_API,
                "kind": "SecurityPolicy",
                "metadata": {"name": policy_name, "namespace": namespace, "labels": labels},
                "spec": {
                    "targetRefs": target,
                    "extAuth": {
                        "http": {
                            "path": authz_route,
                            "backendRefs": [{"name": AUTHZ_SERVICE_NAME, "port": AUTHZ_SERVICE_PORT}],
                            "headersToBackend": list(AUTHZ_HEADERS),
                        }
                    },
                },
            }
        )
    elif provider == "agentgateway":
        documents.append(
            {
                "apiVersion": "agentgateway.dev/v1alpha1",
                "kind": "AgentgatewayPolicy",
                "metadata": {"name": policy_name, "namespace": namespace, "labels": labels},
                "spec": {
                    "targetRefs": target,
                    "traffic": {
                        "extAuth": {
                            "backendRef": {
                                "name": AUTHZ_SERVICE_NAME,
                                "namespace": namespace,
                                "port": AUTHZ_SERVICE_PORT,
                            },
                            # ``path`` is a CEL expression; a string literal pins the authz path.
                            "http": {"path": f'"{authz_route}"', "allowedResponseHeaders": list(AUTHZ_HEADERS)},
                        }
                    },
                },
            }
        )
    elif provider == "istio":
        documents.append(
            {
                "apiVersion": "security.istio.io/v1",
                "kind": "AuthorizationPolicy",
                "metadata": {"name": policy_name, "namespace": namespace, "labels": labels},
                "spec": {
                    "targetRefs": target,
                    "action": "CUSTOM",
                    "provider": {"name": AUTHZ_SERVICE_NAME},
                    "rules": [{}],
                },
            }
        )
    return documents


def istio_authz_overlay(namespace: str, authz_port: int = AUTHZ_SERVICE_PORT, cluster_id: str | None = None) -> str:
    """IstioOperator overlay registering the Lens HTTP ext_authz provider.

    ``authz_port`` is the stable in-cluster ``lens-authz`` Service port (not the
    Lens host's port, which the Service's Endpoints route to). Passed to
    ``istioctl install -f`` (``--set`` cannot express the nested lists
    reliably). Istio's HTTP provider field is ``envoyExtAuthzHttp``;
    ``Authorization`` is not sent to the authz server by default, and the model is
    read from the ``X-Gateway-Base-Model-Name`` header (Istio's request-body option
    generates an invalid Envoy ``with_request_body``).
    """
    spec = {
        "apiVersion": "install.istio.io/v1alpha1",
        "kind": "IstioOperator",
        "spec": {
            "values": {"pilot": {"env": {"ENABLE_GATEWAY_API_INFERENCE_EXTENSION": "true"}}},
            "meshConfig": {
                "extensionProviders": [
                    {
                        "name": AUTHZ_SERVICE_NAME,
                        "envoyExtAuthzHttp": {
                            "service": f"{AUTHZ_SERVICE_NAME}.{namespace}.svc.cluster.local",
                            "port": authz_port,
                            "pathPrefix": authz_path(cluster_id),
                            "includeRequestHeadersInCheck": [
                                "authorization",
                                "x-lens-model",
                                "x-gateway-base-model-name",
                            ],
                            "headersToUpstreamOnAllow": list(AUTHZ_HEADERS),
                            # ext_authz only forwards the status, not the body; expose
                            # the reason (e.g. "model not found") as a downstream header.
                            "headersToDownstreamOnDeny": [AUTHZ_ERROR_HEADER],
                        },
                    }
                ]
            },
        },
    }
    return yaml.safe_dump(spec, sort_keys=False)


def render_model_route(
    *,
    namespace: str,
    model_name: str,
    base_model: str,
    pool: PoolBinding,
    gateway_name: str,
    gateway_namespace: str | None = None,
    adapters: Sequence[str] = (),
) -> str:
    """Render a model service's HTTPRoute + IPP model-mapping ConfigMap.

    The route references exactly ONE deployment-owned ``InferencePool`` backend.
    llm-d's data plane is one EPP per InferencePool (`llm-d-router` watches a single
    pool by name) and its gateway chart renders one pool per HTTPRoute, so Lens does
    not aggregate or weight multiple pools into one route.
    """
    safe_name = resource_name(model_name)
    parent_ref: dict = {"group": GATEWAY_API_GROUP, "kind": "Gateway", "name": gateway_name}
    if gateway_namespace:
        parent_ref["namespace"] = gateway_namespace
    # No ``port``: an InferencePool backend resolves to the pool's own
    # ``spec.targetPorts`` (llm-d's llm-d-router-gateway chart does the same);
    # the member's EPP Service port (80) must not be used here.
    pool_ref = {
        "group": INFERENCE_POOL_API_GROUP,
        "kind": "InferencePool",
        "name": pool.pool_name,
    }
    if pool.namespace and pool.namespace != namespace:
        pool_ref["namespace"] = pool.namespace
    http_route = {
        "apiVersion": GATEWAY_API,
        "kind": "HTTPRoute",
        "metadata": {"name": safe_name, "namespace": namespace, "labels": {MANAGED_LABEL: MANAGED_VALUE}},
        "spec": {
            "parentRefs": [parent_ref],
            "rules": [
                {
                    "matches": [{"headers": [{"type": "Exact", "name": BASE_MODEL_HEADER, "value": base_model}]}],
                    "backendRefs": [pool_ref],
                }
            ],
        },
    }
    # A cross-namespace pool reference needs a ReferenceGrant in the pool namespace.
    grants = [
        {
            "apiVersion": "gateway.networking.k8s.io/v1beta1",
            "kind": "ReferenceGrant",
            "metadata": {"name": f"{safe_name}-pool-grant", "namespace": pool_ns},
            "spec": {
                "from": [{"group": GATEWAY_API_GROUP, "kind": "HTTPRoute", "namespace": namespace}],
                "to": [{"group": INFERENCE_POOL_API_GROUP, "kind": "InferencePool"}],
            },
        }
        for pool_ns in sorted({pool.namespace} if pool.namespace and pool.namespace != namespace else set())
    ]
    model_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{safe_name}-model-map",
            "namespace": namespace,
            "labels": {IPP_MANAGED_LABEL: "true", MANAGED_LABEL: MANAGED_VALUE},
        },
        "data": {
            "baseModel": base_model,
            "adapters": "".join(f"- {adapter}\n" for adapter in adapters),
        },
    }
    return yaml.safe_dump_all([http_route, *grants, model_map], sort_keys=False)


#: DaemonSet that raises the per-uid inotify instance limit on every node.
NODE_SYSCTL_NAME = "lens-node-sysctl"
#: Image must provide ``sh``/``nsenter``/``sysctl``; override with
#: ``LENS_NODE_SYSCTL_IMAGE`` when the cluster cannot pull the default.
NODE_SYSCTL_IMAGE = "busybox:1.36"


def render_node_inotify_daemonset(limit: int, namespace: str, image: str | None = None) -> str:
    """Render a privileged DaemonSet that sets ``fs.inotify.max_user_instances``.

    The limit is per real uid, so a node running many root containers exhausts
    kind's default (128) and controllers fail with "too many open files". The
    DaemonSet enters the host namespaces and applies the sysctl on every node.
    """
    sysctl_image = (image or NODE_SYSCTL_IMAGE).strip()
    command = f"nsenter -t 1 -m -u -i -n -p -- sysctl -w fs.inotify.max_user_instances={int(limit)} && sleep infinity"
    manifest = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {
            "name": NODE_SYSCTL_NAME,
            "namespace": namespace,
            "labels": {MANAGED_LABEL: NODE_SYSCTL_NAME},
        },
        "spec": {
            "selector": {"matchLabels": {"app": NODE_SYSCTL_NAME}},
            "template": {
                "metadata": {"labels": {"app": NODE_SYSCTL_NAME}},
                "spec": {
                    "hostPID": True,
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [
                        {
                            "name": "sysctl",
                            "image": sysctl_image,
                            "securityContext": {"privileged": True},
                            "command": ["sh", "-c", command],
                        }
                    ],
                },
            },
        },
    }
    return yaml.safe_dump(manifest, sort_keys=False)


def ensure_istioctl(version: str) -> Path:
    """Return a local ``istioctl`` for ``version``, downloading it once.

    ``istioctl install`` uses the binary's own version, so honoring the pinned
    stack version means running a matching binary. Istio release versions carry
    no leading ``v`` (e.g. ``1.29.2``); the download is cached under the Lens
    cache and reused.
    """
    import platform  # noqa: PLC0415
    import tarfile  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from llm_d_bench.utils.paths import storage_path  # noqa: PLC0415

    machine = platform.machine().lower()
    arch = "arm64" if machine in {"aarch64", "arm64"} else "amd64"
    root = _Path(storage_path("cache", "tools", "istioctl", version))
    binary = root / "istioctl"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    root.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/istio/istio/releases/download/{version}/istioctl-{version}-linux-{arch}.tar.gz"
    archive = root / "istioctl.tar.gz"
    with httpx.Client(follow_redirects=True, timeout=180) as client:
        response = client.get(url)
        response.raise_for_status()
        archive.write_bytes(response.content)
    with tarfile.open(archive) as tar:
        tar.extractall(root)  # noqa: S202 - trusted Istio release archive
    binary.chmod(0o755)
    archive.unlink(missing_ok=True)
    return binary
