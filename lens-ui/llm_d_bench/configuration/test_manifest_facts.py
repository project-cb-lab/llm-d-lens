from types import SimpleNamespace

import pytest
import yaml

from llm_d_bench.configuration import service
from llm_d_bench.configuration.manifest_facts import validate_manifest_facts
from llm_d_bench.configuration.models import SaveRequest
from llm_d_bench.utils.artifacts import (
    configuration_checksum,
    text_checksum,
    validate_configuration_manifest_content,
)


def _deployment(
    role: str = "decode",
    *,
    replicas: int = 2,
    image: str = "registry.example/vllm:v1",
    command: list[str] | None = None,
    args: list[str] | None = None,
    environment: list[dict] | None = None,
    volumes: list[dict] | None = None,
    mounts: list[dict] | None = None,
) -> dict:
    container = {
        "name": "modelserver",
        "image": image,
        "command": command if command is not None else ["vllm", "serve"],
        "args": args
        if args is not None
        else [
            "example/model",
            "--tensor-parallel-size=2",
            "--max-model-len=8192",
            "--max-num-seqs=32",
            "--block-size=64",
        ],
        "env": environment
        if environment is not None
        else [
            {"name": "GLOBAL_SETTING", "value": "on"},
            {"name": "ROLE_SETTING", "value": "decode"},
        ],
    }
    if mounts is not None:
        container["volumeMounts"] = mounts
    pod_spec = {"containers": [container]}
    if volumes is not None:
        pod_spec["volumes"] = volumes
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": f"guide-{role}", "labels": {"llm-d.ai/role": role}},
        "spec": {"replicas": replicas, "template": {"spec": pod_spec}},
    }


def _manifest(*deployments: dict) -> str:
    return yaml.safe_dump_all(
        [
            *deployments,
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "modelserver"},
                "spec": {"ports": [{"port": 8000}]},
            },
        ]
    )


def _content(manifest: str) -> dict:
    return {
        "deploymentType": "baseline",
        "model": {"name": "example/model", "maxModelLen": 8192},
        "serving": {"replicaCount": 2, "tensorParallelSize": 2, "maxModelLen": 8192},
        # The current publisher retains this decode projection for baseline Guides.
        "decode": {
            "replicaCount": 2,
            "tensorParallelSize": 2,
            "maxModelLen": 8192,
            "maxNumSeqs": 32,
        },
        "runtime": {
            "image": "registry.example/vllm:v1",
            "imageMode": "use-upstream-image",
            "modelServer": "vllm",
            "modelSource": "huggingface",
            "environment": {"GLOBAL_SETTING": "on"},
        },
        "customParameters": [
            {"target": "both", "kind": "argument", "name": "block-size", "value": "64"},
            {"target": "decode", "kind": "environment", "name": "ROLE_SETTING", "value": "decode"},
        ],
        "officialGuide": {
            "source": {"guide": "optimized-baseline"},
            "renderedManifest": manifest,
            "manifestChecksum": text_checksum(manifest),
            "deployment": {
                "readinessDeployments": ["guide-decode"],
                "endpoint": {"protocol": "http", "serviceName": "modelserver", "port": 8000},
            },
        },
    }


def _model_only_content(guide: str = "optimized-baseline") -> dict:
    return {
        "model": {"name": "example/model"},
        "officialGuide": {"source": {"guide": guide}},
    }


def test_matching_argv_manifest_proves_all_explicit_configuration_facts():
    manifest = _manifest(_deployment())
    validate_manifest_facts(_content(manifest), manifest)


def test_manifest_rejects_administrator_managed_hugging_face_environment():
    manifest = _manifest(_deployment())
    content = _content(manifest)
    content["customParameters"].append(
        {"target": "decode", "kind": "environment", "name": "HF_HUB_OFFLINE", "value": "0"}
    )
    with pytest.raises(ValueError, match="administrator-managed Hugging Face"):
        validate_manifest_facts(content, manifest)


def test_requested_two_replicas_rejects_one_rendered_replica():
    manifest = _manifest(_deployment(replicas=1))

    with pytest.raises(ValueError, match="replica count is 1.*requires 2"):
        validate_manifest_facts(_content(manifest), manifest)


