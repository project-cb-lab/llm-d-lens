"""Tests for the pinned Gateway API / GIE CRD installer."""

from llm_d_bench import versions
from llm_d_bench.cluster.gateway_crds import _REQUIRED_CRDS, manifest_urls


def test_manifest_urls_follow_the_pinned_versions():
    current = versions.stack()
    urls = manifest_urls()

    assert current.k8s_gateway_api in urls["gateway_api"]
    assert current.k8s_gateway_api_inference_extension in urls["gateway_api_inference_extension"]
    assert urls["gateway_api"].endswith("standard-install.yaml")
    assert urls["gateway_api_inference_extension"].endswith("v1-manifests.yaml")


def test_required_crds_cover_both_bundles():
    assert "gateways.gateway.networking.k8s.io" in _REQUIRED_CRDS["gateway_api"]
    assert "inferencepools.inference.networking.k8s.io" in _REQUIRED_CRDS["gateway_api_inference_extension"]
