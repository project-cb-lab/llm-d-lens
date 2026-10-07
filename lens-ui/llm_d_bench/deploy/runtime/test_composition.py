"""Tests for portable Deploy runtime path discovery."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_d_bench.configuration.models import ModelSecretConfiguration
from llm_d_bench.deploy.runtime.composition import (
    RegisteredGuideCommandRunner,
    RegisteredGuideRuntime,
    RestrictedKubectlRunner,
    RuntimeConfigurationError,
    RuntimeEnvironment,
    _build_precise_prefix_cache_routing_provider,
    _build_tiered_prefix_cache_provider,
    build_namespace_factory,
)


def _executable(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    root = tmp_path / "llm-d"
    (root / "guides").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = _executable(bin_dir, "kubectl")
    _executable(bin_dir, "helm")
    _executable(bin_dir, "docker")
    return {
        "LLM_D_ROOT": str(root),
        "PATH": str(bin_dir),
        "LLM_D_BENCH_KUBECTL_PATH": "",
        "LLM_D_BENCH_HELM_PATH": "",
        "LLM_D_BENCH_DOCKER_PATH": "",
    }, kubectl


def test_runtime_discovers_executables_from_path(tmp_path: Path):
    environment, kubectl = _environment(tmp_path)

    runtime = RuntimeEnvironment.from_environment(environment)

    assert runtime is not None
    assert runtime.kubectl_path == kubectl
    assert runtime.helm_path == kubectl.with_name("helm")
    assert runtime.docker_path == kubectl.with_name("docker")


def test_runtime_rejects_relative_executable_override(tmp_path: Path):
    environment, _ = _environment(tmp_path)
    environment["LLM_D_BENCH_DOCKER_PATH"] = "bin/docker"

    with pytest.raises(RuntimeConfigurationError, match="executable absolute path"):
        RuntimeEnvironment.from_environment(environment)


def test_tiered_prefix_cache_provider_registers_all_upstream_xpu_variants(tmp_path: Path):
    environment, _ = _environment(tmp_path)
    runtime = RuntimeEnvironment.from_environment(environment)

    adapter = _build_tiered_prefix_cache_provider(runtime)

    assert set(adapter._descriptor.variants) == {
        "base",
        "native/cpu/base",
        "lmcache-connector/cpu/base",
    }
    assert adapter._descriptor.variants["lmcache-connector/cpu/base"].name == "base"
    assert adapter._descriptor.variants["lmcache-connector/cpu/base"].parent.name == "cpu"
    # Native OffloadingConnector stays the recommended default; LMCache is opt-in.
    assert adapter._descriptor.default_variant == "native/cpu/base"


def test_existing_model_secret_requires_namespace_and_name():
    with pytest.raises(ValidationError, match="sourceNamespace and sourceName"):
        ModelSecretConfiguration(mode="existing-secret")


def test_model_secret_defaults_to_no_credential():
    assert ModelSecretConfiguration().mode == "none"


def test_precise_prefix_cache_routing_provider_registers_modelserver_render_and_baseline(tmp_path: Path):
    environment, _ = _environment(tmp_path)
    runtime = RuntimeEnvironment.from_environment(environment)

    adapter = _build_precise_prefix_cache_routing_provider(runtime)

    # _environment()'s fixture guides/ tree has no nested llm-d/ subdirectory, so the
    # provider's guide_root fallback resolves directly to manifest_root (same fallback
    # tiered-prefix-cache's builder uses).
    guide_root = runtime.manifest_root
    assert adapter._modelserver_source == guide_root / "guides/precise-prefix-cache-routing/modelserver/xpu/vllm"
    assert adapter._render_source == guide_root / "guides/precise-prefix-cache-routing/render"
    assert adapter._baseline_source.name == "baseline"
    assert adapter._baseline_source.is_dir()
    assert (
        adapter._router_values
        == guide_root / "guides/precise-prefix-cache-routing/router/precise-prefix-cache-routing.values.yaml"
    )
    assert adapter.discover().guide_id == "precise-prefix-cache-routing"

    configured = ModelSecretConfiguration(
        mode="existing-secret", sourceNamespace="model-secrets", sourceName="huggingface"
    )
    assert configured.source_namespace == "model-secrets"


def test_tiered_prefix_cache_provider_renders_gpu_when_the_run_recorded_an_nvidia_accelerator(tmp_path: Path):
    environment, _ = _environment(tmp_path)
    runtime = RuntimeEnvironment.from_environment({**environment, "PRISM_DEPLOY_ACCELERATOR": "gpu"})

    adapter = _build_tiered_prefix_cache_provider(runtime)

    assert all(str(path).count("/gpu/") == 1 for path in adapter._descriptor.variants.values())


def test_precise_prefix_cache_routing_provider_renders_gpu_when_the_run_recorded_an_nvidia_accelerator(
    tmp_path: Path,
):
    environment, _ = _environment(tmp_path)
    runtime = RuntimeEnvironment.from_environment({**environment, "PRISM_DEPLOY_ACCELERATOR": "gpu"})

    adapter = _build_precise_prefix_cache_routing_provider(runtime)

    guide_root = runtime.manifest_root
    assert adapter._modelserver_source == guide_root / "guides/precise-prefix-cache-routing/modelserver/gpu/vllm/base"


def test_concurrent_runs_with_different_accelerators_do_not_share_overlay_state(tmp_path: Path):
    """Two runs for different clusters must each render their own vendor's overlay.

    Nothing here is process-global (no shared env var is mutated), so building
    an XPU-run provider after a GPU-run provider must not leak the GPU choice,
    and vice versa -- this is the regression the per-run ``accelerator`` field
    protects against.
    """
    environment, _ = _environment(tmp_path)
    gpu_runtime = RuntimeEnvironment.from_environment({**environment, "PRISM_DEPLOY_ACCELERATOR": "gpu"})
    xpu_runtime = RuntimeEnvironment.from_environment({**environment, "PRISM_DEPLOY_ACCELERATOR": "xpu"})

    gpu_adapter = _build_precise_prefix_cache_routing_provider(gpu_runtime)
    xpu_adapter = _build_precise_prefix_cache_routing_provider(xpu_runtime)
    gpu_adapter_again = _build_precise_prefix_cache_routing_provider(gpu_runtime)

    guide_root = gpu_runtime.manifest_root
    assert gpu_adapter._modelserver_source == guide_root / "guides/precise-prefix-cache-routing/modelserver/gpu/vllm/base"
    assert xpu_adapter._modelserver_source == guide_root / "guides/precise-prefix-cache-routing/modelserver/xpu/vllm"
    assert gpu_adapter_again._modelserver_source == gpu_adapter._modelserver_source


def test_registered_guide_allows_idempotent_namespace_cleanup(tmp_path: Path):
    namespace = "llm-d-bench-optimized-baseline-test"
    runner = RegisteredGuideCommandRunner(
        kubectl_path=tmp_path / "kubectl",
        helm_path=tmp_path / "helm",
        namespace_prefix="llm-d-bench-",
        guide=RegisteredGuideRuntime(
            guide_id="optimized-baseline",
            source_ref="main",
            content_hash="test",
            maturity="supported",
            manifest_path=tmp_path / "kustomization.yaml",
            readiness_deployment_name="optimized-baseline-xpu-vllm-decode",
        ),
        timeout_seconds=30,
        rendered_overlay_root=tmp_path,
    )
    runner.register_restored_namespace(namespace)

    runner._validate_kustomize(
        [
            "kubectl",
            "delete",
            "namespace",
            namespace,
            "--wait=false",
            "--ignore-not-found=true",
        ]
    )
    runner._validate_kustomize(
        [
            "kubectl",
            "delete",
            "namespace",
            namespace,
            "--ignore-not-found=true",
        ]
    )
    runner._validate_kustomize(
        [
            "kubectl",
            "delete",
            "namespace",
            namespace,
        ]
    )


def test_namespace_factory_uses_the_request_policy_prefix():
    request = type(
        "Request",
        (),
        {
            "provenance": {"guide_id": "baseline-vllm"},
            "deployment_policy": type(
                "Policy",
                (),
                {
                    "value": {"namespace_policy": {"prefix": "standard-"}},
                },
            )(),
            "configuration_artifacts": [
                type(
                    "Artifact",
                    (),
                    {
                        "content": '{"decode":{"replicaCount":2,"tensorParallelSize":4}}',
                    },
                )()
            ],
        },
    )()

    namespace = build_namespace_factory("llmd-")(request)

    assert namespace.startswith("standard-2-4-")
    assert not namespace.startswith("llmd-")


@pytest.mark.parametrize(
    "command_tail",
    [
        ["-l", "llm-d.ai/role=decode", "-c", "modelserver", "--tail=120", "--prefix=true"],
        ["-l", "llm-d.ai/role=prefill", "-c", "modelserver", "--tail=120", "--prefix=true"],
        ["-l", "llm-d.ai/role=decode", "-c", "routing-proxy", "--tail=120", "--prefix=true"],
        ["-l", "llm-d.ai/role=decode", "-c", "routing-proxy", "--previous", "--tail=120", "--prefix=true"],
    ],
)
def test_restricted_kubectl_allows_pd_diagnostic_logs(tmp_path: Path, command_tail: list[str]):
    runner = RestrictedKubectlRunner(tmp_path / "kubectl", "llm-d-bench-")

    runner._validate(
        [
            "kubectl",
            "logs",
            "--namespace",
            "llm-d-bench-pd-test",
            *command_tail,
        ]
    )


@pytest.mark.asyncio
async def test_published_deployment_names_scope_commands_and_smoke_test(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from llm_d_bench.deploy.runtime import composition

    namespace = "llm-d-bench-published"
    runner = RegisteredGuideCommandRunner(
        kubectl_path=tmp_path / "kubectl",
        helm_path=tmp_path / "helm",
        namespace_prefix="llm-d-bench-",
        guide=RegisteredGuideRuntime(
            guide_id="optimized-baseline",
            source_ref="main",
            content_hash="test",
            maturity="supported",
            manifest_path=tmp_path / "kustomization.yaml",
            readiness_deployment_name="old-decode",
            endpoint_service_name="epp",
            endpoint_service_port=80,
        ),
        timeout_seconds=30,
        rendered_overlay_root=tmp_path,
    )
    runner.register_published_deployments(namespace, ["published-decode"])
    runner._validate_kustomize(
        ["kubectl", "rollout", "status", "deployment/published-decode", "--namespace", namespace, "--timeout=30s"]
    )
    runner._validate_kustomize(
        ["kubectl", "scale", "deployment/published-decode", "--namespace", namespace, "--replicas=0"]
    )
    with pytest.raises(RuntimeConfigurationError):
        runner._validate_kustomize(
            ["kubectl", "scale", "deployment/unrelated", "--namespace", namespace, "--replicas=0"]
        )
    with pytest.raises(RuntimeConfigurationError):
        runner._validate_kustomize(
            ["kubectl", "scale", "deployment/published-decode", "--namespace", "llm-d-bench-other", "--replicas=0"]
        )
    with pytest.raises(RuntimeConfigurationError):
        runner.register_published_deployments("default", ["published-decode"])
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b"ok", b"")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(composition, "spawn", spawn)
    assert await runner.endpoint_smoke_test(namespace, f"http://epp.{namespace}.svc:80") is None
    assert spawn.call_args.args[0][2] == "deployment/published-decode"
