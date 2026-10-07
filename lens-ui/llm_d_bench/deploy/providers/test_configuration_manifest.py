"""Tests for deploying published configuration manifests."""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import llm_d_bench.deploy.providers.configuration_manifest as configuration_manifest
from llm_d_bench.deploy.providers.configuration_manifest import ConfigurationManifestAdapter
from llm_d_bench.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact


class _Runner:
    def __init__(self) -> None:
        self.copy_model_secret = AsyncMock()
        self.commands: list[list[str]] = []

    async def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.commands.append(command)
        return 0, "ok", ""


class _RoutedDelegate:
    published_manifest_lifecycle = True

    def __init__(self) -> None:
        self.router_installs: list[str] = []

    async def install_published_router(self, artifact, context) -> None:
        self.router_installs.append(context["namespace"])
        context.update(
            {
                "router_effective_values": "router: {}\n",
                "router_effective_values_checksum": "sha256:values",
                "router_rendered_manifest": "kind: Deployment\n",
                "router_rendered_manifest_checksum": "sha256:manifest",
            }
        )


def _asset(name: str, content: str) -> dict[str, str]:
    return {
        "name": name,
        "content": content,
        "checksum": f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}",
    }


@pytest.mark.asyncio
async def test_render_preserves_validated_deployment_bundle_and_router_profile(monkeypatch, tmp_path: Path):
    manifest = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: model\n"
    bundle = {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "optimized-baseline",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "oci://router",
            "version": "v1",
            "releaseName": "optimized-baseline",
            "values": [_asset("router-effective.yaml", "router: {}\n")],
        },
        "resources": [],
    }
    deployment = {
        "readinessDeployments": ["model"],
        "endpoint": {"protocol": "http", "serviceName": "model", "port": 8000},
    }
    monkeypatch.setattr(
        configuration_manifest,
        "validate_configuration_manifest_content",
        lambda *_: (
            manifest,
            "sha256:source",
            deployment,
            {
                "guide": "optimized-baseline",
                "commit": "a" * 40,
            },
        ),
    )
    adapter = ConfigurationManifestAdapter(AsyncMock(), _Runner(), "llm-d-bench-", 30, tmp_path)
    definition = GuideDefinition("optimized-baseline", "source", "hash", "supported")

    artifact = await adapter.render(
        definition,
        {
            "_deployment_namespace": "llm-d-bench-run",
            "routerProfile": "load-only",
            "officialGuide": {"deploymentBundle": bundle},
        },
    )

    assert artifact.deployment_contract["deploymentBundle"] == bundle
    assert artifact.deployment_contract["routerProfile"] == "load-only"


@pytest.mark.asyncio
async def test_target_node_does_not_read_kustomize_directory_as_manifest(monkeypatch, tmp_path: Path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    delegate = AsyncMock()
    delegate.render.return_value = GuideDeploymentArtifact(
        guide_id="optimized-baseline", artifact_hash="hash", manifest_ref=str(overlay)
    )
    adapter = ConfigurationManifestAdapter(delegate, _Runner(), "llm-d-bench-", 30, tmp_path)
    definition = GuideDefinition("optimized-baseline", "source", "hash", "supported")
    monkeypatch.setattr(configuration_manifest, "configured_target_node", lambda: "smc-19")

    artifact = await adapter.render(definition, {})

    assert artifact.manifest_ref == str(overlay)


@pytest.mark.asyncio
async def test_deploy_delegates_kustomize_directory_artifact(tmp_path: Path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(overlay),
        deployment_contract={"routerProfile": "optimized-baseline"},
    )
    runner = _Runner()
    delegate = AsyncMock()
    delegate.deploy.return_value = {"namespace": "llm-d-bench-run"}
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)
    context = {"namespace": "llm-d-bench-run"}

    assert await adapter.deploy(artifact, context) == {"namespace": "llm-d-bench-run"}
    delegate.deploy.assert_awaited_once_with(artifact, context)
    assert runner.commands == []


