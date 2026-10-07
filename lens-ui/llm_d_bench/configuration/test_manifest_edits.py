from types import SimpleNamespace

import pytest
import yaml

from llm_d_bench.configuration import service
from llm_d_bench.configuration.models import SaveRequest
from llm_d_bench.utils.artifacts import configuration_checksum, text_checksum


def manifest(replicas=1):
    return yaml.safe_dump_all(
        [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "serving"},
                "spec": {
                    "replicas": replicas,
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": "modelserver", "image": "image", "args": ["model", "--tensor-parallel-size=1"]}
                            ]
                        }
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "serving"},
                "spec": {"ports": [{"port": 8000}]},
            },
        ]
    )


def test_edit_cannot_save_changed_runtime_with_stale_configuration_facts(monkeypatch, tmp_path):
    original = manifest()
    edited = manifest(2)
    content = {
        "model": {"name": "model"},
        "decode": {"replicaCount": 1, "tensorParallelSize": 1},
        "officialGuide": {
            "renderedManifest": edited,
            "manifestChecksum": text_checksum(edited),
            "deployment": {
                "readinessDeployments": ["serving"],
                "endpoint": {"serviceName": "serving", "port": 8000, "protocol": "http"},
            },
        },
    }
    monkeypatch.setattr(service, "CONFIGURATION_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(service, "CONFIGURATION_ARTIFACT_DIR", tmp_path / ".artifacts")
    monkeypatch.setattr(service, "require_active_session", lambda _: SimpleNamespace(id="session", server_id="cluster"))
    monkeypatch.setattr(
        service,
        "get_configuration_artifact",
        lambda _: SimpleNamespace(
            deployable_configuration=SimpleNamespace(content={"officialGuide": {"renderedManifest": original}})
        ),
    )
    request = SaveRequest.model_validate(
        {
            "deployable_configuration": {
                "type": "baseline",
                "provider_ref": "optimized-baseline",
                "format": "manifest",
                "content": content,
                "checksum": configuration_checksum(content),
                "provenance": {
                    "edited_from_artifact_id": "source",
                    "cluster_ref": {"id": "cluster", "session_id": "session"},
                },
            }
        }
    )
    with pytest.raises(ValueError, match="replica count"):
        service.save_configuration(request)
    assert not list(tmp_path.glob("*.yaml"))


def test_yaml_annotation_and_formatting_edits_preserve_facts():
    from llm_d_bench.configuration.manifest_edits import validate_manifest_edit

    original = manifest()
    documents = list(yaml.safe_load_all(original))
    documents[0]["metadata"]["annotations"] = {"description": "reviewed"}
    validate_manifest_edit(original, yaml.safe_dump_all(list(reversed(documents))))


@pytest.mark.parametrize(
    "field,value", [("image", "different-image"), ("args", ["another-model"]), ("env", [{"name": "NEW", "value": "1"}])]
)
def test_yaml_runtime_changes_require_typed_editor(field, value):
    from llm_d_bench.configuration.manifest_edits import validate_manifest_edit

    original = manifest()
    documents = list(yaml.safe_load_all(original))
    documents[0]["spec"]["template"]["spec"]["containers"][0][field] = value
    with pytest.raises(ValueError, match="configuration editor"):
        validate_manifest_edit(original, yaml.safe_dump_all(documents))


def test_uploaded_yaml_cannot_diverge_from_the_planned_configuration(monkeypatch):
    from llm_d_bench.configuration.models import RenderRequest

    monkeypatch.setattr(service, "require_active_session", lambda _: SimpleNamespace(server_id="cluster"))
    request = RenderRequest.model_validate(
        {
            "candidate_config": {
                "type": "baseline",
                "candidate_source": {"name": "manual"},
                "target": {"model": "model"},
                "decode": {"replicas": 1, "tensor_parallel_size": 1},
            },
            "render": {
                "format": "manifest",
                "guide_ref": "optimized-baseline",
                "template_ref": "upload/a.yaml",
                "guide_source": {
                    "repository": "llm-d/llm-d",
                    "requestedRef": "main",
                    "commit": "0" * 40,
                    "guide": "optimized-baseline",
                    "accelerator": "xpu",
                    "modelServer": "vllm",
                    "variant": "base",
                    "files": ["a.yaml"],
                },
                "rendered_manifest": manifest(2),
                "reference_manifest": manifest(1),
                "cluster_ref": {
                    "id": "cluster",
                    "session_id": "11111111-1111-1111-1111-111111111111",
                    "name": "Cluster",
                    "connection": "managed-kubeconfig",
                },
                "deployment": {
                    "readinessDeployments": ["serving"],
                    "endpoint": {"serviceName": "serving", "port": 8000, "protocol": "http"},
                },
            },
        }
    )
    with pytest.raises(ValueError, match="configuration editor"):
        service.render_configuration(request)
