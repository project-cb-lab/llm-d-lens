"""Tests for the structured P/D disaggregation deployment adapter."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from llm_d_bench.deploy.providers.guide_adapter import GuideDeploymentArtifact
from llm_d_bench.deploy.providers.pd_disaggregation import PdDisaggregationAdapter


class _ReadinessRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    async def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.commands.append(command)
        if command[1:3] == ["get", "service"]:
            return (
                0,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "pd-disaggregation-epp"},
                                "spec": {
                                    "ports": [
                                        {"name": "grpc-ext-proc", "port": 9002},
                                        {"name": "http-metrics", "port": 9090},
                                        {"name": "http", "port": 80},
                                    ]
                                },
                            }
                        ],
                    }
                ),
                "",
            )
        if command[1:3] == ["get", "deployments"]:
            return (
                0,
                json.dumps(
                    {
                        "items": [
                            {"metadata": {"name": "pd-disaggregation-nvidia-gpu-vllm-prefill"}},
                            {"metadata": {"name": "pd-disaggregation-nvidia-gpu-vllm-decode"}},
                            {"metadata": {"name": "pd-disaggregation-epp"}},
                        ]
                    }
                ),
                "",
            )
        return 0, "deployment successfully rolled out", ""


def _asset(name: str, content: str) -> dict[str, str]:
    return {"name": name, "content": content, "checksum": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"}


@pytest.mark.asyncio
async def test_deploy_uses_saved_bundle_when_original_guide_tree_is_missing(tmp_path: Path):
    runner = _ReadinessRunner()
    adapter = PdDisaggregationAdapter(
        runner,
        tmp_path / "removed-guide",
        "llm-d-bench-",
        30,
        Path("helm"),
        None,
    )
    bundle_commands: list[list[str]] = []

    async def bundle_runner(command):
        bundle_commands.append(command)
        if command[1:3] == ["get", "manifest"]:
            return 0, "kind: Deployment\nmetadata:\n  name: saved-router\n", ""
        return 0, "kind: Deployment\n", ""

    adapter._bundle_command_runner = bundle_runner
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\nmetadata:\n  name: saved-model\n", encoding="utf-8")
    bundle = {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "pd-disaggregation",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "saved-chart",
            "version": "saved-version",
            "releaseName": "pd-disaggregation",
            "values": [_asset("router-effective.yaml", "router:\n  saved: true\n")],
        },
        "resources": [_asset("saved-resource.yaml", "kind: Service\nmetadata:\n  name: saved-service\n")],
    }
    artifact = GuideDeploymentArtifact(
        "pd-disaggregation",
        "hash",
        manifest_ref=str(manifest),
        source_ref="a" * 40,
        deployment_contract={"deploymentBundle": bundle},
    )

    execution = await adapter.deploy(artifact, {"namespace": "llm-d-bench-run"})

    assert any(command[:2] == ["kubectl", "apply"] for command in bundle_commands)
    assert not any("removed-guide" in " ".join(command) for command in bundle_commands + runner.commands)
    # Gateway Mode: the saved effective values are merged with the shared-Gateway
    # values so the deployment's own proxy is disabled.
    effective = yaml.safe_load(execution["router_effective_values"])
    assert effective["router"]["saved"] is True
    assert effective["router"]["proxy"]["enabled"] is False
    assert effective["router"]["epp"]["flags"]["secure-serving"] is False
    assert execution["router_rendered_manifest"].endswith("name: saved-router\n")


@pytest.mark.asyncio
async def test_readiness_discovers_epp_router_service():
    runner = _ReadinessRunner()
    adapter = PdDisaggregationAdapter.__new__(PdDisaggregationAdapter)
    adapter._runner = runner
    adapter._timeout = 30
    execution = {"namespace": "llm-d-bench-run"}

    result = await adapter.readiness(execution)

    assert result.accepted
    assert execution["endpoint_url"] == "http://pd-disaggregation-epp.llm-d-bench-run.svc:80"
    assert runner.commands[-1] == [
        "kubectl",
        "get",
        "service",
        "--namespace",
        "llm-d-bench-run",
        "--output",
        "json",
    ]


@pytest.mark.asyncio
async def test_readiness_uses_contract_deployment_names():
    runner = _ReadinessRunner()
    adapter = PdDisaggregationAdapter.__new__(PdDisaggregationAdapter)
    adapter._runner = runner
    adapter._timeout = 30
    artifact = GuideDeploymentArtifact(
        "pd-disaggregation",
        "hash",
        deployment_contract={
            "data_plane_kind": "llm-d-router",
            "readinessDeployments": [
                "pd-disaggregation-nvidia-gpu-vllm-prefill",
                "pd-disaggregation-nvidia-gpu-vllm-decode",
            ],
        },
    )
    execution = {"namespace": "llm-d-bench-run", "_artifact": artifact}

    result = await adapter.readiness(execution)

    assert result.accepted
    rolled = [command[3] for command in runner.commands if command[1:3] == ["rollout", "status"]]
    assert rolled == [
        "deployment/pd-disaggregation-nvidia-gpu-vllm-prefill",
        "deployment/pd-disaggregation-nvidia-gpu-vllm-decode",
    ]
    assert not any(command[1:3] == ["get", "deployments"] for command in runner.commands)


def test_guide_sources_follow_the_active_accelerator(monkeypatch, tmp_path):
    import llm_d_bench.deploy.providers.pd_disaggregation as pd

    monkeypatch.setattr(pd, "overlay_variant", lambda: "gpu")
    assert PdDisaggregationAdapter._guide_sources(tmp_path) == {
        "base": tmp_path / "guides/pd-disaggregation/modelserver/gpu/vllm/base"
    }

    monkeypatch.setattr(pd, "overlay_variant", lambda: "xpu")
    assert set(PdDisaggregationAdapter._guide_sources(tmp_path)) == {"vllm", "vllm-rdma"}
