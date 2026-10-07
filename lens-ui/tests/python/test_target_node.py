from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from llm_d_bench.deploy.providers.configuration_manifest import ConfigurationManifestAdapter
from llm_d_bench.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact
from llm_d_bench.deploy.providers.target_node import (
    configured_target_node,
    pin_manifest_to_node,
    write_pinned_manifest,
)


@pytest.mark.parametrize(
    ("kind", "manifest", "selector_path"),
    [
        ("Pod", {"kind": "Pod", "spec": {}}, ("spec",)),
        ("Deployment", {"kind": "Deployment", "spec": {"template": {"spec": {}}}}, ("spec", "template", "spec")),
        ("StatefulSet", {"kind": "StatefulSet", "spec": {"template": {"spec": {}}}}, ("spec", "template", "spec")),
        ("Job", {"kind": "Job", "spec": {"template": {"spec": {}}}}, ("spec", "template", "spec")),
        (
            "CronJob",
            {"kind": "CronJob", "spec": {"jobTemplate": {"spec": {"template": {"spec": {}}}}}},
            ("spec", "jobTemplate", "spec", "template", "spec"),
        ),
    ],
)
def test_pin_manifest_to_node_covers_workload_types(kind, manifest, selector_path):
    rendered, count = pin_manifest_to_node(yaml.safe_dump(manifest), "smc-18")
    document = yaml.safe_load(rendered)
    pod_spec = document
    for key in selector_path:
        pod_spec = pod_spec[key]

    assert kind == document["kind"]
    assert count == 1
    assert pod_spec["nodeSelector"]["kubernetes.io/hostname"] == "smc-18"


def test_pin_manifest_preserves_existing_selectors_and_handles_lists():
    manifest = yaml.safe_dump(
        {
            "kind": "List",
            "items": [
                {
                    "kind": "Deployment",
                    "spec": {"template": {"spec": {"nodeSelector": {"accelerator": "xpu"}}}},
                }
            ],
        }
    )

    rendered, count = pin_manifest_to_node(manifest, "smc-18")
    selector = yaml.safe_load(rendered)["items"][0]["spec"]["template"]["spec"]["nodeSelector"]

    assert count == 1
    assert selector == {"accelerator": "xpu", "kubernetes.io/hostname": "smc-18"}


def test_configured_target_node_rejects_invalid_names():
    with pytest.raises(ValueError, match="valid Kubernetes node name"):
        configured_target_node({"PRISM_K8S_TARGET_NODE": "SMC_18"})


def test_write_pinned_manifest_is_content_addressed(tmp_path: Path):
    source = tmp_path / "source.yaml"
    source.write_text("kind: Pod\nspec: {}\n", encoding="utf-8")

    manifest_ref, checksum, count = write_pinned_manifest(str(source), tmp_path / "output", "smc-18")

    assert count == 1
    assert checksum.removeprefix("sha256:") in manifest_ref
    assert yaml.safe_load(Path(manifest_ref).read_text())["spec"]["nodeSelector"] == {
        "kubernetes.io/hostname": "smc-18"
    }


@pytest.mark.asyncio
async def test_adapter_pins_delegate_provider_manifest(monkeypatch, tmp_path: Path):
    source = tmp_path / "delegate.yaml"
    source.write_text("kind: Deployment\nspec:\n  template:\n    spec: {}\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="test-guide",
        artifact_hash="provider-hash",
        manifest_ref=str(source),
        manifest_checksum="sha256:provider",
    )
    delegate = SimpleNamespace(render=AsyncMock(return_value=artifact))
    adapter = ConfigurationManifestAdapter(delegate, AsyncMock(), "llm-d-bench-", 30, tmp_path / "output")
    monkeypatch.setenv("PRISM_K8S_TARGET_NODE", "smc-18")

    rendered = await adapter.render(GuideDefinition("test-guide", "source", "content", "experimental"), {})

    assert rendered.manifest_ref != str(source)
    deployment = yaml.safe_load(Path(rendered.manifest_ref).read_text())
    assert deployment["spec"]["template"]["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "smc-18"}