@pytest.mark.asyncio
async def test_deploy_copies_frontend_selected_existing_secret(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("env:\n  - secretKeyRef:\n      name: llm-d-hf-token\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["decode"],
            "endpoint": {"protocol": "http", "serviceName": "model", "port": 8000},
        },
    )
    runner = _Runner()
    adapter = ConfigurationManifestAdapter(AsyncMock(), runner, "llm-d-bench-", 30, tmp_path)

    await adapter.deploy(
        artifact,
        {
            "namespace": "llm-d-bench-run",
            "deployment_policy": {
                "model_secret": {
                    "mode": "existing-secret",
                    "sourceNamespace": "model-secrets",
                    "sourceName": "huggingface",
                }
            },
        },
    )

    runner.copy_model_secret.assert_awaited_once_with("llm-d-bench-run", "model-secrets", "huggingface")
    assert runner.commands[-1][:3] == ["kubectl", "apply", "--namespace"]


@pytest.mark.asyncio
async def test_pd_manifest_uses_delegate_for_router_lifecycle(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="pd-disaggregation",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["decode", "prefill"],
            "endpoint": {"protocol": "http", "serviceName": "model", "port": 8200},
        },
    )
    runner = _Runner()
    delegate = AsyncMock()
    delegate.deploy.return_value = {"namespace": "llm-d-bench-run"}
    delegate.readiness.return_value = object()
    delegate.diagnostics.return_value = {"router": "ready"}
    delegate.stop.return_value = {"stopped": True}
    delegate.cleanup.return_value = {"cleaned_up": True}
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)
    context = {"namespace": "llm-d-bench-run", "_artifact": artifact}

    assert await adapter.deploy(artifact, context) == {"namespace": "llm-d-bench-run"}
    await adapter.readiness(context)
    await adapter.diagnostics(context)
    await adapter.stop(context, artifact)
    await adapter.cleanup(context, artifact, force=True)

    delegate.deploy.assert_awaited_once_with(artifact, context)
    delegate.readiness.assert_awaited_once_with(context)
    delegate.diagnostics.assert_awaited_once_with(context)
    delegate.stop.assert_awaited_once_with(context, artifact)
    delegate.cleanup.assert_awaited_once_with(context, artifact, force=True)
    assert all(command[:2] != ["kubectl", "apply"] for command in runner.commands)


@pytest.mark.asyncio
async def test_optimized_manifest_installs_router_then_applies_exact_model_manifest(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["optimized-baseline-xpu-vllm-decode"],
            "endpoint": {"protocol": "http", "serviceName": "optimized-baseline-modelserver", "port": 8000},
        },
    )
    runner = _Runner()
    delegate = _RoutedDelegate()
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)

    result = await adapter.deploy(artifact, {"namespace": "llm-d-bench-run"})

    assert delegate.router_installs == ["llm-d-bench-run"]
    assert runner.commands[-1] == ["kubectl", "apply", "--namespace", "llm-d-bench-run", "--filename", str(manifest)]
    assert result["namespace"] == "llm-d-bench-run"
    assert result["router_effective_values"] == "router: {}\n"
    assert result["router_rendered_manifest_checksum"] == "sha256:manifest"


