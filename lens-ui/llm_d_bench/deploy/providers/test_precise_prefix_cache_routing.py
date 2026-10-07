"""Tests for the structured precise-prefix-cache-routing deployment adapter."""

import asyncio
import hashlib
from pathlib import Path

import pytest
import yaml

from llm_d_bench.deploy.providers.guide_adapter import GuideDeploymentArtifact
from llm_d_bench.deploy.providers.precise_prefix_cache_routing import (
    UPSTREAM_SAMPLE_PEAK_PREFILL_THROUGHPUT,
    PrecisePrefixCacheRoutingAdapter,
    _EPP_ENDPOINT_SETTLE_SECONDS,
)


def _valid_overrides(**extra):
    overrides = {
        "model": {"name": "Qwen/Qwen3-30B-A3B"},
        "runtime": {"image": "ghcr.io/llm-d/llm-d-xpu:v0.9.0"},
        "decode": {"replicaCount": 8, "tensorParallelSize": 4},
    }
    overrides.update(extra)
    return overrides


def test_parameters_requires_model_image_and_decode_topology():
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)

    with pytest.raises(ValueError, match="model.name"):
        adapter._parameters(_valid_overrides(model={}))
    with pytest.raises(ValueError, match="tagged runtime.image"):
        adapter._parameters(_valid_overrides(runtime={"image": "untagged"}))
    with pytest.raises(ValueError, match="replicaCount and tensorParallelSize"):
        adapter._parameters(_valid_overrides(decode={"replicaCount": 0, "tensorParallelSize": 4}))


def test_parameters_validates_optional_router_overrides():
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)

    with pytest.raises(ValueError, match="peakPrefillThroughput must be a positive number"):
        adapter._parameters(_valid_overrides(router={"peakPrefillThroughput": -1}))
    with pytest.raises(ValueError, match="valuesOverride must be a YAML string"):
        adapter._parameters(_valid_overrides(router={"valuesOverride": 123}))
    with pytest.raises(ValueError, match="mountPath must be an absolute host path"):
        adapter._parameters(_valid_overrides(runtime={"image": "img:tag", "mountPath": "relative/path"}))


def test_parameters_defaults_router_fields_to_none():
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)

    parameters = adapter._parameters(_valid_overrides())

    assert parameters["peak_prefill_throughput"] is None
    assert parameters["router_values_override"] is None
    assert parameters["max_model_len"] is None
    assert parameters["gpu_memory_utilization"] is None


def test_decode_deployment_follows_contract_and_manifest(tmp_path: Path):
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    contract_artifact = GuideDeploymentArtifact(
        "precise-prefix-cache-routing",
        "hash",
        deployment_contract={"readinessDeployments": ["precise-prefix-cache-routing-gpu-vllm-decode"]},
    )
    assert adapter._decode_deployment(contract_artifact) == "precise-prefix-cache-routing-gpu-vllm-decode"

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "kind: Deployment\nmetadata:\n  name: precise-prefix-cache-routing-gpu-vllm-decode\n", encoding="utf-8"
    )
    manifest_artifact = GuideDeploymentArtifact(
        "precise-prefix-cache-routing", "hash", manifest_ref=str(manifest), deployment_contract={}
    )
    assert adapter._decode_deployment(manifest_artifact) == "precise-prefix-cache-routing-gpu-vllm-decode"


def test_parse_peak_prefill_throughput_reads_either_format():
    from llm_d_bench.deploy.providers.precise_prefix_cache_routing import _parse_peak_prefill_throughput

    assert _parse_peak_prefill_throughput("  Measured peakPrefillThroughput = 12345 tokens/sec\n") == 12345
    assert _parse_peak_prefill_throughput("PEAK_PREFILL_THROUGHPUT=99\n") == 99
    assert _parse_peak_prefill_throughput("nothing here") is None
    assert _parse_peak_prefill_throughput("PEAK_PREFILL_THROUGHPUT=0") is None


