"""Tests for the plain-Kubernetes vLLM baseline provider."""

from llm_d_bench.deploy.providers.baseline_vllm import BaselineVllmAdapter


def _base_overrides():
    return {
        "model": {"name": "Qwen/Qwen3-0.6B"},
        "decode": {"replicaCount": 2, "tensorParallelSize": 1},
        "runtime": {"image": "vllm/vllm-openai:latest"},
    }


def _container(overrides):
    parameters = BaselineVllmAdapter._parameters(overrides)
    resources = BaselineVllmAdapter._resources(parameters)
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    return deployment["spec"]["template"]["spec"]["containers"][0]


def _environment(container):
    return {item["name"]: item.get("value") for item in container["env"]}


def test_resources_name_the_modelserver_container_port():
    # The deployment's PodMonitor (monitoring/deployment/service.py) selects a
    # scrape endpoint by named port ("modelserver"), not targetPort, so an
    # unnamed containerPort silently drops the pod from Prometheus scraping
    # and leaves the profiling Flow Map with no traffic at all.
    container = _container(_base_overrides())
    assert container["ports"] == [{"name": "modelserver", "containerPort": 8000}]


def test_auto_cache_serving_pod_runs_huggingface_offline():
    overrides = _base_overrides()
    overrides["runtime"]["modelSource"] = "auto-cache"
    environment = _environment(_container(overrides))
    assert environment["HF_HOME"] == "/model-cache"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"


def test_huggingface_source_serving_pod_stays_online():
    environment = _environment(_container(_base_overrides()))
    assert "HF_HUB_OFFLINE" not in environment
    assert "TRANSFORMERS_OFFLINE" not in environment


def test_shared_path_mount_serves_the_local_directory_offline():
    overrides = _base_overrides()
    overrides["runtime"].update({"modelSource": "shared-path", "mountPath": "/shared/models/Qwen3-0.6B"})
    environment = _environment(_container(overrides))
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    # The model is served from the mounted directory, not resolved through HF_HOME.
    assert "HF_HOME" not in environment
