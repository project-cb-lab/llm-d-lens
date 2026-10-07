"""Tests for the llm-d Gateway Mode resource renderers."""

from __future__ import annotations

import pytest
import yaml

from llm_d_bench.model_service.gateway_providers import (
    BASE_MODEL_HEADER,
    PROVIDER_SPECS,
    PoolBinding,
    available_providers,
    default_pool_name,
    provider_spec,
    render_inference_gateway,
    render_model_route,
    resource_name,
)


def _docs(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if doc]


def test_available_providers_covers_the_supported_gateways():
    assert available_providers() == ["agentgateway", "envoy-ai-gateway", "gke", "istio"]
    assert set(PROVIDER_SPECS) == set(available_providers())


def test_provider_spec_rejects_unknown_provider():
    with pytest.raises(ValueError, match="unsupported gateway provider"):
        provider_spec("traefik")


def test_default_pool_name_strips_epp_suffix():
    assert default_pool_name("optimized-baseline-epp") == "optimized-baseline"
    assert default_pool_name("optimized-baseline") == "optimized-baseline"
    assert default_pool_name(None) is None


def test_resource_name_slugifies_model_ids():
    assert resource_name("Qwen/Qwen3-0.6B") == "qwen-qwen3-0-6b"
    assert resource_name("///") == "model"


def test_render_model_route_uses_dns_safe_names_for_model_ids():
    docs = _docs(
        render_model_route(
            namespace="lens-gateway",
            model_name="Qwen/Qwen3-0.6B",
            base_model="Qwen/Qwen3-0.6B",
            pool=PoolBinding("optimized-baseline", 8000, "ns-a"),
            gateway_name="lens-inference-gateway",
        )
    )
    route = next(doc for doc in docs if doc["kind"] == "HTTPRoute")
    assert route["metadata"]["name"] == "qwen-qwen3-0-6b"
    assert route["spec"]["rules"][0]["matches"][0]["headers"][0]["value"] == "Qwen/Qwen3-0.6B"
    grant = next(doc for doc in docs if doc["kind"] == "ReferenceGrant")
    assert grant["metadata"]["name"] == "qwen-qwen3-0-6b-pool-grant"
    config_map = next(doc for doc in docs if doc["kind"] == "ConfigMap")
    assert config_map["metadata"]["name"] == "qwen-qwen3-0-6b-model-map"


def test_render_inference_gateway_wires_envoy_proxy_for_service_type():
    docs = _docs(
        render_inference_gateway(
            namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            provider="envoy-ai-gateway",
            service_type="NodePort",
            service_port=30999,
        )
    )
    gateway = next(doc for doc in docs if doc["kind"] == "Gateway")
    assert gateway["spec"]["infrastructure"]["parametersRef"] == {
        "group": "gateway.envoyproxy.io",
        "kind": "EnvoyProxy",
        "name": "lens-inference-gateway-proxy",
    }
    proxy = next(doc for doc in docs if doc["kind"] == "EnvoyProxy")
    assert proxy["metadata"]["name"] == "lens-inference-gateway-proxy"
    envoy_service = proxy["spec"]["provider"]["kubernetes"]["envoyService"]
    assert envoy_service["type"] == "NodePort"
    assert envoy_service["patch"] == {
        "type": "StrategicMerge",
        "value": {"spec": {"ports": [{"port": 80, "nodePort": 30999}]}},
    }


def test_render_inference_gateway_istio_uses_configmap_and_extproc_label():
    docs = _docs(
        render_inference_gateway(
            namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            provider="istio",
            service_type="NodePort",
            service_port=30998,
        )
    )
    gateway = next(doc for doc in docs if doc["kind"] == "Gateway")
    assert gateway["metadata"]["labels"]["istio.io/enable-inference-extproc"] == "true"
    assert gateway["spec"]["infrastructure"]["parametersRef"] == {
        "group": "",
        "kind": "ConfigMap",
        "name": "lens-inference-gateway-config",
    }
    config = next(doc for doc in docs if doc["kind"] == "ConfigMap")
    assert "type: NodePort" in config["data"]["service"]
    assert "nodePort: 30998" in config["data"]["service"]