def test_missing_tensor_parallel_flag_is_vllm_default_one():
    deployment = _deployment(
        args=[
            "example/model",
            "--max-model-len=8192",
            "--max-num-seqs=32",
            "--block-size=64",
        ]
    )
    manifest = _manifest(deployment)
    content = _content(manifest)
    content["serving"]["tensorParallelSize"] = 1
    content["decode"]["tensorParallelSize"] = 1

    validate_manifest_facts(content, manifest)

    content["serving"]["tensorParallelSize"] = 2
    content["decode"]["tensorParallelSize"] = 2
    with pytest.raises(ValueError, match="tensor parallel size is 1.*requires 2"):
        validate_manifest_facts(content, manifest)


@pytest.mark.parametrize(
    ("field", "mutate", "message"),
    [
        ("replicas", lambda content: content["decode"].update(replicaCount=3), "replica"),
        ("tensor parallelism", lambda content: content["decode"].update(tensorParallelSize=4), "tensor"),
        ("image", lambda content: content["runtime"].update(image="registry.example/vllm:v2"), "image"),
        (
            "custom argument",
            lambda content: content["customParameters"][0].update(value="128"),
            "block-size",
        ),
        (
            "custom environment",
            lambda content: content["customParameters"][1].update(value="prefill"),
            "ROLE_SETTING",
        ),
    ],
)
def test_manifest_rejects_mismatched_explicit_fact(field, mutate, message):
    manifest = _manifest(_deployment())
    content = _content(manifest)
    mutate(content)

    with pytest.raises(ValueError, match=message):
        validate_manifest_facts(content, manifest)


def test_matching_pd_shell_manifest_proves_role_specific_facts():
    prefill = _deployment(
        "prefill",
        replicas=1,
        command=["/bin/bash", "-c"],
        args=[
            "exec vllm serve example/model \\\n              --tensor-parallel-size=4 "
            "--max-num-seqs=16 --role-setting=prefill"
        ],
        environment=[],
    )
    decode = _deployment(
        "decode",
        replicas=2,
        command=["sh", "-c"],
        args=["vllm serve example/model --tensor-parallel-size=2 --max-num-seqs=32"],
        environment=[],
    )
    manifest = _manifest(prefill, decode)
    content = {
        "deploymentType": "pd",
        "model": {"name": "example/model"},
        "prefill": {"replicaCount": 1, "tensorParallelSize": 4},
        "decode": {"replicaCount": 2, "tensorParallelSize": 2},
        "customParameters": [
            {"target": "prefill", "kind": "argument", "name": "max-num-seqs", "value": "16"},
            {"target": "prefill", "kind": "argument", "name": "role-setting", "value": "prefill"},
            {"target": "decode", "kind": "argument", "name": "max-num-seqs", "value": "32"},
        ],
    }

    validate_manifest_facts(content, manifest)


def test_compound_shell_program_is_not_accepted_as_configuration_evidence():
    deployment = _deployment(
        command=["bash", "-c"],
        args=["prepare-model && exec vllm serve example/model --tensor-parallel-size=2"],
    )

    with pytest.raises(ValueError, match="compound shell"):
        validate_manifest_facts(_content(_manifest(deployment)), _manifest(deployment))


@pytest.mark.parametrize(
    "script",
    [
        "exec vllm serve example/model\nexec echo unexpected",
        "exec vllm serve example/model # ignored shell text",
        "exec vllm serve example/model >> /tmp/modelserver.log",
    ],
)
def test_shell_newlines_comments_and_redirects_are_not_accepted_as_evidence(script):
    deployment = _deployment(replicas=1, command=["bash", "-c"], args=[script])

    with pytest.raises(ValueError, match="shell invocation"):
        validate_manifest_facts(_model_only_content(), _manifest(deployment))


def test_simple_bash_lc_vllm_invocation_is_supported():
    deployment = _deployment(
        replicas=1,
        command=["/bin/bash", "-lc"],
        args=["exec vllm serve example/model"],
    )

    validate_manifest_facts(_model_only_content(), _manifest(deployment))


def test_shell_block_scalar_allows_leading_and_trailing_newlines():
    deployment = _deployment(
        replicas=1,
        command=["/bin/bash", "-c"],
        args=["\n  exec vllm serve example/model\n"],
    )

    validate_manifest_facts(_model_only_content(), _manifest(deployment))


def test_non_vllm_wrapper_cannot_hide_vllm_tokens_in_its_arguments():
    deployment = _deployment(
        replicas=1,
        command=["python", "wrapper.py"],
        args=["vllm", "serve", "example/model"],
    )

    with pytest.raises(ValueError, match="recognizable vllm serve"):
        validate_manifest_facts(_model_only_content(), _manifest(deployment))