def test_modelserver_overlay_follows_the_active_accelerator(tmp_path: Path):
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._overlay_variant_value = "gpu"
    assert adapter._modelserver_overlay(tmp_path) == (
        tmp_path / "guides/precise-prefix-cache-routing/modelserver/gpu/vllm/base"
    )
    adapter._overlay_variant_value = "xpu"
    assert adapter._modelserver_overlay(tmp_path) == (
        tmp_path / "guides/precise-prefix-cache-routing/modelserver/xpu/vllm"
    )


def _upstream_deployment_documents():
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "precise-prefix-cache-routing-xpu-vllm-decode"},
            "spec": {
                "replicas": 2,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "modelserver",
                                "command": ["vllm", "serve"],
                                "args": [
                                    "Qwen/Qwen3-0.6B",
                                    "--port=$(POD_PORT)",
                                    "--dtype=float16",
                                    "--block-size=64",
                                    '--kv-events-config={"topic":"kv@$(POD_IP):$(POD_PORT)@Qwen/Qwen3-0.6B"}',
                                ],
                                "env": [{"name": "DO_NOT_TRACK", "value": "1"}],
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "resource.k8s.io/v1",
            "kind": "ResourceClaimTemplate",
            "metadata": {"name": "precise-prefix-cache-routing-xpu-vllm-intel-claim-template-decode"},
            "spec": {
                "spec": {
                    "devices": {
                        "requests": [{"name": "intel", "exactly": {"deviceClassName": "gpu.intel.com", "count": 1}}]
                    }
                }
            },
        },
    ]


