"""Behavioral tests for immutable Guide deployment bundles."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from llm_d_bench.deploy.providers.deployment_bundle import (
    install_deployment_bundle,
    validate_deployment_bundle,
)


def _asset(name: str, content: str) -> dict[str, str]:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {"name": name, "content": content, "checksum": f"sha256:{digest}"}


def _bundle(*, guide: str = "optimized-baseline", commit: str = "a" * 40) -> dict:
    return {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": guide,
        "sourceCommit": commit,
        "helm": {
            "chart": "oci://example.invalid/router",
            "version": "v1.2.3",
            "releaseName": guide,
            "values": [
                _asset("router-base.yaml", "base: true\n"),
                _asset("router-effective.yaml", "router:\n  setting: saved\n"),
            ],
        },
        "resources": [_asset("saved-service.yaml", "apiVersion: v1\nkind: Service\nmetadata:\n  name: saved\n")],
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(schemaVersion="v2"), "schemaVersion"),
        (lambda value: value.update(guide="pd-disaggregation"), "Guide"),
        (lambda value: value.update(sourceCommit="b" * 40), "source commit"),
        (lambda value: value["resources"].append(_asset("../escape.yaml", "kind: Service\n")), "safe file name"),
        (lambda value: value["resources"].append(_asset("saved-service.yaml", "kind: Service\n")), "unique"),
        (lambda value: value["helm"]["values"][-1].update(content="router: changed\n"), "checksum"),
        (
            lambda value: value["helm"]["values"][-1].update(
                content="- invalid\n", checksum=_asset("x", "- invalid\n")["checksum"]
            ),
            "mapping",
        ),
        (
            lambda value: value["resources"][0].update(
                content="kind: Namespace\n", checksum=_asset("x", "kind: Namespace\n")["checksum"]
            ),
            "namespace-neutral",
        ),
        (
            lambda value: value["resources"][0].update(
                content="kind: Service\nmetadata:\n  name: x\n  namespace: fixed\n",
                checksum=_asset("x", "kind: Service\nmetadata:\n  name: x\n  namespace: fixed\n")["checksum"],
            ),
            "namespace-neutral",
        ),
    ],
)
def test_validation_rejects_bundle_integrity_breaks(mutate, message):
    bundle = _bundle()
    mutate(bundle)

    with pytest.raises(ValueError, match=message):
        validate_deployment_bundle(bundle, "optimized-baseline", "a" * 40)


@pytest.mark.asyncio
async def test_install_uses_only_saved_effective_values_and_saved_resources(tmp_path: Path):
    commands: list[list[str]] = []

    async def runner(command: list[str]) -> tuple[int, str, str]:
        commands.append(command)
        if command[1] == "template":
            return 0, "kind: Deployment\nmetadata:\n  name: template-only\n", ""
        if command[1:3] == ["get", "manifest"]:
            return 0, "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: rendered-router\n", ""
        return 0, "ok", ""

    result = await install_deployment_bundle(
        _bundle(),
        guide="optimized-baseline",
        source_commit="a" * 40,
        namespace="llmd-run",
        command_runner=runner,
        output_root=tmp_path,
    )

    template, apply_resource, install, get_manifest = commands
    effective_path = Path(template[template.index("--values") + 1])
    assert effective_path.read_text(encoding="utf-8") == "router:\n  setting: saved\n"
    assert "router-base.yaml" not in " ".join(template + install)
    assert apply_resource[:5] == ["kubectl", "apply", "--namespace", "llmd-run", "--filename"]
    assert Path(apply_resource[-1]).read_text(encoding="utf-8").endswith("name: saved\n")
    assert install[:7] == [
        "helm",
        "upgrade",
        "--install",
        "optimized-baseline",
        "oci://example.invalid/router",
        "--namespace",
        "llmd-run",
    ]
    assert get_manifest == ["helm", "get", "manifest", "optimized-baseline", "--namespace", "llmd-run"]
    assert result["router_effective_values"] == "router:\n  setting: saved\n"
    assert result["router_effective_values_checksum"].startswith("sha256:")
    assert result["router_rendered_manifest"].endswith("name: rendered-router\n")
    assert result["router_rendered_manifest_checksum"].startswith("sha256:")


@pytest.mark.asyncio
async def test_install_fails_before_cluster_changes_when_helm_rendering_fails(tmp_path: Path):
    commands: list[list[str]] = []

    async def runner(command: list[str]) -> tuple[int, str, str]:
        commands.append(command)
        return 1, "", "chart unavailable"

    with pytest.raises(RuntimeError, match="chart unavailable"):
        await install_deployment_bundle(
            _bundle(),
            guide="optimized-baseline",
            source_commit="a" * 40,
            namespace="llmd-run",
            command_runner=runner,
            output_root=tmp_path,
        )

    assert len(commands) == 1
    assert commands[0][1] == "template"


@pytest.mark.asyncio
async def test_install_rejects_empty_installed_helm_manifest(tmp_path: Path):
    async def runner(command: list[str]) -> tuple[int, str, str]:
        if command[1:3] == ["get", "manifest"]:
            return 0, "", ""
        return 0, "kind: Deployment\n", ""

    with pytest.raises(RuntimeError, match="empty"):
        await install_deployment_bundle(
            _bundle(),
            guide="optimized-baseline",
            source_commit="a" * 40,
            namespace="llmd-run",
            command_runner=runner,
            output_root=tmp_path,
        )