def test_render_inference_gateway_istio_extproc_label_without_service_type():
    docs = _docs(
        render_inference_gateway(namespace="lens-gateway", gateway_name="lens-inference-gateway", provider="istio")
    )
    gateway = next(doc for doc in docs if doc["kind"] == "Gateway")
    assert gateway["metadata"]["labels"]["istio.io/enable-inference-extproc"] == "true"
    assert "infrastructure" not in gateway["spec"]


def test_render_inference_gateway_ignores_service_type_for_other_providers():
    docs = _docs(
        render_inference_gateway(namespace="inference", gateway_name="gw", provider="gke", service_type="NodePort")
    )
    assert all(doc["kind"] not in {"EnvoyProxy", "ConfigMap"} for doc in docs)
    gateway = next(doc for doc in docs if doc["kind"] == "Gateway")
    assert "infrastructure" not in gateway["spec"]


def test_render_inference_gateway_labels_the_provider():
    docs = _docs(
        render_inference_gateway(namespace="lens-gateway", gateway_name="lens-inference-gateway", provider="istio")
    )
    gateway = next(doc for doc in docs if doc["kind"] == "Gateway")
    gateway_class = next(doc for doc in docs if doc["kind"] == "GatewayClass")
    assert gateway["metadata"]["labels"]["lens.ai/gateway-provider"] == "istio"
    assert gateway_class["metadata"]["labels"]["lens.ai/gateway-provider"] == "istio"


def test_render_inference_gateway_envoy_includes_buffer_policy():
    text = render_inference_gateway(namespace="lens-gateway", gateway_name="lens-gateway", provider="envoy-ai-gateway")
    docs = _docs(text)
    kinds = {doc["kind"] for doc in docs}
    assert {"Namespace", "GatewayClass", "Gateway", "ClientTrafficPolicy"} <= kinds
    gateway_class = next(doc for doc in docs if doc["kind"] == "GatewayClass")
    assert gateway_class["spec"]["controllerName"] == "gateway.envoyproxy.io/gatewayclass-controller"
    gateway = next(doc for doc in docs if doc["kind"] == "Gateway")
    assert gateway["spec"]["gatewayClassName"] == "envoy-ai-gateway"
    policy = next(doc for doc in docs if doc["kind"] == "ClientTrafficPolicy")
    # targetRefs.group must be the bare API group, never version-qualified.
    assert policy["spec"]["targetRefs"][0]["group"] == "gateway.networking.k8s.io"


@pytest.mark.parametrize(
    "provider,expected_class",
    [
        ("istio", "istio"),
        ("agentgateway", "agentgateway"),
        ("gke", "gke-l7-regional-external-managed"),
    ],
)
def test_render_inference_gateway_non_envoy_has_no_client_traffic_policy(provider, expected_class):
    docs = _docs(render_inference_gateway(namespace="ns", gateway_name="gw", provider=provider))
    kinds = {doc["kind"] for doc in docs}
    assert "ClientTrafficPolicy" not in kinds
    gateway = next(doc for doc in docs if doc["kind"] == "Gateway")
    assert gateway["spec"]["gatewayClassName"] == expected_class


def test_render_model_route_references_single_pool():
    docs = _docs(
        render_model_route(
            namespace="inference",
            model_name="qwen",
            base_model="Qwen/Qwen3-32B",
            pool=PoolBinding("qwen", 8000),
            gateway_name="lens-gateway",
            gateway_namespace="lens-gateway",
            adapters=["food-review-1"],
        )
    )
    route = next(doc for doc in docs if doc["kind"] == "HTTPRoute")
    rule = route["spec"]["rules"][0]
    assert rule["matches"][0]["headers"][0] == {
        "type": "Exact",
        "name": BASE_MODEL_HEADER,
        "value": "Qwen/Qwen3-32B",
    }
    assert rule["backendRefs"] == [{"group": "inference.networking.k8s.io", "kind": "InferencePool", "name": "qwen"}]
    assert route["spec"]["parentRefs"][0]["group"] == "gateway.networking.k8s.io"
    assert route["spec"]["parentRefs"][0]["namespace"] == "lens-gateway"

    config_map = next(doc for doc in docs if doc["kind"] == "ConfigMap")
    assert config_map["data"]["baseModel"] == "Qwen/Qwen3-32B"
    assert config_map["data"]["adapters"] == "- food-review-1\n"


