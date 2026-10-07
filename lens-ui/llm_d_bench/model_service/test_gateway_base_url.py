"""Unit coverage for the externally reachable Gateway base-URL resolution."""

from types import SimpleNamespace

from llm_d_bench.model_service.gateway_ops import gateway_base_url as _gateway_base_url


def _cluster(**kwargs):
    return SimpleNamespace(
        **{
            "gateway_public_url": None,
            "gateway_port": None,
            "gateway_node_address": None,
            "gateway_address": None,
            **kwargs,
        }
    )


def test_explicit_host_wins_over_derivation():
    cluster = _cluster(
        gateway_public_url="gw.example.com",
        gateway_port=30080,
        gateway_node_address="10.1.2.3",
    )
    assert _gateway_base_url(cluster, "10.0.0.9", None) == "http://gw.example.com:30080/v1"


def test_explicit_host_without_port_builds_a_url():
    cluster = _cluster(gateway_public_url="10.1.2.3")
    assert _gateway_base_url(cluster, "10.0.0.9", None) == "http://10.1.2.3/v1"


def test_global_override_wins_over_cluster_host():
    cluster = _cluster(gateway_public_url="gw.example.com", gateway_port=30080)
    assert _gateway_base_url(cluster, "10.0.0.9", "https://global.example.com/v1") == "https://global.example.com/v1"


def test_loadbalancer_address_used_when_not_a_nodeport():
    cluster = _cluster(gateway_address="203.0.113.10")
    assert _gateway_base_url(cluster, "10.0.0.9", None) == "http://203.0.113.10/v1"


def test_nodeport_uses_the_cluster_node_address():
    cluster = _cluster(gateway_port=30080, gateway_node_address="10.112.229.74")
    assert _gateway_base_url(cluster, "10.112.228.229", None) == "http://10.112.229.74:30080/v1"


def test_nodeport_on_a_container_only_node_uses_the_lens_tunnel():
    cluster = _cluster(gateway_port=30998, gateway_node_address="172.19.0.2")
    assert _gateway_base_url(cluster, "10.112.228.229", None) == "http://10.112.228.229:30998/v1"


def test_no_reachable_address_yields_empty_url():
    assert _gateway_base_url(_cluster(), "10.0.0.9", None) == ""