def test_patch_modelserver_sets_model_replicas_tp_and_claim_count():
    documents = _upstream_deployment_documents()
    parameters = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)._parameters(
        _valid_overrides(
            decode={"replicaCount": 8, "tensorParallelSize": 4, "maxModelLen": 6000, "gpuMemoryUtilization": 0.9}
        )
    )

    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._accelerator = None
    adapter._patch_modelserver(documents, parameters)

    deployment = next(item for item in documents if item["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert deployment["spec"]["replicas"] == 8
    assert container["args"][0] == "Qwen/Qwen3-30B-A3B"
    assert "--tensor-parallel-size=4" in container["args"]
    assert "--max-model-len=6000" in container["args"]
    assert "--gpu-memory-utilization=0.9" in container["args"]
    kv_events_argument = next(item for item in container["args"] if item.startswith("--kv-events-config="))
    kv_events = yaml.safe_load(kv_events_argument.removeprefix("--kv-events-config="))
    assert kv_events["topic"] == "kv@$(POD_IP):$(POD_PORT)@Qwen/Qwen3-30B-A3B"
    assert container["image"] == "ghcr.io/llm-d/llm-d-xpu:v0.9.0"
    claim = next(item for item in documents if item["kind"] == "ResourceClaimTemplate")
    assert claim["spec"]["spec"]["devices"]["requests"][0]["exactly"]["count"] == 4


def test_patch_modelserver_applies_custom_parameters_and_mount_path():
    documents = _upstream_deployment_documents()
    parameters = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)._parameters(
        _valid_overrides(
            runtime={
                "image": "img:tag",
                "mountPath": "/mnt/data/huggingface-cache",
                "environment": {"HF_HOME": "/models"},
            },
            customParameters=[{"target": "decode", "kind": "argument", "name": "max-num-seqs", "value": "64"}],
        )
    )

    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._accelerator = None
    adapter._patch_modelserver(documents, parameters)

    deployment = next(item for item in documents if item["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["args"][0] == "/model-cache"
    assert "--max-num-seqs=64" in container["args"]
    assert {"name": "HF_HOME", "value": "/models"} in container["env"]
    # A mounted cache serves the local directory, so Hugging Face stays offline.
    assert {"name": "HF_HUB_OFFLINE", "value": "1"} in container["env"]
    assert {"name": "TRANSFORMERS_OFFLINE", "value": "1"} in container["env"]
    pod_spec = deployment["spec"]["template"]["spec"]
    assert {
        "name": "model-cache",
        "hostPath": {"path": "/mnt/data/huggingface-cache", "type": "DirectoryOrCreate"},
    } in pod_spec["volumes"]
    assert {"name": "model-cache", "mountPath": "/model-cache", "readOnly": True} in container["volumeMounts"]


def _write_router_values(path: Path, model_name: str = "Qwen/Qwen3-0.6B") -> None:
    plugins_config = {
        "apiVersion": "llm-d.ai/v1alpha1",
        "kind": "EndpointPickerConfig",
        "plugins": [
            {
                "type": "token-producer",
                "parameters": {
                    "modelName": model_name,
                    "vllm": {"url": "http://precise-prefix-cache-routing-render:8000"},
                },
            },
            {"type": "precise-prefix-cache-producer", "parameters": {"tokenProcessorConfig": {"blockSizeTokens": 64}}},
            {
                "type": "prefix-cache-affinity-filter",
                "parameters": {"peakPrefillThroughput": UPSTREAM_SAMPLE_PEAK_PREFILL_THROUGHPUT},
            },
        ],
    }
    base = {
        "router": {
            "epp": {
                "replicas": 1,
                "pluginsConfigFile": "precise-prefix-cache-routing-plugins.yaml",
                "pluginsCustomConfig": {
                    "precise-prefix-cache-routing-plugins.yaml": yaml.safe_dump(plugins_config, sort_keys=False),
                },
            },
        },
    }
    path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")


def _asset(name: str, content: str) -> dict[str, str]:
    return {"name": name, "content": content, "checksum": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"}


@pytest.mark.asyncio
async def test_deploy_uses_saved_precise_resources_values_and_calibration_when_sources_are_missing(tmp_path: Path):
    runner = _ReadinessRunner()
    adapter = PrecisePrefixCacheRoutingAdapter(
        runner,
        tmp_path / "removed-guide",
        "llmd-",
        30,
        Path("helm"),
        None,
    )
    bundle_commands = []

    async def bundle_runner(command):
        bundle_commands.append(command)
        if command[1:3] == ["get", "manifest"]:
            return 0, "kind: Deployment\nmetadata:\n  name: installed-router\n", ""
        return 0, "kind: Deployment\n", ""

    adapter._bundle_command_runner = bundle_runner
    values_path = tmp_path / "effective.yaml"
    _write_router_values(values_path, "Qwen/Qwen3-0.6B")
    values = values_path.read_text(encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump_all(_upstream_deployment_documents()), encoding="utf-8")
    calibration = "#!/bin/bash\necho PEAK_PREFILL_THROUGHPUT=123\n"
    bundle = {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "precise-prefix-cache-routing",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "saved-chart",
            "version": "saved-version",
            "releaseName": "precise-prefix-cache-routing",
            "values": [_asset("router-effective.yaml", values)],
        },
        "resources": [
            _asset("render.yaml", "kind: Service\nmetadata:\n  name: precise-prefix-cache-routing-render\n"),
            _asset("baseline.yaml", "kind: Service\nmetadata:\n  name: precise-prefix-cache-routing-baseline\n"),
        ],
        "calibration": [
            _asset("calibrate.sh", calibration),
            _asset("calibration-peak-throughput.yaml", "kind: Job\nmetadata:\n  name: calibrate\n"),
        ],
    }
    artifact = GuideDeploymentArtifact(
        "precise-prefix-cache-routing",
        "hash",
        manifest_ref=str(manifest),
        source_ref="a" * 40,
        deployment_contract={"deploymentBundle": bundle},
    )

    execution = await adapter.deploy(artifact, {"namespace": "llmd-run"})

    applied = [command for command in bundle_commands if command[:2] == ["kubectl", "apply"]]
    assert [Path(command[-1]).name for command in applied] == ["render.yaml", "baseline.yaml"]
    assert not any("removed-guide" in " ".join(command) for command in bundle_commands + runner.commands)
    assert Path(execution["calibration_script_path"]).read_text(encoding="utf-8") == calibration
    assert execution["router_rendered_manifest"].endswith("name: installed-router\n")


@pytest.mark.asyncio
async def test_calibrated_upgrade_patches_saved_effective_values_and_captures_installed_manifest(tmp_path: Path):
    values_path = tmp_path / "router-effective.yaml"
    _write_router_values(values_path, "Qwen/Saved")
    values = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    values["customSetting"] = {"preserve": True}
    values_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    commands = []

    async def runner(command):
        commands.append(command)
        if command[1:3] == ["get", "manifest"]:
            return 0, "kind: Deployment\nmetadata:\n  name: calibrated-router\n", ""
        return 0, "ok", ""

    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._bundle_command_runner = runner
    execution = {
        "namespace": "llmd-run",
        "model": "Qwen/Saved",
        "router_values_path": str(values_path),
        "router_chart": "saved-chart",
        "router_version": "saved-version",
        "router_release_name": "precise-prefix-cache-routing",
    }

    await adapter._apply_calibrated_router_values(execution, 23456)

    assert Path(execution["router_values_path"]).name == "router-values-calibrated-llmd-run.yaml"
    calibrated = yaml.safe_load(execution["router_effective_values"])
    assert calibrated["customSetting"] == {"preserve": True}
    plugins_file = calibrated["router"]["epp"]["pluginsConfigFile"]
    plugins = yaml.safe_load(calibrated["router"]["epp"]["pluginsCustomConfig"][plugins_file])
    affinity = next(item for item in plugins["plugins"] if item["type"] == "prefix-cache-affinity-filter")
    assert affinity["parameters"]["peakPrefillThroughput"] == 23456
    assert commands[-1] == ["helm", "get", "manifest", "precise-prefix-cache-routing", "--namespace", "llmd-run"]
    assert execution["router_rendered_manifest"].endswith("name: calibrated-router\n")
    # The post-calibration re-render must keep the plaintext EPP data-plane
    # values, or the deployment's proxy reverts to TLS and 503s.
    from llm_d_bench.deploy.data_plane import PLAINTEXT_EPP_VALUES_PATH

    template_command = next(command for command in commands if "template" in command)
    assert str(PLAINTEXT_EPP_VALUES_PATH) in template_command


def test_patched_router_values_updates_model_name_and_peak_prefill_throughput(tmp_path: Path):
    router_values = tmp_path / "router.values.yaml"
    _write_router_values(router_values)
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._router_values = router_values

    patched = adapter._patched_router_values("Qwen/Qwen3-30B-A3B", 12345)

    base = yaml.safe_load(patched)
    plugins_config = yaml.safe_load(
        base["router"]["epp"]["pluginsCustomConfig"]["precise-prefix-cache-routing-plugins.yaml"]
    )
    token_producer = next(item for item in plugins_config["plugins"] if item["type"] == "token-producer")
    affinity_filter = next(item for item in plugins_config["plugins"] if item["type"] == "prefix-cache-affinity-filter")
    assert token_producer["parameters"]["modelName"] == "Qwen/Qwen3-30B-A3B"
    assert affinity_filter["parameters"]["peakPrefillThroughput"] == 12345
    # Unrelated plugin config (block size, render URL) survives the round-trip untouched.
    producer = next(item for item in plugins_config["plugins"] if item["type"] == "precise-prefix-cache-producer")
    assert producer["parameters"]["tokenProcessorConfig"]["blockSizeTokens"] == 64
    assert token_producer["parameters"]["vllm"]["url"] == "http://precise-prefix-cache-routing-render:8000"


def test_patched_router_values_leaves_peak_prefill_throughput_when_not_overridden(tmp_path: Path):
    router_values = tmp_path / "router.values.yaml"
    _write_router_values(router_values)
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._router_values = router_values

    patched = adapter._patched_router_values("Qwen/Qwen3-32B", None)

    base = yaml.safe_load(patched)
    plugins_config = yaml.safe_load(
        base["router"]["epp"]["pluginsCustomConfig"]["precise-prefix-cache-routing-plugins.yaml"]
    )
    affinity_filter = next(item for item in plugins_config["plugins"] if item["type"] == "prefix-cache-affinity-filter")
    assert affinity_filter["parameters"]["peakPrefillThroughput"] == UPSTREAM_SAMPLE_PEAK_PREFILL_THROUGHPUT


def test_extract_model_name_reads_first_arg_of_modelserver_container():
    manifest = yaml.safe_dump_all(_upstream_deployment_documents())

    model = PrecisePrefixCacheRoutingAdapter._extract_model_name(manifest)

    assert model == "Qwen/Qwen3-0.6B"


def test_extract_model_name_uses_kv_topic_when_model_is_mounted_path():
    documents = _upstream_deployment_documents()
    container = documents[0]["spec"]["template"]["spec"]["containers"][0]
    container["args"][0] = "/model-cache"

    model = PrecisePrefixCacheRoutingAdapter._extract_model_name(yaml.safe_dump_all(documents))

    assert model == "Qwen/Qwen3-0.6B"


def test_extract_model_name_returns_none_when_no_deployment_present():
    manifest = yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "foo"}})

    assert PrecisePrefixCacheRoutingAdapter._extract_model_name(manifest) is None