@pytest.mark.parametrize(
    "topic",
    [
        None,
        "oops@host@example/model",
        "example/model",
        "kv@@example/model",
    ],
)
def test_precise_guide_requires_well_formed_kv_event_topic_without_a_bundle(topic):
    args = ["example/model"]
    if topic is not None:
        args.append(f'--kv-events-config={{"topic":"{topic}"}}')
    deployment = _deployment(replicas=1, args=args)

    with pytest.raises(ValueError, match="kv-events topic"):
        validate_manifest_facts(
            _model_only_content("precise-prefix-cache-routing"),
            _manifest(deployment),
        )


def test_precise_guide_accepts_well_formed_matching_kv_event_topic():
    deployment = _deployment(
        replicas=1,
        args=[
            "example/model",
            '--kv-events-config={"topic":"kv@$(POD_IP):$(POD_PORT)@example/model"}',
        ],
    )

    validate_manifest_facts(
        _model_only_content("precise-prefix-cache-routing"),
        _manifest(deployment),
    )


def test_shared_path_uses_container_model_path_and_optional_served_identity():
    deployment = _deployment(
        args=[
            "/model-cache",
            "--tensor-parallel-size=2",
            "--served-model-name=example/model",
            "--max-model-len=8192",
        ],
        volumes=[{"name": "model-cache", "hostPath": {"path": "/srv/models/example"}}],
        mounts=[{"name": "model-cache", "mountPath": "/model-cache", "readOnly": True}],
    )
    manifest = _manifest(deployment)
    content = _content(manifest)
    content["runtime"].update(modelSource="shared-path", mountPath="/srv/models/example")
    content["customParameters"] = []
    content["decode"].pop("maxNumSeqs")

    validate_manifest_facts(content, manifest)


def test_precise_shared_path_requires_logical_served_model_identity():
    deployment = _deployment(
        args=[
            "/model-cache",
            "--tensor-parallel-size=2",
            "--max-model-len=8192",
            "--max-num-seqs=32",
            "--block-size=64",
            '--kv-events-config={"topic":"kv@$(POD_IP):$(POD_PORT)@example/model"}',
        ],
        volumes=[{"name": "model-cache", "hostPath": {"path": "/srv/models/example"}}],
        mounts=[{"name": "model-cache", "mountPath": "/model-cache", "readOnly": True}],
    )
    manifest = _manifest(deployment)
    content = _content(manifest)
    content["runtime"].update(modelSource="shared-path", mountPath="/srv/models/example")

    with pytest.raises(ValueError, match="served-model-name"):
        validate_manifest_facts(content, manifest)


def test_shared_path_rejects_wrong_host_storage():
    deployment = _deployment(
        args=["/model-cache", "--tensor-parallel-size=2", "--max-model-len=8192"],
        volumes=[{"name": "model-cache", "hostPath": {"path": "/srv/models/old"}}],
        mounts=[{"name": "model-cache", "mountPath": "/model-cache", "readOnly": True}],
    )
    manifest = _manifest(deployment)
    content = _content(manifest)
    content["runtime"].update(modelSource="shared-path", mountPath="/srv/models/example")
    content["customParameters"] = []
    content["decode"].pop("maxNumSeqs")

    with pytest.raises(ValueError, match="storage"):
        validate_manifest_facts(content, manifest)


def test_model_cache_storage_id_requires_its_pvc():
    deployment = _deployment(
        volumes=[{"name": "model-cache", "persistentVolumeClaim": {"claimName": "wrong-pvc"}}],
        mounts=[{"name": "model-cache", "mountPath": "/model-cache", "readOnly": False}],
    )
    manifest = _manifest(deployment)
    content = _content(manifest)
    content["runtime"].update(
        modelSource="auto-cache",
        storageVolumeId="storage-abc123",
        modelPvcClaimName="prism-storage-storage-abc123",
        mountPath="/cache-metadata-for-local-volumes",
    )

    with pytest.raises(ValueError, match="storage PVC"):
        validate_manifest_facts(content, manifest)


