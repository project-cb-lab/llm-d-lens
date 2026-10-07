"""Pre-pull llm-d container images onto every cluster node.

A heterogeneous cluster may need the model-server image for each accelerator it
hosts plus the shared llm-d-router images. Pulling them at cluster creation (a
one-shot privileged DaemonSet that runs the host's ``crictl`` through
``nsenter``) avoids a slow lazy pull at first deployment. The DaemonSet stays
running so its per-node readiness can be polled; uninstall removes it.

Router/gateway versions come from the pinned stack profile
(``llm_d_bench.versions``); each model-server image comes from the hardware
profile the accelerator resolves to, never from the caller.
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any

from llm_d_bench.hardware.resolver import resolve_by_accelerator_key
from llm_d_bench.utils.kubernetes import scoped_runner
from llm_d_bench.versions import (
    inference_payload_processor_image,
    router_epp_image,
    router_pd_sidecar_image,
)

IMAGES_NAMESPACE = "lens-images"
IMAGES_NAME = "lens-image-puller"
#: BusyBox ships ``nsenter``; the host already has ``crictl`` and the containerd
#: config (including any proxy), so pulls run in the host namespaces.
PULLER_IMAGE = os.environ.get("LENS_IMAGE_PULLER_IMAGE", "busybox:1.36")
DONE_FILE = "/tmp/lens-image-pull-done"  # noqa: S108 - marker inside the puller container
_APPLY_TIMEOUT = 120.0
_QUERY_TIMEOUT = 60.0

#: Canonical accelerator name -> hardware profile accelerator key. The profile
#: (loaded from the registry, not a hardcoded file) owns the model-server image.
_ACCELERATOR_PROFILE_KEYS = {
    "nvidia": "nvidia",
    "intel": "xpu",
}

#: The image set last applied per cluster, so a status poll can report which
#: images are being pulled (the DaemonSet itself only reports per-node readiness).
_LAST_IMAGES: dict[str, list[str]] = {}


def _canonical_accelerator(accelerator: str) -> str | None:
    value = str(accelerator or "").lower()
    if "nvidia" in value or "cuda" in value:
        return "nvidia"
    if "intel" in value or "xpu" in value:
        return "intel"
    return None


def gateway_provider_images(provider: str | None) -> list[str]:
    """Controller/data-plane images for a supported Gateway provider.

    Only Istio is selectable today; its version comes from the stack profile.
    """
    from llm_d_bench.versions import gateway_provider_version  # noqa: PLC0415

    if provider == "istio":
        version = gateway_provider_version("istio")
        return [f"docker.io/istio/pilot:{version}", f"docker.io/istio/proxyv2:{version}"]
    return []


def prepull_images(accelerators: list[str], provider: str | None = None) -> list[str]:
    """The images to pre-pull: router + IPP + gateway provider + model server(s)."""
    images = [
        router_epp_image(),
        router_pd_sidecar_image(),
        inference_payload_processor_image(),
        *gateway_provider_images(provider),
    ]
    for accelerator in accelerators:
        canonical = _canonical_accelerator(accelerator)
        key = _ACCELERATOR_PROFILE_KEYS.get(canonical) if canonical else None
        if not key:
            continue
        profile = resolve_by_accelerator_key(key)
        image = profile.deployment.runtime_image if profile else None
        if image:
            images.append(image)
    return list(dict.fromkeys(images))  # de-duplicate, keep order


def _done_file(index: int) -> str:
    return f"{DONE_FILE}-{index}"


def render_image_puller_daemonset(images: list[str], *, namespace: str, name: str, image: str) -> str:
    """Render the privileged DaemonSet that pulls ``images`` per node.

    One container per image, each with its own readiness probe, so the pod's
    container statuses report per-image completion (the pod is Ready only once
    every image is pulled on that node).
    """
    containers = []
    for index, item in enumerate(images):
        done = _done_file(index)
        command = (
            f"nsenter -t 1 -m -u -i -n -p -- crictl pull {shlex.quote(item)} "
            f"&& touch {done} && sleep infinity"
        )
        containers.append(
            {
                "name": f"pull-{index}",
                "image": image,
                "securityContext": {"privileged": True},
                "command": ["sh", "-c", command],
                "readinessProbe": {
                    "exec": {"command": ["sh", "-c", f"test -f {done}"]},
                    "initialDelaySeconds": 5,
                    "periodSeconds": 5,
                },
            }
        )
    manifest = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}},
        "spec": {
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "hostPID": True,
                    "tolerations": [{"operator": "Exists"}],
                    "containers": containers,
                },
            },
        },
    }
    import yaml

    return yaml.safe_dump(manifest, sort_keys=False)


async def start_image_prepull(
    cluster_id: str, accelerators: list[str], provider: str | None = None
) -> dict[str, Any]:
    """Apply the puller DaemonSet for the selected accelerators/provider."""
    images = prepull_images(accelerators, provider)
    if not images:
        return {"state": "idle", "desired": 0, "ready": 0, "nodes": [], "images": []}
    _LAST_IMAGES[cluster_id] = images
    runner = scoped_runner(cluster_id)
    namespace_manifest = json.dumps(
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": IMAGES_NAMESPACE}}
    )
    await runner.run(
        ["kubectl", "apply", "-f", "-"], input=namespace_manifest, timeout=_APPLY_TIMEOUT
    )
    manifest = render_image_puller_daemonset(
        images, namespace=IMAGES_NAMESPACE, name=IMAGES_NAME, image=PULLER_IMAGE
    )
    await runner.run(["kubectl", "apply", "-f", "-"], input=manifest, timeout=_APPLY_TIMEOUT)
    return await image_prepull_status(cluster_id, images=images)


async def image_prepull_status(cluster_id: str, *, images: list[str] | None = None) -> dict[str, Any]:
    """Read the puller DaemonSet's per-node readiness."""
    resolved_images = images if images is not None else _LAST_IMAGES.get(cluster_id, [])
    runner = scoped_runner(cluster_id)
    result = await runner.run(
        ["kubectl", "get", "daemonset", IMAGES_NAME, "-n", IMAGES_NAMESPACE, "-o", "json"],
        timeout=_QUERY_TIMEOUT,
    )
    if result.returncode != 0:
        return {"state": "idle", "desired": 0, "ready": 0, "nodes": [], "images": []}
    desired = int(json.loads(result.stdout).get("status", {}).get("desiredNumberScheduled") or 0)

    pods = await runner.run(
        ["kubectl", "get", "pods", "-n", IMAGES_NAMESPACE, "-l", f"app={IMAGES_NAME}", "-o", "json"],
        timeout=_QUERY_TIMEOUT,
    )
    # Per-image readiness, keyed by the container name `pull-<index>`.
    per_image = [
        {"name": name, "ready": 0, "desired": desired, "state": "pulling"} for name in resolved_images
    ]
    nodes: list[dict[str, Any]] = []
    if pods.returncode == 0:
        for item in json.loads(pods.stdout).get("items", []):
            statuses = {c.get("name"): c for c in item.get("status", {}).get("containerStatuses", [])}
            node_ready = bool(statuses) and all(c.get("ready") for c in statuses.values())
            nodes.append(
                {
                    "node": item.get("spec", {}).get("nodeName"),
                    "phase": item.get("status", {}).get("phase"),
                    "ready": node_ready,
                    "restarts": sum(int(c.get("restartCount") or 0) for c in statuses.values()),
                }
            )
            for index, entry in enumerate(per_image):
                container = statuses.get(f"pull-{index}")
                if container and container.get("ready"):
                    entry["ready"] += 1
                    continue
                if not container:
                    continue
                state = container.get("state") or {}
                waiting = state.get("waiting") or {}
                terminated = state.get("terminated") or {}
                message = waiting.get("message") or waiting.get("reason") or terminated.get("message")
                if message:
                    entry["message"] = str(message)
                # A transient registry/network failure retries; only a repeated
                # failure (CrashLoop) is surfaced.
                if int(container.get("restartCount") or 0) >= 3 and not container.get("ready"):
                    entry["state"] = "failed"
    for entry in per_image:
        if entry["state"] != "failed":
            entry["state"] = "ready" if desired and entry["ready"] >= desired else "pulling"

    if per_image and all(entry["state"] == "ready" for entry in per_image):
        state = "ready"
    elif any(entry["state"] == "failed" for entry in per_image):
        state = "failed"
    else:
        state = "pulling"
    ready = sum(1 for entry in per_image if entry["state"] == "ready")
    return {"state": state, "desired": desired, "ready": ready, "nodes": nodes, "images": per_image}


async def stop_image_prepull(cluster_id: str) -> None:
    """Remove the puller DaemonSet (best effort)."""
    runner = scoped_runner(cluster_id)
    await runner.run(
        ["kubectl", "delete", "daemonset", IMAGES_NAME, "-n", IMAGES_NAMESPACE, "--ignore-not-found=true"],
        timeout=_QUERY_TIMEOUT,
    )