def test_extract_calibration_chunk_size_uses_vllm_max_num_batched_tokens():
    documents = _upstream_deployment_documents()
    documents[0]["spec"]["template"]["spec"]["containers"][0]["args"].append("--max-num-batched-tokens=4096")

    chunk_size = PrecisePrefixCacheRoutingAdapter._extract_calibration_chunk_size(yaml.safe_dump_all(documents))

    assert chunk_size == 4096


def test_extract_calibration_chunk_size_defaults_to_upstream_recipe_value():
    assert (
        PrecisePrefixCacheRoutingAdapter._extract_calibration_chunk_size(
            yaml.safe_dump_all(_upstream_deployment_documents())
        )
        == 8192
    )


def test_extract_calibration_chunk_size_leaves_one_token_below_max_model_len():
    documents = _upstream_deployment_documents()
    documents[0]["spec"]["template"]["spec"]["containers"][0]["args"].append("--max-model-len=6000")

    assert PrecisePrefixCacheRoutingAdapter._extract_calibration_chunk_size(yaml.safe_dump_all(documents)) == 5999


def test_extractors_parse_bash_c_vllm_invocation():
    documents = _upstream_deployment_documents()
    container = documents[0]["spec"]["template"]["spec"]["containers"][0]
    container["command"] = ["/bin/bash", "-c"]
    container["args"] = [
        ("exec vllm serve Qwen/Qwen3-8B " + "\\\n  --max-model-len=4096 " + "\\\n  --max-num-batched-tokens 2048")
    ]
    manifest = yaml.safe_dump_all(documents)

    assert PrecisePrefixCacheRoutingAdapter._extract_model_name(manifest) == "Qwen/Qwen3-8B"
    assert PrecisePrefixCacheRoutingAdapter._extract_calibration_chunk_size(manifest) == 2048


