import hashlib
from pathlib import Path

import pytest

from llm_d_bench.deploy.providers.guide_adapter import GuideDeploymentArtifact
from llm_d_bench.deploy.providers.helm_kustomize import HelmKustomizeGuideAdapter


def _asset(name: str, content: str) -> dict[str, str]:
    return {"name": name, "content": content, "checksum": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"}


@pytest.mark.asyncio
async def test_deploy_uses_saved_bundle_without_descriptor_source_files(tmp_path: Path):
    commands = []

    async def runner(command):
        commands.append(command)
        return 0, "ok", ""

    bundle_commands = []

    async def bundle_runner(command):
        bundle_commands.append(command)
        if command[1:3] == ["get", "manifest"]:
            return 0, "kind: Deployment\nmetadata:\n  name: installed-router\n", ""
        return 0, "kind: Deployment\n", ""

    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._runner = runner
    adapter._bundle_command_runner = bundle_runner
    adapter._namespace_prefix = "llmd-"
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\nmetadata:\n  name: model\n", encoding="utf-8")
    bundle = {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "tiered-prefix-cache",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "saved-chart",
            "version": "saved-version",
            "releaseName": "tiered-prefix-cache",
            "values": [_asset("router-effective.yaml", "router:\n  saved: true\n")],
        },
        "resources": [],
    }
    artifact = GuideDeploymentArtifact(
        "tiered-prefix-cache",
        "hash",
        manifest_ref=str(manifest),
        source_ref="a" * 40,
        deployment_contract={"deploymentBundle": bundle},
    )

    execution = await adapter.deploy(artifact, {"namespace": "llmd-run"})

    assert bundle_commands[0][1:4] == ["template", "tiered-prefix-cache", "saved-chart"]
    assert commands[-1] == ["kubectl", "apply", "--namespace", "llmd-run", "--filename", str(manifest)]
    assert execution["router_effective_values"] == "router:\n  saved: true\n"


def test_patch_documents_sets_xpu_startup_budget_and_model_length():
    documents = [
        {
            "kind": "Deployment",
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "modelserver",
                                "image": "old:image",
                                "args": ["exec vllm serve Qwen/Old --tensor-parallel-size=1"],
                            }
                        ],
                    },
                },
            },
        },
        {
            "kind": "ResourceClaimTemplate",
            "spec": {"spec": {"devices": {"requests": [{"exactly": {"count": 1}}]}}},
        },
    ]
    parameters = {
        "replicas": 1,
        "image": "new:image",
        "mount_path": "",
        "model": "Qwen/Qwen3-8B",
        "tensor_parallel_size": 1,
        "max_model_len": 16384,
        "custom_parameters": [],
        "environment": {},
    }

    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._accelerator = None
    adapter._patch_documents(documents, parameters)

    deployment = documents[0]
    command = deployment["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert deployment["spec"]["progressDeadlineSeconds"] == 1800
    assert "--max-model-len=16384" in command


def test_patch_documents_runs_huggingface_offline_for_a_mounted_cache():
    documents = [
        {
            "kind": "Deployment",
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "modelserver", "image": "old:image", "args": ["exec vllm serve Qwen/Old"]}
                        ],
                    },
                },
            },
        },
        {
            "kind": "ResourceClaimTemplate",
            "spec": {"spec": {"devices": {"requests": [{"exactly": {"count": 1}}]}}},
        },
    ]
    parameters = {
        "replicas": 1,
        "image": "new:image",
        "mount_path": "/mnt/models",
        "model": "Qwen/Qwen3-8B",
        "tensor_parallel_size": 1,
        "max_model_len": None,
        "custom_parameters": [],
        "environment": {},
    }

    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._accelerator = None
    adapter._patch_documents(documents, parameters)

    container = documents[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["args"][0].startswith("exec vllm serve /model-cache")
    environment = {item["name"]: item.get("value") for item in container["env"]}
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert "HF_HOME" not in environment
