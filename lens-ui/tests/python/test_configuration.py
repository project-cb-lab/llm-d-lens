# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from llm_d_bench.configuration.models import RenderRequest, ResolveRequest, SaveRequest
from llm_d_bench.configuration.service import (
    get_configuration_artifact,
    list_configuration_artifacts,
    render_configuration,
    resolve_configurations,
    save_configuration,
)


@pytest.mark.asyncio
async def test_resolve_normalizes_pd_aliases_and_validates_resources(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.configuration.service.check_capability",
        AsyncMock(return_value=(True, None, [])),
    )
    request = ResolveRequest.model_validate(
        {
            "candidate_source": {"name": "pd_search", "run_id": "run-1"},
            "configurations": [
                {
                    "configuration_id": "result-1",
                    "input_mode": "source_result",
                    "payload": {
                        "model": "example/model",
                        "hardware": {"accelerator_model": "b60", "accelerator_count": "8"},
                        "prefill": {"tp": "4", "replicas": "1"},
                        "decode": {"tensor_parallel": 2, "workers": 2},
                        "max_model_len": "32768",
                    },
                }
            ],
            "target": {"cluster_id": "cluster-1"},
        }
    )

    response = await resolve_configurations(request)

    result = response.results[0]
    assert result.validation.status == "valid"
    assert result.candidate_config.type == "pd"
    assert result.candidate_config.prefill == {"tensor_parallel_size": 4, "replicas": 1}
    assert result.candidate_config.decode == {"tensor_parallel_size": 2, "replicas": 2}
    assert result.candidate_config.serving == {"max_model_len": 32768}
    assert result.candidate_config.candidate_source.result_id == "result-1"
    assert result.candidate_config.network["inference_pool"]["target_port_number"] == 8000


@pytest.mark.asyncio
async def test_resolve_returns_invalid_result_for_unsupported_hardware(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.configuration.service.check_capability",
        AsyncMock(return_value=(False, "unsupported model and hardware", [])),
    )
    request = ResolveRequest.model_validate(
        {
            "candidate_source": {"name": "baseline_search"},
            "configurations": [
                {
                    "configuration_id": "baseline-1",
                    "payload": {
                        "model": "example/model",
                        "hardware": {"accelerator_model": "B200", "accelerator_count": 8},
                        "serving": {"tp": 2, "replicas": 2},
                    },
                }
            ],
        }
    )

    result = (await resolve_configurations(request)).results[0]

    assert result.validation.status == "invalid"
    assert result.validation.errors == ["unsupported model and hardware"]