def test_render_model_route_grants_cross_namespace_pool():
    docs = _docs(
        render_model_route(
            namespace="lens-gateway",
            model_name="qwen",
            base_model="Qwen/Qwen3-32B",
            pool=PoolBinding("qwen", 8000, "ns-a"),
            gateway_name="lens-inference-gateway",
        )
    )
    route = next(doc for doc in docs if doc["kind"] == "HTTPRoute")
    refs = route["spec"]["rules"][0]["backendRefs"]
    assert refs == [
        {
            "group": "inference.networking.k8s.io",
            "kind": "InferencePool",
            "name": "qwen",
            "namespace": "ns-a",
        }
    ]
    grants = [doc for doc in docs if doc["kind"] == "ReferenceGrant"]
    assert [grant["metadata"]["namespace"] for grant in grants] == ["ns-a"]
    assert grants[0]["spec"]["to"][0]["group"] == "inference.networking.k8s.io"


def _ext_auth_docs(provider: str) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(
            render_inference_gateway(
                namespace="lens-gateway",
                gateway_name="lens-inference-gateway",
                provider=provider,
                authz_host="10.0.0.9",
            )
        )
        if doc
    ]


def test_ext_auth_renders_shared_backend_for_each_http_provider():
    for provider in ("envoy-ai-gateway", "istio", "agentgateway"):
        docs = _ext_auth_docs(provider)
        assert any(doc["kind"] == "Service" and doc["metadata"]["name"] == "lens-authz" for doc in docs), provider
        endpoints = next(doc for doc in docs if doc["kind"] == "Endpoints" and doc["metadata"]["name"] == "lens-authz")
        assert endpoints["subsets"][0]["addresses"] == [{"ip": "10.0.0.9"}]


def test_ext_auth_service_port_is_stable_while_endpoints_track_lens_port():
    docs = [
        doc
        for doc in yaml.safe_load_all(
            render_inference_gateway(
                namespace="lens-gateway",
                gateway_name="lens-inference-gateway",
                provider="istio",
                authz_host="10.0.0.9",
                authz_port=8084,
            )
        )
        if doc
    ]
    service = next(doc for doc in docs if doc["kind"] == "Service")
    assert service["spec"]["ports"][0]["port"] == 8081
    endpoints = next(doc for doc in docs if doc["kind"] == "Endpoints")
    assert endpoints["subsets"][0]["ports"][0]["port"] == 8084


def test_ext_auth_uses_each_provider_policy_kind():
    envoy = _ext_auth_docs("envoy-ai-gateway")
    policy = next(doc for doc in envoy if doc["kind"] == "SecurityPolicy")
    http = policy["spec"]["extAuth"]["http"]
    assert http["path"] == "/api/v1/internal/model-gateway/authorize"
    assert "x-llm-d-inference-fairness-id" in http["headersToBackend"]

    istio = _ext_auth_docs("istio")
    policy = next(doc for doc in istio if doc["kind"] == "AuthorizationPolicy")
    assert policy["spec"]["action"] == "CUSTOM"
    assert policy["spec"]["provider"]["name"] == "lens-authz"

    agent = _ext_auth_docs("agentgateway")
    policy = next(doc for doc in agent if doc["kind"] == "AgentgatewayPolicy")
    ext = policy["spec"]["traffic"]["extAuth"]
    assert ext["backendRef"]["name"] == "lens-authz"
    assert "x-llm-d-inference-fairness-id" in ext["http"]["allowedResponseHeaders"]


