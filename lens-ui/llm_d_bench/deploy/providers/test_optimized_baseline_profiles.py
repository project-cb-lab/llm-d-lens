import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from llm_d_bench.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact
from llm_d_bench.deploy.providers.optimized_baseline import (
    OptimizedBaselineGuideAdapter,
    OptimizedBaselineGuidePolicy,
)


async def _runner(_command):
    return 0, "", ""


def _adapter(tmp_path: Path) -> tuple[OptimizedBaselineGuideAdapter, Path]:
    root = tmp_path / "guide"
    overlay = root / "rendered"
    overlay.mkdir(parents=True)
    full = root / "optimized.values.yaml"
    plugin_document = {
        "apiVersion": "llm-d.ai/v1alpha1",
        "kind": "EndpointPickerConfig",
        "plugins": [
            {"type": "approx-prefix-cache-producer"},
            {"type": "inflight-load-producer"},
            {"type": "prefix-cache-affinity-filter", "parameters": {"peakPrefillThroughput": 100}},
            {"type": "token-load-scorer"},
        ],
        "schedulingProfiles": [
            {
                "name": "default",
                "plugins": [
                    {"pluginRef": "prefix-cache-affinity-filter"},
                    {"pluginRef": "token-load-scorer"},
                ],
            }
        ],
    }
    full.write_text(
        yaml.safe_dump(
            {
                "router": {
                    "epp": {
                        "pluginsConfigFile": "full.yaml",
                        "pluginsCustomConfig": {"full.yaml": yaml.safe_dump(plugin_document)},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    neutral = root / "neutral.values.yaml"
    neutral.write_text("router: {}\n", encoding="utf-8")
    policy = OptimizedBaselineGuidePolicy(
        namespace_prefix="test",
        guide_root=root,
        overlay_path=overlay,
        router_base_values_path=full,
        router_values_path=full,
        neutral_router_values_path=neutral,
        gateway_mode_values_path=neutral,
        router_chart="chart",
        router_chart_version="1",
        router_release_name="router",
        model_deployment_name="model",
        endpoint_service_name="epp",
        endpoint_service_port=80,
        baseline_service_name="modelserver",
        baseline_service_port=8000,
        rendered_overlay_root=root,
    )
    return OptimizedBaselineGuideAdapter(
        GuideDefinition("optimized-baseline", "source", "hash", "supported"),
        _runner,
        policy=policy,
    ), overlay


def _plugin_types(values_path: Path) -> tuple[set[str], list[str]]:
    values = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    epp = values["router"]["epp"]
    document = yaml.safe_load(epp["pluginsCustomConfig"][epp["pluginsConfigFile"]])
    return {item["type"] for item in document["plugins"]}, [
        item["pluginRef"] for item in document["schedulingProfiles"][0]["plugins"]
    ]


def _asset(name: str, content: str) -> dict[str, str]:
    return {"name": name, "content": content, "checksum": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"}


def _bundle(values: str = "router:\n  saved: true\n") -> dict:
    return {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "optimized-baseline",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "saved-chart",
            "version": "saved-version",
            "releaseName": "saved-release",
            "values": [_asset("router-effective.yaml", values)],
        },
        "resources": [],
    }


def test_load_only_profile_keeps_load_producer_and_scorer(tmp_path):
    adapter, overlay = _adapter(tmp_path)
    types, scheduled = _plugin_types(adapter._router_values_for_profile("load-only", overlay))
    assert types == {"inflight-load-producer", "token-load-scorer"}
    assert scheduled == ["token-load-scorer"]


def test_affinity_only_profile_keeps_affinity_gate_without_final_scorer(tmp_path):
    adapter, overlay = _adapter(tmp_path)
    types, scheduled = _plugin_types(adapter._router_values_for_profile("affinity-only", overlay))
    assert types == {"approx-prefix-cache-producer", "inflight-load-producer", "prefix-cache-affinity-filter"}
    assert scheduled == ["prefix-cache-affinity-filter"]


def test_neutral_profile_preserves_saved_user_values_while_replacing_policy(tmp_path):
    adapter, _overlay = _adapter(tmp_path)
    values = yaml.safe_load(adapter._policy.router_values_path.read_text(encoding="utf-8"))
    values["userSetting"] = {"preserved": True}

    neutral = yaml.safe_load(
        adapter._profiled_router_values_content(
            yaml.safe_dump(values, sort_keys=False),
            "router-neutral",
        )
    )

    assert neutral["userSetting"] == {"preserved": True}
    epp = neutral["router"]["epp"]
    plugins = yaml.safe_load(epp["pluginsCustomConfig"][epp["pluginsConfigFile"]])
    assert plugins["plugins"] == [{"type": "random-picker"}]


@pytest.mark.asyncio
async def test_published_default_profile_installs_bundle_after_guide_values_disappear(tmp_path):
    adapter, overlay = _adapter(tmp_path)
    commands = []

    async def bundle_runner(command):
        commands.append(command)
        return (0, "kind: Deployment\n" if command[1] == "template" else "ok", "")

    adapter._bundle_command_runner = bundle_runner
    adapter._policy.router_base_values_path.unlink()
    artifact = GuideDeploymentArtifact(
        "optimized-baseline",
        "hash",
        manifest_ref=str(overlay),
        source_ref="a" * 40,
        deployment_contract={"deploymentBundle": _bundle()},
    )
    context = {"namespace": "test-run"}

    await adapter.install_published_router(artifact, context)

    assert commands[0][1:5] == ["template", "saved-release", "saved-chart", "--namespace"]
    # Gateway Mode: the saved effective values are merged with the shared-Gateway
    # values so the deployment's own proxy is disabled for the shared Gateway.
    effective = yaml.safe_load(context["router_effective_values"])
    assert effective["router"]["saved"] is True
    assert effective["router"]["proxy"]["enabled"] is False
    assert effective["router"]["epp"]["flags"]["secure-serving"] is False


@pytest.mark.asyncio
async def test_published_ablation_profile_derives_from_saved_values_for_manifest_file(tmp_path):
    adapter, overlay = _adapter(tmp_path)
    bundle_commands = []

    async def bundle_runner(command):
        bundle_commands.append(command)
        return 0, "kind: Deployment\n", ""

    adapter._bundle_command_runner = bundle_runner
    saved_values = adapter._policy.router_values_path.read_text(encoding="utf-8")
    manifest = overlay / "published-manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        "optimized-baseline",
        "hash",
        manifest_ref=str(manifest),
        source_ref="a" * 40,
        deployment_contract={"deploymentBundle": _bundle(saved_values), "routerProfile": "load-only"},
    )

    await adapter.install_published_router(artifact, {"namespace": "test-run"})

    values_argument = bundle_commands[0][bundle_commands[0].index("--values") + 1]
    types, scheduled = _plugin_types(Path(values_argument))
    assert types == {"inflight-load-producer", "token-load-scorer"}
    assert scheduled == ["token-load-scorer"]
    assert "saved-chart" in bundle_commands[0]


@pytest.mark.asyncio
async def test_published_readiness_and_stop_use_saved_deployment_names(tmp_path):
    adapter, _ = _adapter(tmp_path)
    commands = []

    async def runner(command):
        commands.append(command)
        if "deployment/model" in command:
            return 1, "", 'Error from server (NotFound): deployments.apps "model" not found'
        return 0, "ok", ""

    async def smoke_test(namespace, endpoint):
        return None

    runner.endpoint_smoke_test = smoke_test
    adapter._command_runner = runner
    artifact = GuideDeploymentArtifact(
        "optimized-baseline",
        "hash",
        deployment_contract={
            "readinessDeployments": ["saved-decode", "saved-helper"],
            "endpoint": {"protocol": "http", "serviceName": "saved-modelserver", "port": 80},
        },
    )
    execution = {"namespace": "test-run", "_artifact": artifact}
    result = await adapter.readiness(execution)
    assert result.accepted, result.reasons
    assert [command[3] for command in commands] == ["deployment/saved-decode", "deployment/saved-helper"]
    assert "epp.test-run.svc" in execution["endpoint_url"]
    assert "saved-modelserver.test-run.svc" in execution["baseline_endpoint_url"]
    commands.clear()
    result = await adapter.stop(execution, artifact)
    assert result["stopped"]
    assert [command[2] for command in commands] == ["deployment/saved-decode", "deployment/saved-helper"]


@pytest.mark.asyncio
async def test_published_readiness_retries_transient_missing_deployment(monkeypatch, tmp_path):
    """The delegate lifecycle needs the same propagation-race protection."""
    adapter, _ = _adapter(tmp_path)
    responses = iter(
        [
            (1, "", 'Error from server (NotFound): deployments.apps "saved-decode" not found'),
            (0, "deployment successfully rolled out", ""),
        ]
    )

    async def runner(_command):
        return next(responses)

    async def smoke_test(_namespace, _endpoint):
        return None

    runner.endpoint_smoke_test = smoke_test
    adapter._command_runner = runner
    sleep = AsyncMock()
    monkeypatch.setattr("llm_d_bench.deploy.providers.optimized_baseline.asyncio.sleep", sleep)
    artifact = GuideDeploymentArtifact(
        "optimized-baseline",
        "hash",
        deployment_contract={
            "readinessDeployments": ["saved-decode"],
            "endpoint": {"protocol": "http", "serviceName": "saved-modelserver", "port": 80},
        },
    )

    result = await adapter.readiness({"namespace": "test-run", "_artifact": artifact})

    assert result.accepted, result.reasons
    sleep.assert_awaited_once_with(1)


_MODEL_PATCH = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: model
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: modelserver
          args:
            - "Qwen/Qwen3-0.6B"
          env:
          volumeMounts:
            - name: unrelated
              mountPath: /tmp
      volumes:
        - name: unrelated
          emptyDir: {}
"""

_MODEL_CLAIM = """\
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: model-claim
spec:
  spec:
    devices:
      requests:
        - exactly:
            deviceClassName: gpu.intel.com
            count: 1
"""


def _write_model_overlay(overlay: Path) -> None:
    (overlay / "patch-vllm.yaml").write_text(_MODEL_PATCH, encoding="utf-8")
    (overlay / "resource-claim-template.yaml").write_text(_MODEL_CLAIM, encoding="utf-8")
    (overlay / "kustomization.yaml").write_text("resources:\n  - patch-vllm.yaml\n", encoding="utf-8")


def _deployment_parameters() -> dict[str, int | None]:
    return {
        "replicas": 1,
        "tensor_parallel_size": 1,
        "accelerator_count": 1,
        "max_num_seqs": None,
        "max_model_len": None,
    }


def _runtime(**overrides) -> dict:
    runtime = {
        "image": "example/vllm:1",
        "resolved_mount": None,
        "environment": {},
        "mount_host_path": "",
        "model_source": "huggingface",
        "mounted_model_path": "",
        "configuration_source_document": None,
    }
    runtime.update(overrides)
    return runtime


def _rendered_container(adapter, overlay: Path, runtime: dict) -> tuple[dict, dict[str, str | None]]:
    _write_model_overlay(overlay)
    path = adapter._render_overlay(_deployment_parameters(), "Qwen/Qwen3-0.6B", runtime)
    patch = yaml.safe_load((path / "patch-vllm.yaml").read_text(encoding="utf-8"))
    container = patch["spec"]["template"]["spec"]["containers"][0]
    entries = container.get("env") or []
    return container, {item["name"]: item.get("value") for item in entries}


def test_auto_cache_serving_pod_runs_huggingface_offline(tmp_path):
    adapter, overlay = _adapter(tmp_path)
    _, environment = _rendered_container(
        adapter,
        overlay,
        _runtime(
            model_source="auto-cache",
            resolved_mount={
                "volume_source": {"persistentVolumeClaim": {"claimName": "cache"}},
                "mount_path": "/model-cache",
                "read_only": False,
                "model_source": "auto-cache",
            },
        ),
    )
    assert environment["HF_HOME"] == "/model-cache"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"


def test_huggingface_source_serving_pod_stays_online(tmp_path):
    adapter, overlay = _adapter(tmp_path)
    _, environment = _rendered_container(adapter, overlay, _runtime())
    assert "HF_HUB_OFFLINE" not in environment
    assert "TRANSFORMERS_OFFLINE" not in environment


def test_read_only_volume_resolves_shared_path_and_drops_stale_hf_home(tmp_path):
    adapter, overlay = _adapter(tmp_path)
    container, environment = _rendered_container(
        adapter,
        overlay,
        _runtime(
            model_source="auto-cache",
            resolved_mount={
                "volume_source": {"persistentVolumeClaim": {"claimName": "cache"}},
                "mount_path": "/model-cache",
                "read_only": True,
                "model_source": "shared-path",
            },
        ),
    )
    # A read-only volume resolves to shared-path, so vLLM is handed the local
    # directory rather than a repo id: HF_HOME must not linger from the requested
    # auto-cache source, but the mounted cache still runs offline.
    assert container["args"][0] == "/model-cache"
    assert "HF_HOME" not in environment
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"


def test_input_validation_allows_waiting_for_accelerators(tmp_path):
    adapter, _ = _adapter(tmp_path)
    result = adapter.validate_inputs(
        adapter._definition,
        {"accelerators": 1},
        {
            "model": {"name": "Qwen/Qwen3-0.6B"},
            "decode": {"replicaCount": 4, "tensorParallelSize": 1},
            "runtime": {"image": "example/vllm:xpu", "imageMode": "use-upstream-image"},
        },
    )
    assert result.accepted, result.reasons


def test_gateway_mode_proxy_disable_skipped_for_evaluation_owned_deployments(tmp_path):
    from llm_d_bench.deploy.data_plane import GATEWAY_MODE_VALUES_PATH, PLAINTEXT_EPP_VALUES_PATH

    adapter, _ = _adapter(tmp_path)
    plaintext = ["--values", str(PLAINTEXT_EPP_VALUES_PATH)]
    shared = [*plaintext, "--values", str(GATEWAY_MODE_VALUES_PATH)]

    assert adapter._gateway_mode_args({}) == shared
    # Evaluation-owned deployments keep their own proxy, but the EPP is still
    # forced plaintext so the proxy's ext_proc stays consistent.
    assert adapter._gateway_mode_args({"provenance": {"evaluate_workflow": True}}) == plaintext
    assert adapter._gateway_mode_args({"provenance": {"evaluation_id": "eval-1"}}) == plaintext
    assert adapter._gateway_mode_args({"provenance": {"evaluation_case_id": "case-1"}}) == plaintext
