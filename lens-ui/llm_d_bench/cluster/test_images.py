"""Tests for the cluster image pre-pull helper."""

from llm_d_bench import versions
from llm_d_bench.cluster.images import prepull_images, render_image_puller_daemonset
from llm_d_bench.hardware.resolver import resolve_by_accelerator_key


def test_prepull_images_covers_router_and_selected_model_servers():
    images = prepull_images(["nvidia", "intel"])

    assert versions.router_epp_image() in images
    assert versions.router_pd_sidecar_image() in images
    assert versions.inference_payload_processor_image() in images
    assert resolve_by_accelerator_key("nvidia").deployment.runtime_image in images
    assert resolve_by_accelerator_key("xpu").deployment.runtime_image in images


def test_prepull_images_includes_the_selected_gateway_provider():
    version = versions.gateway_provider_version("istio")
    images = prepull_images([], provider="istio")

    assert f"docker.io/istio/pilot:{version}" in images
    assert f"docker.io/istio/proxyv2:{version}" in images


def test_prepull_images_uses_the_pinned_versions_and_deduplicates():
    images = prepull_images(["nvidia", "nvidia", "cuda"])

    assert images.count(resolve_by_accelerator_key("nvidia").deployment.runtime_image) == 1


def test_render_puller_daemonset_pulls_every_image_privileged():
    manifest = render_image_puller_daemonset(
        ["ghcr.io/llm-d/llm-d-cuda:v0.9.0", "ghcr.io/llm-d/llm-d-xpu:v0.9.0"],
        namespace="lens-images",
        name="lens-image-puller",
        image="busybox:1.36",
    )

    # One container per image, each with its own readiness probe.
    assert manifest.count("crictl pull") == 2
    assert "name: pull-0" in manifest
    assert "name: pull-1" in manifest
    assert "lens-image-pull-done-0" in manifest
    assert "lens-image-pull-done-1" in manifest
    assert "privileged: true" in manifest
    assert "hostPID: true" in manifest