def test_explicit_pvc_takes_precedence_over_storage_mount_path_metadata():
    deployment = _deployment(
        volumes=[
            {
                "name": "model-cache",
                "persistentVolumeClaim": {"claimName": "prism-storage-storage-abc123"},
            }
        ],
        mounts=[{"name": "model-cache", "mountPath": "/model-cache", "readOnly": False}],
    )
    manifest = _manifest(deployment)
    content = _content(manifest)
    content["runtime"].update(
        modelSource="auto-cache",
        storageVolumeId="storage-abc123",
        modelPvcClaimName="prism-storage-storage-abc123",
        mountPath="/cache-metadata-for-local-volumes",
    )

    validate_manifest_facts(content, manifest)


def test_precise_kv_topic_must_use_configuration_model():
    deployment = _deployment(
        args=[
            "example/model",
            "--tensor-parallel-size=2",
            '--kv-events-config={"topic":"kv@$(POD_IP):$(POD_PORT)@old/model"}',
            "--max-model-len=8192",
            "--max-num-seqs=32",
            "--block-size=64",
        ]
    )
    manifest = _manifest(deployment)

    with pytest.raises(ValueError, match="kv.*topic"):
        validate_manifest_facts(_content(manifest), manifest)


def test_legacy_model_only_artifact_is_validated_without_requiring_new_fields():
    deployment = _deployment(replicas=1, args=["example/model"])
    manifest = _manifest(deployment)

    validate_manifest_facts({"model": {"name": "example/model"}, "officialGuide": {}}, manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda content: content.update(customParameters={"name": "block-size"}),
        lambda content: content["runtime"].update(environment=[]),
    ],
)
def test_malformed_explicit_runtime_facts_are_not_silently_ignored(mutate):
    manifest = _manifest(_deployment())
    content = _content(manifest)
    mutate(content)

    with pytest.raises(ValueError):
        validate_manifest_facts(content, manifest)


def test_save_revalidates_manifest_facts_before_writing(monkeypatch, tmp_path):
    manifest = _manifest(_deployment())
    content = _content(manifest)
    content["model"]["name"] = "different/model"
    monkeypatch.setattr(service, "CONFIGURATION_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(service, "CONFIGURATION_ARTIFACT_DIR", tmp_path / ".artifacts")
    monkeypatch.setattr(
        service,
        "require_active_session",
        lambda _: SimpleNamespace(id="session", server_id="cluster"),
    )
    request = SaveRequest.model_validate(
        {
            "deployable_configuration": {
                "type": "baseline",
                "provider_ref": "optimized-baseline",
                "format": "manifest",
                "content": content,
                "checksum": configuration_checksum(content),
                "provenance": {"cluster_ref": {"id": "cluster", "session_id": "session"}},
            }
        }
    )

    with pytest.raises(ValueError, match="model"):
        service.save_configuration(request)
    assert not list(tmp_path.glob("*.yaml"))


def test_deploy_validation_rechecks_manifest_facts():
    manifest = _manifest(_deployment())
    content = _content(manifest)
    content["decode"]["replicaCount"] = 1

    with pytest.raises(ValueError, match="replica"):
        validate_configuration_manifest_content(content, "optimized-baseline")


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (["--value=one=two"], "one=two"),
        (["--value", "-1"], "-1"),
        (["--value="], ""),
        (["--value=one", "--alias", "one"], "one"),
        (["--unrelated", "--value=one"], "one"),
    ],
)
def test_option_readers_preserve_attached_separate_and_repeated_values(tokens, expected):
    from llm_d_bench.configuration.manifest_facts import _argument_value, _option

    for reader in (_option, _argument_value):
        assert reader(tokens, {"--value", "--alias"}, "worker") == expected


@pytest.mark.parametrize("tokens", [["--value"], ["--value", "--next"]])
def test_option_readers_keep_distinct_missing_value_policies(tokens):
    from llm_d_bench.configuration.manifest_facts import _MISSING, _argument_value, _option

    with pytest.raises(ValueError, match="valueless --value option"):
        _option(tokens, {"--value"}, "worker")
    assert _argument_value(tokens, {"--value"}, "worker") == ""
    assert _option(tokens, {"--absent"}, "worker") is None
    assert _argument_value(tokens, {"--absent"}, "worker") is _MISSING


@pytest.mark.parametrize("reader_name", ["_option", "_argument_value"])
def test_option_readers_reject_conflicting_aliases(reader_name):
    from llm_d_bench.configuration import manifest_facts

    with pytest.raises(ValueError, match="conflicting --alias/--value options"):
        getattr(manifest_facts, reader_name)(["--value=one", "--alias", "two"], {"--value", "--alias"}, "worker")