def test_resolve_router_values_prefers_override_then_patched_then_derived_then_static(tmp_path: Path):
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    static_router_values = tmp_path / "static-router-values.yaml"
    _write_router_values(static_router_values)
    adapter._router_values = static_router_values

    directory = tmp_path / "artifact"
    directory.mkdir()
    manifest_path = directory / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump_all(_upstream_deployment_documents()), encoding="utf-8")

    class _Artifact:
        manifest_ref = str(manifest_path)

    # No override or patched file yet -> derives modelName from the manifest and writes it.
    resolved = adapter._resolve_router_values(_Artifact())
    assert resolved == directory / "router-values-patched.yaml"
    derived = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    plugins_config = yaml.safe_load(
        derived["router"]["epp"]["pluginsCustomConfig"]["precise-prefix-cache-routing-plugins.yaml"]
    )
    token_producer = next(item for item in plugins_config["plugins"] if item["type"] == "token-producer")
    assert token_producer["parameters"]["modelName"] == "Qwen/Qwen3-0.6B"

    # An override file, once present, always wins.
    (directory / "router-values-override.yaml").write_text("custom: true\n", encoding="utf-8")
    assert adapter._resolve_router_values(_Artifact()) == directory / "router-values-override.yaml"


class _ReadinessRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    async def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.commands.append(command)
        return 0, "deployment successfully rolled out", ""