def test_gke_has_no_gateway_api_ext_auth():
    docs = _ext_auth_docs("gke")
    assert not any(doc["kind"] in {"SecurityPolicy", "AuthorizationPolicy", "AgentgatewayPolicy"} for doc in docs)
    assert not any(doc.get("metadata", {}).get("name") == "lens-authz" for doc in docs)


def test_ext_auth_absent_without_authz_host():
    docs = _docs(
        render_inference_gateway(
            namespace="lens-gateway", gateway_name="lens-inference-gateway", provider="envoy-ai-gateway"
        )
    )
    assert not any(doc["kind"] in {"SecurityPolicy", "AuthorizationPolicy", "AgentgatewayPolicy"} for doc in docs)


def test_qualified_pool_name_is_namespace_scoped():
    from llm_d_bench.model_service.gateway_providers import qualified_pool_name, split_pool_name

    assert qualified_pool_name("optimized-baseline", "llmd-a") == "llmd-a/optimized-baseline"
    assert qualified_pool_name("ns/pool", "other") == "ns/pool"
    assert qualified_pool_name(None, "ns") is None
    assert split_pool_name("llmd-a/optimized-baseline") == ("llmd-a", "optimized-baseline")
    assert split_pool_name("bare-pool") == (None, "bare-pool")
    assert split_pool_name(None) == (None, None)


def test_istio_authz_overlay_registers_envoy_ext_authz_http():
    """Regression: the Istio provider field is envoyExtAuthzHttp, not envoyHttpService."""
    from llm_d_bench.model_service.gateway_providers import istio_authz_overlay

    spec = yaml.safe_load(istio_authz_overlay("lens-gateway"))
    provider = spec["spec"]["meshConfig"]["extensionProviders"][0]
    assert provider["name"] == "lens-authz"
    assert "envoyHttpService" not in provider
    http = provider["envoyExtAuthzHttp"]
    assert http["service"] == "lens-authz.lens-gateway.svc.cluster.local"
    assert http["port"] == 8081
    assert "authorization" in http["includeRequestHeadersInCheck"]
    assert "x-llm-d-inference-fairness-id" in http["headersToUpstreamOnAllow"]


def test_istio_authz_overlay_forwards_deny_reason_header():
    """ext_authz drops the authz body; the reason is exposed as a downstream header."""
    from llm_d_bench.model_service.gateway_providers import istio_authz_overlay

    spec = yaml.safe_load(istio_authz_overlay("lens-gateway"))
    http = spec["spec"]["meshConfig"]["extensionProviders"][0]["envoyExtAuthzHttp"]
    assert "x-lens-error" in http["headersToDownstreamOnDeny"]


def test_ext_auth_path_carries_cluster_id():
    """A shared public name must resolve per cluster, so the authz path encodes it."""
    from llm_d_bench.model_service.gateway_providers import authz_path, render_gateway_ext_auth

    assert authz_path("cluster-a").endswith("/authorize/cluster/cluster-a")
    docs = render_gateway_ext_auth(
        provider="envoy-ai-gateway",
        namespace="lens-gateway",
        gateway_name="gw",
        authz_host="10.0.0.9",
        authz_cluster_id="cluster-a",
    )
    policy = next(doc for doc in docs if doc["kind"] == "SecurityPolicy")
    assert policy["spec"]["extAuth"]["http"]["path"].endswith("/cluster/cluster-a")


def test_render_node_inotify_daemonset_sets_limit():
    from llm_d_bench.model_service.gateway_providers import render_node_inotify_daemonset

    doc = yaml.safe_load(render_node_inotify_daemonset(4096, "lens-gateway"))
    assert doc["kind"] == "DaemonSet"
    assert doc["metadata"]["namespace"] == "lens-gateway"
    spec = doc["spec"]["template"]["spec"]
    assert spec["hostPID"] is True
    container = spec["containers"][0]
    assert container["securityContext"]["privileged"] is True
    assert "fs.inotify.max_user_instances=4096" in container["command"][-1]
