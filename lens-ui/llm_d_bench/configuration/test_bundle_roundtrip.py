import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from llm_d_bench.configuration import service
from llm_d_bench.configuration.models import RenderRequest, SaveRequest
from llm_d_bench.utils.artifacts import configuration_checksum


def asset(name, content):
    return {"name": name, "content": content, "checksum": "sha256:" + hashlib.sha256(content.encode()).hexdigest()}


def request_payload():
    source = {
        "repository": "llm-d/llm-d",
        "requestedRef": "main",
        "commit": "a" * 40,
        "guide": "optimized-baseline",
        "accelerator": "xpu",
        "modelServer": "vllm",
        "variant": ".",
        "files": ["guides/optimized-baseline/modelserver/xpu/vllm/kustomization.yaml"],
    }
    manifest = yaml.safe_dump_all(
        [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "decode"},
                "spec": {
                    "replicas": 2,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "modelserver",
                                    "image": "model:v1",
                                    "command": ["vllm", "serve"],
                                    "args": ["New/Model", "--tensor-parallel-size=1"],
                                }
                            ]
                        }
                    },
                },
            },
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "model"}, "spec": {"ports": [{"port": 8000}]}},
        ]
    )
    bundle = {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "optimized-baseline",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "oci://ghcr.io/llm-d/charts/llm-d-router-standalone",
            "version": "v0.9.0",
            "releaseName": "optimized-baseline",
            "values": [
                asset("router-base.yaml", "router: {}"),
                asset("router-guide.yaml", "router:\n  epp:\n    replicas: 1"),
                asset("router-effective.yaml", "router:\n  epp:\n    replicas: 2"),
            ],
        },
        "resources": [],
    }
    return {
        "candidate_config": {
            "type": "baseline",
            "candidate_source": {"name": "manual"},
            "target": {"model": "New/Model"},
            "serving": {"replicas": 2, "tensor_parallel_size": 1},
            "runtime": {"image": "model:v1"},
            "guide_settings": {"routerValues": "router:\n  epp:\n    replicas: 2"},
        },
        "render": {
            "format": "manifest",
            "guide_ref": "optimized-baseline",
            "template_ref": source["files"][0],
            "guide_source": source,
            "rendered_manifest": manifest,
            "deployment_bundle": bundle,
            "cluster_ref": {
                "id": "cluster",
                "name": "cluster",
                "session_id": "00000000-0000-0000-0000-000000000001",
                "connection": "managed-kubeconfig",
            },
            "deployment": {
                "readinessDeployments": ["decode"],
                "endpoint": {"protocol": "http", "serviceName": "model", "port": 8000},
            },
        },
    }


def setup_service(monkeypatch, tmp_path):
    monkeypatch.setattr(
        service,
        "require_active_session",
        lambda _: SimpleNamespace(id="00000000-0000-0000-0000-000000000001", server_id="cluster"),
    )
    monkeypatch.setattr(service, "CONFIGURATION_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(service, "CONFIGURATION_ARTIFACT_DIR", tmp_path / ".artifacts")


def test_router_settings_and_inputs_survive_render_save_reload(monkeypatch, tmp_path):
    setup_service(monkeypatch, tmp_path)
    request = request_payload()
    rendered = service.render_configuration(RenderRequest.model_validate(request))
    saved = service.save_configuration(SaveRequest(deployable_configuration=rendered.deployable_configuration))
    restored = service.get_configuration_artifact(saved.artifact.artifact_id)
    assert restored.deployable_configuration.content["guideSettings"] == {
        "routerValues": "router:\n  epp:\n    replicas: 2"
    }
    assert (
        restored.deployable_configuration.content["officialGuide"]["deploymentBundle"]
        == request["render"]["deployment_bundle"]
    )


def test_rechecks_router_semantics_even_if_all_checksums_are_recomputed(monkeypatch, tmp_path):
    setup_service(monkeypatch, tmp_path)
    rendered = service.render_configuration(RenderRequest.model_validate(request_payload()))
    configuration = deepcopy(rendered.deployable_configuration)
    configuration.content["officialGuide"]["deploymentBundle"]["helm"]["values"][-1] = asset(
        "router-effective.yaml", "router:\n  epp:\n    replicas: 9"
    )
    configuration.checksum = configuration_checksum(configuration.content)
    with pytest.raises(ValueError, match="Effective router values"):
        service.save_configuration(SaveRequest(deployable_configuration=configuration))


def test_same_filename_is_saved_immutably_and_registered(monkeypatch, tmp_path):
    import json

    setup_service(monkeypatch, tmp_path)
    rendered = service.render_configuration(RenderRequest.model_validate(request_payload()))
    request = SaveRequest(deployable_configuration=rendered.deployable_configuration)
    first = service.save_configuration(request)
    second = service.save_configuration(request)
    assert first.configuration_file.file_name == second.configuration_file.file_name
    assert first.configuration_file.path != second.configuration_file.path
    for saved in (first, second):
        root = tmp_path / saved.artifact.artifact_id
        assert (root / saved.configuration_file.file_name).is_file()
        manifest = json.loads((root / "manifest.json").read_text())
        assert manifest["source_version"] == "a" * 40
        assert saved.artifact.artifact_id in manifest["configuration_ids"]
        assert saved.configuration_file.uri.startswith("lens-artifact://configuration/")
    assert service.delete_configuration_artifact(first.artifact.artifact_id)
    assert (tmp_path / second.artifact.artifact_id / second.configuration_file.file_name).is_file()


@pytest.mark.parametrize("symlink_target", ["owner", "payload"])
def test_delete_rejects_sibling_owner_symlink(monkeypatch, tmp_path, symlink_target):
    import shutil

    setup_service(monkeypatch, tmp_path)
    rendered = service.render_configuration(RenderRequest.model_validate(request_payload()))
    request = SaveRequest(deployable_configuration=rendered.deployable_configuration)
    first = service.save_configuration(request)
    second = service.save_configuration(request)
    owner = tmp_path / first.artifact.artifact_id
    sibling = tmp_path / second.artifact.artifact_id
    if symlink_target == "owner":
        shutil.rmtree(owner)
        owner.symlink_to(sibling, target_is_directory=True)
    else:
        payload = owner / first.configuration_file.file_name
        payload.unlink()
        payload.symlink_to(sibling / second.configuration_file.file_name)
    with pytest.raises(ValueError, match="symlink"):
        service.delete_configuration_artifact(first.artifact.artifact_id)
    assert (sibling / second.configuration_file.file_name).is_file()
    assert service.get_configuration_artifact(first.artifact.artifact_id) is not None