def test_render_and_save_preserve_deployment_type(monkeypatch, tmp_path):
    monkeypatch.setattr("llm_d_bench.configuration.service.CONFIGURATION_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("llm_d_bench.configuration.service.CONFIGURATION_ARTIFACT_DIR", tmp_path / ".artifacts")
    monkeypatch.setattr(
        "llm_d_bench.configuration.service.require_active_session",
        lambda _session_id: SimpleNamespace(id="00000000-0000-0000-0000-000000000001", server_id="cluster-1"),
    )
    manifest = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: pd-prefill
  labels:
    llm-d.ai/role: prefill
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: modelserver
          image: registry/model:v1
          command: [vllm, serve]
          args: [example/model, --tensor-parallel-size=4, --max-model-len=32768]
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pd-decode
  labels:
    llm-d.ai/role: decode
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: modelserver
          image: registry/model:v1
          command: [vllm, serve]
          args: [example/model, --tensor-parallel-size=2, --max-model-len=32768]
---
apiVersion: v1
kind: Service
metadata:
  name: pd-modelserver
spec:
  ports:
    - port: 8000
"""
    render_request = RenderRequest.model_validate(
        {
            "candidate_config": {
                "type": "pd",
                "candidate_source": {"name": "pd_search"},
                "target": {"model": "example/model"},
                "serving": {"max_model_len": 32768},
                "prefill": {"tensor_parallel_size": 4, "replicas": 1},
                "decode": {"tensor_parallel_size": 2, "replicas": 2},
                "network": {
                    "protocol": "HTTP",
                    "inference_pool": {
                        "target_port_number": 8080,
                        "selector": {"llm-d.ai/inferenceServing": "true"},
                        "extension_ref": {"port_number": 9002, "failure_mode": "FailClose"},
                    },
                    "http_route": {"path_prefix": "/v1"},
                },
                "runtime": {"image": "registry/model:v1", "imageMode": "use-upstream-image"},
            },
            "render": {
                "format": "manifest",
                "guide_ref": "pd-disaggregation",
                "template_ref": "guides/pd-disaggregation/modelserver/xpu/vllm/kustomization.yaml",
                "guide_source": {
                    "repository": "llm-d/llm-d",
                    "requestedRef": "main",
                    "commit": "0123456789abcdef0123456789abcdef01234567",
                    "guide": "pd-disaggregation",
                    "accelerator": "xpu",
                    "modelServer": "vllm",
                    "variant": "base",
                    "files": ["guides/pd-disaggregation/modelserver/xpu/vllm/kustomization.yaml"],
                },
                "rendered_manifest": manifest,
                "cluster_ref": {
                    "id": "cluster-1",
                    "name": "B60 cluster",
                    "session_id": "00000000-0000-0000-0000-000000000001",
                    "connection": "managed-kubeconfig",
                },
                "deployment": {
                    "readinessDeployments": ["pd-prefill", "pd-decode"],
                    "endpoint": {"protocol": "http", "serviceName": "pd-modelserver", "port": 8000},
                },
            },
        }
    )

    rendered = render_configuration(render_request)
    saved = save_configuration(
        SaveRequest(
            deployable_configuration=rendered.deployable_configuration,
            file={"name": "../pd-example.yaml"},
        )
    )

    assert rendered.deployable_configuration.type == "pd"
    assert rendered.deployable_configuration.provider_ref == "pd-disaggregation"
    assert rendered.deployable_configuration.content["prefill"]["tensorParallelSize"] == 4
    assert rendered.deployable_configuration.content["network"]["inference_pool"]["target_port_number"] == 8080
    assert rendered.deployable_configuration.content["network"]["http_route"]["path_prefix"] == "/v1"
    assert rendered.deployable_configuration.content["runtime"]["image"] == "registry/model:v1"
    assert rendered.deployable_configuration.content["officialGuide"]["source"]["guide"] == "pd-disaggregation"
    assert rendered.deployable_configuration.content["officialGuide"]["cluster"]["id"] == "cluster-1"
    assert "kind: Deployment" in rendered.deployable_configuration.content["officialGuide"]["renderedManifest"]
    assert rendered.metadata["guide_source"]["repository"] == "llm-d/llm-d"
    assert "serving" not in rendered.deployable_configuration.content
    assert saved.configuration_file.file_name == "pd-example.yaml"
    assert saved.configuration_file.path == f"/configs/{saved.artifact.artifact_id}/pd-example.yaml"
    stored_path = tmp_path / saved.configuration_file.path.removeprefix("/configs/")
    stored = list(yaml.safe_load_all(stored_path.read_text()))
    assert [document["kind"] for document in stored] == ["Deployment", "Deployment", "Service"]
    assert len(saved.configuration_file.sha256) == 64
    assert saved.artifact.deployable_configuration == rendered.deployable_configuration
    assert get_configuration_artifact(saved.artifact.artifact_id) == saved.artifact
    assert list_configuration_artifacts() == [saved.artifact]


def test_render_rejects_manifest_model_that_disagrees_with_candidate(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.configuration.service.require_active_session",
        lambda _session_id: SimpleNamespace(id="00000000-0000-0000-0000-000000000001", server_id="cluster-1"),
    )
    manifest = yaml.safe_dump_all(
        [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "baseline-decode", "labels": {"llm-d.ai/role": "decode"}},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "modelserver",
                                    "image": "registry/model:v1",
                                    "command": ["vllm", "serve"],
                                    "args": ["old/model", "--tensor-parallel-size=1"],
                                }
                            ]
                        }
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "baseline-modelserver"},
                "spec": {"ports": [{"port": 8000}]},
            },
        ]
    )
    request = RenderRequest.model_validate(
        {
            "candidate_config": {
                "type": "baseline",
                "candidate_source": {"name": "manual"},
                "target": {"model": "new/model"},
                "serving": {"tensor_parallel_size": 1, "replicas": 1},
                "runtime": {"image": "registry/model:v1"},
            },
            "render": {
                "format": "manifest",
                "guide_ref": "optimized-baseline",
                "template_ref": "guides/optimized-baseline/modelserver/xpu/vllm/kustomization.yaml",
                "guide_source": {
                    "repository": "llm-d/llm-d",
                    "requestedRef": "main",
                    "commit": "0123456789abcdef0123456789abcdef01234567",
                    "guide": "optimized-baseline",
                    "accelerator": "xpu",
                    "modelServer": "vllm",
                    "variant": "base",
                    "files": ["guides/optimized-baseline/modelserver/xpu/vllm/kustomization.yaml"],
                },
                "rendered_manifest": manifest,
                "cluster_ref": {
                    "id": "cluster-1",
                    "name": "B60 cluster",
                    "session_id": "00000000-0000-0000-0000-000000000001",
                    "connection": "managed-kubeconfig",
                },
                "deployment": {
                    "readinessDeployments": ["baseline-decode"],
                    "endpoint": {
                        "protocol": "http",
                        "serviceName": "baseline-modelserver",
                        "port": 8000,
                    },
                },
            },
        }
    )

    with pytest.raises(ValueError, match="model"):
        render_configuration(request)