@pytest.mark.asyncio
async def test_readiness_exposes_routed_and_baseline_endpoints(tmp_path: Path, monkeypatch):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    runner = _ReadinessRunner()
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._runner = runner
    adapter._timeout = 30
    adapter._bundle_calibration_scripts = {}
    calibration_script = tmp_path / "calibrate.sh"
    calibration_script.write_text("echo saved\n", encoding="utf-8")

    async def calibrate(_namespace, _model, _chunk_size):
        assert adapter._bundle_calibration_scripts[_namespace] == calibration_script
        return 12345

    async def apply_calibrated(execution, measured):
        execution["calibration_output"] = f"PEAK_PREFILL_THROUGHPUT={measured}"

    adapter._calibrate_peak_prefill_throughput = calibrate
    adapter._apply_calibrated_router_values = apply_calibrated
    execution = {
        "namespace": "llmd-precise-prefix-cache-routing-run",
        "model": "Qwen/Qwen3-30B-A3B",
        "calibration_chunk_size": 8192,
        "calibration_script_path": str(calibration_script),
    }

    result = await adapter.readiness(execution)

    assert result.accepted
    assert (
        execution["endpoint_url"]
        == "http://precise-prefix-cache-routing-epp.llmd-precise-prefix-cache-routing-run.svc:80"
    )
    assert execution["baseline_endpoint_url"] == (
        "http://precise-prefix-cache-routing-baseline.llmd-precise-prefix-cache-routing-run.svc:8000"
    )
    assert execution["calibrated_peak_prefill_throughput"] == 12345
    assert runner.commands[-1] == [
        "kubectl",
        "rollout",
        "status",
        "deployment/precise-prefix-cache-routing-epp",
        "--namespace",
        "llmd-precise-prefix-cache-routing-run",
        "--timeout=30s",
    ]
    assert sleep_calls == [_EPP_ENDPOINT_SETTLE_SECONDS]


@pytest.mark.asyncio
async def test_diagnostics_exposes_state_needed_to_restore_calibration():
    runner = _ReadinessRunner()
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._runner = runner
    execution = {
        "namespace": "llmd-run",
        "model": "Qwen/Saved",
        "calibration_chunk_size": 2048,
        "calibration_script_path": "/saved/calibrate.sh",
        "router_values_path": "/saved/effective.yaml",
        "router_chart": "saved-chart",
        "router_version": "v1",
        "router_release_name": "precise",
    }

    diagnostics = await adapter.diagnostics(execution)

    for key, value in execution.items():
        assert diagnostics[key] == value


@pytest.mark.asyncio
async def test_readiness_reuses_successful_calibration(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    runner = _ReadinessRunner()
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._runner = runner
    adapter._timeout = 30

    async def calibrate(_namespace, _model, _chunk_size):
        raise AssertionError("readiness must not repeat a successful calibration")

    async def apply_calibrated(_execution, _measured):
        raise AssertionError("readiness must not reapply calibrated router values")

    adapter._calibrate_peak_prefill_throughput = calibrate
    adapter._apply_calibrated_router_values = apply_calibrated
    execution = {
        "namespace": "llmd-precise-prefix-cache-routing-run",
        "model": "Qwen/Qwen3-30B-A3B",
        "calibration_chunk_size": 8192,
        "calibrated_peak_prefill_throughput": 12345,
    }

    result = await adapter.readiness(execution)

    assert result.accepted
    assert execution["calibrated_peak_prefill_throughput"] == 12345
    assert len(runner.commands) == 2


@pytest.mark.asyncio
async def test_readiness_fails_when_live_calibration_fails():
    runner = _ReadinessRunner()
    adapter = PrecisePrefixCacheRoutingAdapter.__new__(PrecisePrefixCacheRoutingAdapter)
    adapter._runner = runner
    adapter._timeout = 30

    async def calibrate(_namespace, _model, _chunk_size):
        raise RuntimeError("measurement failed")

    adapter._calibrate_peak_prefill_throughput = calibrate
    execution = {
        "namespace": "llmd-precise-prefix-cache-routing-run",
        "model": "Qwen/Qwen3-30B-A3B",
        "calibration_chunk_size": 8192,
    }

    result = await adapter.readiness(execution)

    assert not result.accepted
    assert result.reasons == ["peakPrefillThroughput calibration failed: measurement failed"]
    assert "endpoint_url" not in execution