@pytest.mark.asyncio
async def test_readiness_retries_until_newly_applied_deployment_is_visible(monkeypatch, tmp_path: Path):
    """An apply/read visibility gap must not become a terminal rollout failure."""
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(tmp_path / "manifest.yaml"),
        deployment_contract={
            "readinessDeployments": ["optimized-baseline-xpu-vllm-decode"],
            "endpoint": {"protocol": "http", "serviceName": "model", "port": 8000},
        },
    )
    responses = iter(
        [
            (1, "", 'Error from server (NotFound): deployments.apps "optimized-baseline-xpu-vllm-decode" not found'),
            (1, "", 'Error from server (NotFound): deployments.apps "optimized-baseline-xpu-vllm-decode" not found'),
            (0, "deployment successfully rolled out", ""),
        ]
    )

    async def runner(_command):
        return next(responses)

    sleep = AsyncMock()
    monkeypatch.setattr(configuration_manifest.asyncio, "sleep", sleep)
    adapter = ConfigurationManifestAdapter(AsyncMock(), runner, "llm-d-bench-", 30, tmp_path)

    result = await adapter.readiness({"namespace": "llm-d-bench-run", "_artifact": artifact})

    assert result.accepted
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_exact_manifest_ensures_storage_volume_pvc_in_deployment_namespace(monkeypatch, tmp_path: Path):
    """Exact manifests come from Configuration with a baked PVC claimName, so
    the manifest adapter must prepare that claim inside the per-run deployment
    namespace before applying the manifest.
    """
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["optimized-baseline-xpu-vllm-decode"],
            "endpoint": {"protocol": "http", "serviceName": "optimized-baseline-modelserver", "port": 8000},
        },
    )
    runner = _Runner()
    delegate = _RoutedDelegate()
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)
    volume = object()

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        assert volume_id == "storage-6bb85a3e"
        assert cluster_id == "d459a19d"
        return volume

    ensure_calls = []

    async def fake_ensure_mount_in_namespace(resolved_volume, *, namespace, cluster_id):
        ensure_calls.append((resolved_volume, namespace, cluster_id))
        return "prism-storage-storage-6bb85a3e"

    monkeypatch.setattr(configuration_manifest, "get_ready_volume", fake_get_ready_volume)
    monkeypatch.setattr(configuration_manifest, "ensure_mount_in_namespace", fake_ensure_mount_in_namespace)

    await adapter.deploy(
        artifact,
        {
            "namespace": "llm-d-bench-run",
            "provenance": {"cluster_server_id": "d459a19d"},
            "runtime": {"storageVolumeId": "storage-6bb85a3e"},
        },
    )

    assert ensure_calls == [(volume, "llm-d-bench-run", "d459a19d")]
    assert runner.commands[-1] == ["kubectl", "apply", "--namespace", "llm-d-bench-run", "--filename", str(manifest)]


@pytest.mark.asyncio
async def test_pd_manifest_ensures_storage_volume_pvc_before_delegate_lifecycle(monkeypatch, tmp_path: Path):
    """Regression test: pd-disaggregation (and any other
    ``published_manifest_lifecycle`` guide) must still get its Configuration-
    baked PVC claim prepared in the deployment namespace even though its own
    ``deploy()`` is fully delegated for Router/EPP installation. Before this
    fix, the storage-mount step ran after the delegate-lifecycle early
    return, so Pods referencing the storage volume's PVC failed to schedule
    with "persistentvolumeclaim ... not found".
    """
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="pd-disaggregation",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["decode", "prefill"],
            "endpoint": {"protocol": "http", "serviceName": "model", "port": 8200},
        },
    )
    runner = _Runner()
    delegate = AsyncMock()
    delegate.deploy.return_value = {"namespace": "llm-d-bench-run"}
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)
    volume = object()

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        assert volume_id == "storage-fcb73102"
        return volume

    ensure_calls = []

    async def fake_ensure_mount_in_namespace(resolved_volume, *, namespace, cluster_id):
        ensure_calls.append((resolved_volume, namespace, cluster_id))
        return "prism-storage-storage-fcb73102"

    monkeypatch.setattr(configuration_manifest, "get_ready_volume", fake_get_ready_volume)
    monkeypatch.setattr(configuration_manifest, "ensure_mount_in_namespace", fake_ensure_mount_in_namespace)

    context = {
        "namespace": "llm-d-bench-run",
        "provenance": {"cluster_server_id": "d459a19d"},
        "runtime": {"storageVolumeId": "storage-fcb73102"},
    }
    result = await adapter.deploy(artifact, context)

    assert ensure_calls == [(volume, "llm-d-bench-run", "d459a19d")]
    delegate.deploy.assert_awaited_once_with(artifact, context)
    assert result == {"namespace": "llm-d-bench-run"}
