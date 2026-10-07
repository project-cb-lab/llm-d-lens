"""Validate published Configuration facts against rendered Kubernetes resources."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any

import yaml

from .managed_environment import admin_managed_environment_names


def _documents(manifest: str) -> list[dict[str, Any]]:
    try:
        return [document for document in yaml.safe_load_all(manifest) if isinstance(document, dict)]
    except yaml.YAMLError as error:
        raise ValueError(f"rendered manifest is invalid YAML: {error}") from error


def _modelserver_containers(documents: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for document in documents:
        if document.get("kind") != "Deployment":
            continue
        containers = (((document.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers")
        if not isinstance(containers, list):
            continue
        for container in containers:
            if isinstance(container, dict) and container.get("name") == "modelserver":
                result.append((document, container))
    return result


def _shell_tokens(script: str, deployment_name: str) -> list[str]:
    script = script.replace("\\\r\n", "").replace("\\\n", "").strip()
    if "\n" in script or "\r" in script:
        raise ValueError(f"model server {deployment_name} uses a compound shell invocation")
    lexer = shlex.shlex(
        script,
        posix=True,
        punctuation_chars=";&|<>()`#",
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as error:
        raise ValueError(f"model server {deployment_name} has an ambiguous shell invocation: {error}") from error
    shell_punctuation = set(";&|<>()`#")
    if any(
        argument == "$" or (argument and all(character in shell_punctuation for character in argument))
        for argument in tokens
    ):
        raise ValueError(f"model server {deployment_name} uses a compound shell invocation")
    if tokens and tokens[0] == "exec":
        tokens = tokens[1:]
    if len(tokens) < 2 or PurePosixPath(tokens[0]).name != "vllm" or tokens[1] != "serve":
        raise ValueError(f"model server {deployment_name} does not use a recognizable vllm serve invocation")
    return tokens[2:]


def _invocation_tokens(container: dict[str, Any], deployment_name: str) -> list[str]:
    command = container.get("command") or []
    args = container.get("args") or []
    if (
        not isinstance(command, list)
        or not isinstance(args, list)
        or any(not isinstance(token, str) for token in [*command, *args])
    ):
        raise ValueError(f"model server {deployment_name} has an ambiguous command or args field")
    combined = [*command, *args]
    if command and PurePosixPath(command[0]).name in {"sh", "bash"}:
        if len(combined) != 3 or combined[1] not in {"-c", "-lc"}:
            raise ValueError(f"model server {deployment_name} has an ambiguous shell invocation")
        return _shell_tokens(combined[2], deployment_name)

    if command:
        if PurePosixPath(command[0]).name != "vllm":
            raise ValueError(f"model server {deployment_name} does not use a recognizable vllm serve invocation")
        if len(command) >= 2 and command[1] == "serve":
            return [*command[2:], *args]
        if len(command) == 1 and args and args[0] == "serve":
            return list(args[1:])
        raise ValueError(f"model server {deployment_name} does not use a recognizable vllm serve invocation")
    if len(args) >= 2 and PurePosixPath(args[0]).name == "vllm" and args[1] == "serve":
        return list(args[2:])
    if args:
        return list(args)
    raise ValueError(f"model server {deployment_name} does not use a recognizable vllm serve invocation")


def _option_values(tokens: list[str], names: set[str]) -> Iterator[tuple[str, str | None]]:
    """Visit matching options without deciding how callers treat missing values."""
    for index, token in enumerate(tokens):
        name, separator, attached = token.partition("=")
        if name not in names:
            continue
        if separator:
            yield name, attached
        elif index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
            yield name, tokens[index + 1]
        else:
            yield name, None


def _option(tokens: list[str], names: set[str], deployment_name: str) -> str | None:
    values: list[str] = []
    for name, value in _option_values(tokens, names):
        if value is None:
            raise ValueError(f"model server {deployment_name} has a valueless {name} option")
        values.append(value)
    if len(set(values)) > 1:
        raise ValueError(f"model server {deployment_name} has conflicting {'/'.join(sorted(names))} options")
    return values[-1] if values else None


_MISSING = object()


def _argument_value(tokens: list[str], names: set[str], deployment_name: str) -> str | object:
    values = [value if value is not None else "" for _name, value in _option_values(tokens, names)]
    if len(set(values)) > 1:
        raise ValueError(f"model server {deployment_name} has conflicting {'/'.join(sorted(names))} options")
    return values[-1] if values else _MISSING


def _model_argument(tokens: list[str], deployment_name: str) -> str:
    flagged = _option(tokens, {"--model", "--model-path"}, deployment_name)
    positional = next((token for token in tokens if not token.startswith("-")), None)
    if flagged and positional and flagged != positional:
        raise ValueError(f"model server {deployment_name} has conflicting model arguments")
    model = flagged or positional
    if not model:
        raise ValueError(f"model server {deployment_name} does not declare a served model")
    return model


def _role(deployment: dict[str, Any]) -> str:
    metadata = deployment.get("metadata") or {}
    template_metadata = ((deployment.get("spec") or {}).get("template") or {}).get("metadata") or {}
    labels = {**(template_metadata.get("labels") or {}), **(metadata.get("labels") or {})}
    label_role = str(labels.get("llm-d.ai/role") or "").lower()
    resource_name = str(metadata.get("name") or "").lower()
    for role in ("prefill", "decode", "encode"):
        if label_role == role or role in resource_name:
            return role
    return "serving"


def _component(content: dict[str, Any], name: str) -> dict[str, Any] | None:
    value = content.get(name)
    if not isinstance(value, dict):
        return None
    fields = {
        key: value[key]
        for key in ("replicaCount", "tensorParallelSize", "maxModelLen", "maxNumSeqs")
        if value.get(key) is not None
    }
    return fields or None


def _merge_components(
    first: dict[str, Any] | None,
    second: dict[str, Any] | None,
    first_name: str,
    second_name: str,
) -> dict[str, Any] | None:
    if not first:
        return dict(second) if second else None
    if not second:
        return dict(first)
    result = dict(first)
    for key, value in second.items():
        if key in result and result[key] != value:
            label = {
                "replicaCount": "replica count",
                "tensorParallelSize": "tensor parallel size",
                "maxModelLen": "maximum model length",
                "maxNumSeqs": "maximum sequence count",
            }[key]
            raise ValueError(f"configuration {first_name} and {second_name} {label} facts disagree")
        result[key] = value
    return result


def _expected_components(content: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    deployment_type = str(content.get("deploymentType") or "").lower()
    if not deployment_type:
        deployment_type = "pd" if _component(content, "prefill") else "baseline"
    serving = _component(content, "serving")
    decode = _component(content, "decode")
    if deployment_type in {"baseline", "tiered_cache"}:
        merged = _merge_components(serving, decode, "serving", "decode")
        return deployment_type, {"serving": merged} if merged else {}
    if deployment_type in {"pd", "epd"}:
        if serving and decode:
            for key in ("replicaCount", "tensorParallelSize"):
                if key in serving and key in decode and serving[key] != decode[key]:
                    label = "replica count" if key == "replicaCount" else "tensor parallel size"
                    raise ValueError(f"configuration serving and decode {label} facts disagree")
        roles = ("prefill", "decode") if deployment_type == "pd" else ("encode", "prefill", "decode")
        return deployment_type, {
            role: component for role in roles if (component := _component(content, role)) is not None
        }
    raise ValueError(f"unsupported configuration deployment type {deployment_type!r}")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"configuration {label} must be a positive integer")
    return value


def _workload_role(deployment_type: str, deployment: dict[str, Any]) -> str:
    return "serving" if deployment_type in {"baseline", "tiered_cache"} else _role(deployment)


def _environment(container: dict[str, Any], deployment_name: str) -> dict[str, str | None]:
    entries = container.get("env") or []
    if not isinstance(entries, list):
        raise ValueError(f"model server {deployment_name} has an ambiguous environment")
    result: dict[str, str | None] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError(f"model server {deployment_name} has an ambiguous environment entry")
        env_name = entry["name"]
        env_value = entry.get("value")
        normalized = str(env_value) if env_value is not None else None
        if env_name in result and result[env_name] != normalized:
            raise ValueError(f"model server {deployment_name} has conflicting environment variable {env_name}")
        result[env_name] = normalized
    return result


def _validate_argument(tokens: list[str], deployment_name: str, argument_name: str, expected_value: Any) -> None:
    expected = str(expected_value)
    actual = _argument_value(tokens, {f"--{argument_name}"}, deployment_name)
    if expected.lower() == "false":
        negative = _argument_value(tokens, {f"--no-{argument_name}"}, deployment_name)
        if negative == "" and actual is _MISSING:
            return
    if expected.lower() == "true" and actual == "":
        return
    if actual is _MISSING or actual != expected:
        rendered = "absent" if actual is _MISSING else repr(actual)
        raise ValueError(
            f"model server {deployment_name} argument --{argument_name} is {rendered}; "
            f"configuration requires {expected!r}"
        )


def _validate_storage(
    deployment: dict[str, Any], container: dict[str, Any], runtime: dict[str, Any], deployment_name: str
) -> None:
    source = runtime.get("modelSource")
    host_path = runtime.get("mountPath")
    pvc_name = runtime.get("modelPvcClaimName") or runtime.get("pvcName")
    storage_id = runtime.get("storageVolumeId")
    storage_declared = source in {"shared-path", "auto-cache"} or any(
        isinstance(value, str) and value for value in (host_path, pvc_name, storage_id)
    )
    if not storage_declared:
        return
    if source not in {"shared-path", "auto-cache"}:
        raise ValueError("configuration storage requires runtime.modelSource shared-path or auto-cache")
    mounts = container.get("volumeMounts") or []
    volumes = (((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []
    if not isinstance(mounts, list) or not isinstance(volumes, list):
        raise ValueError(f"model server {deployment_name} storage is ambiguous")
    model_mounts = [mount for mount in mounts if isinstance(mount, dict) and mount.get("mountPath") == "/model-cache"]
    if len(model_mounts) != 1 or not isinstance(model_mounts[0].get("name"), str):
        raise ValueError(f"model server {deployment_name} storage must mount exactly one volume at /model-cache")
    mount = model_mounts[0]
    backing = [volume for volume in volumes if isinstance(volume, dict) and volume.get("name") == mount["name"]]
    if len(backing) != 1:
        raise ValueError(f"model server {deployment_name} storage volume is absent or ambiguous")
    volume = backing[0]
    if source == "shared-path" and mount.get("readOnly") is not True:
        raise ValueError(f"model server {deployment_name} shared-path storage must be read-only")
    if source == "auto-cache" and mount.get("readOnly") is True:
        raise ValueError(f"model server {deployment_name} auto-cache storage must be writable")
    if isinstance(pvc_name, str) and pvc_name:
        actual_claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
        if actual_claim != pvc_name:
            raise ValueError(
                f"model server {deployment_name} storage PVC is {actual_claim!r}; configuration requires {pvc_name!r}"
            )
    elif isinstance(host_path, str) and host_path:
        actual_path = (volume.get("hostPath") or {}).get("path")
        if actual_path != host_path:
            raise ValueError(
                f"model server {deployment_name} storage host path is {actual_path!r}; "
                f"configuration requires {host_path!r}"
            )
    elif storage_id and not (
        isinstance(volume.get("hostPath"), dict) or isinstance(volume.get("persistentVolumeClaim"), dict)
    ):
        raise ValueError(f"model server {deployment_name} storage has no hostPath or PVC backing")


def _validate_kv_topic(tokens: list[str], deployment_name: str, expected_model: str | None, *, required: bool) -> None:
    raw = _argument_value(tokens, {"--kv-events-config"}, deployment_name)
    if raw is _MISSING:
        if required:
            raise ValueError(f"model server {deployment_name} kv-events topic configuration is required")
        return
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"model server {deployment_name} kv-events topic configuration is empty")
    try:
        configuration = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise ValueError(f"model server {deployment_name} kv-events topic configuration is invalid") from error
    topic = configuration.get("topic") if isinstance(configuration, dict) else None
    if not isinstance(topic, str) or re.fullmatch(r"kv@[^@]+@.+", topic) is None:
        raise ValueError(f"model server {deployment_name} kv-events topic is malformed")
    topic_model = topic.rsplit("@", 1)[-1]
    if expected_model and topic_model != expected_model:
        raise ValueError(
            f"model server {deployment_name} kv-events topic names model {topic_model!r}; "
            f"configuration requires {expected_model!r}"
        )


def validate_manifest_facts(configuration_content: dict[str, Any], manifest: str) -> None:
    """Reject explicit Configuration facts that the rendered manifest does not prove."""
    model = configuration_content.get("model")
    expected_model = model.get("name") if isinstance(model, dict) else None
    if not isinstance(expected_model, str) or not expected_model:
        expected_model = None
    runtime_value = configuration_content.get("runtime")
    if runtime_value is not None and not isinstance(runtime_value, dict):
        raise ValueError("configuration runtime must be an object")
    runtime = runtime_value or {}
    custom_value = configuration_content.get("customParameters")
    if custom_value is not None and not isinstance(custom_value, list):
        raise ValueError("configuration customParameters must be an array")
    custom = custom_value or []
    managed_names = admin_managed_environment_names(custom)
    if managed_names:
        raise ValueError(
            "configuration cannot set administrator-managed Hugging Face environment variables: "
            + ", ".join(sorted(managed_names))
        )
    deployment_type, components = _expected_components(configuration_content)
    official = configuration_content.get("officialGuide") or {}
    source = official.get("source") if isinstance(official, dict) else {}
    precise_guide = isinstance(source, dict) and source.get("guide") == "precise-prefix-cache-routing"
    if not expected_model and not runtime and not custom and not components and not precise_guide:
        return
    workloads = _modelserver_containers(_documents(manifest))
    if not workloads:
        raise ValueError("rendered manifest has no modelserver Deployment for explicit configuration facts")
    workload_facts = []
    for deployment, container in workloads:
        name = str((deployment.get("metadata") or {}).get("name") or "<unnamed>")
        tokens = _invocation_tokens(container, name)
        role = _workload_role(deployment_type, deployment)
        workload_facts.append((deployment, container, name, role, tokens))

    for role, component in components.items():
        role_workloads = [workload for workload in workload_facts if workload[3] == role]
        if not role_workloads:
            raise ValueError(f"rendered manifest has no modelserver Deployment for configuration role {role}")
        if "replicaCount" in component:
            expected_replicas = _positive_int(component["replicaCount"], f"{role} replica count")
            actual_replicas = sum(
                _positive_int((deployment.get("spec") or {}).get("replicas", 1), f"{name} replica count")
                for deployment, _container, name, _role_name, _tokens in role_workloads
            )
            if actual_replicas != expected_replicas:
                raise ValueError(
                    f"modelserver {role} replica count is {actual_replicas}; configuration requires {expected_replicas}"
                )
        if "tensorParallelSize" in component:
            expected_tp = _positive_int(component["tensorParallelSize"], f"{role} tensor parallel size")
            for _deployment, _container, name, _role_name, tokens in role_workloads:
                actual = _argument_value(
                    tokens,
                    {"--tensor-parallel-size", "--tensor_parallel_size", "--tp_size"},
                    name,
                )
                if actual is _MISSING:
                    actual_tp = 1
                else:
                    try:
                        actual_tp = int(str(actual))
                    except ValueError as error:
                        raise ValueError(f"model server {name} tensor parallel size is invalid") from error
                if actual_tp != expected_tp:
                    raise ValueError(
                        f"model server {name} tensor parallel size is {actual_tp}; configuration requires {expected_tp}"
                    )

    expected_image = runtime.get("image")
    runtime_environment_value = runtime.get("environment")
    if runtime_environment_value is not None and not isinstance(runtime_environment_value, dict):
        raise ValueError("configuration runtime.environment must be an object")
    runtime_environment = runtime_environment_value or {}
    model_server = runtime.get("modelServer")
    if model_server and str(model_server).lower() != "vllm":
        raise ValueError(f"runtime.modelServer {model_server!r} cannot be proved by a vllm manifest")
    expected_argument = "/model-cache" if runtime.get("modelSource") == "shared-path" else expected_model
    global_max_model_len = model.get("maxModelLen") if isinstance(model, dict) else None
    for deployment, container, name, role, tokens in workload_facts:
        if expected_argument:
            actual_model = _model_argument(tokens, name)
            if actual_model != expected_argument:
                raise ValueError(
                    f"model server {name} serves model {actual_model!r}; configuration requires {expected_argument!r}"
                )
            served_name = _option(tokens, {"--served-model-name"}, name)
            if served_name is not None and served_name != expected_model:
                raise ValueError(
                    f"model server {name} exposes model {served_name!r}; configuration requires {expected_model!r}"
                )
            if (
                runtime.get("modelSource") == "shared-path"
                and _argument_value(tokens, {"--kv-events-config"}, name) is not _MISSING
                and served_name is None
            ):
                raise ValueError(
                    f"model server {name} must declare --served-model-name={expected_model} "
                    "when publishing precise routing events from shared-path storage"
                )
        if expected_image is not None and container.get("image") != expected_image:
            raise ValueError(
                f"model server {name} image is {container.get('image')!r}; configuration requires {expected_image!r}"
            )
        environment = _environment(container, name)
        for variable, expected_value in runtime_environment.items():
            if environment.get(variable) != str(expected_value):
                raise ValueError(
                    f"model server {name} environment variable {variable} is {environment.get(variable)!r}; "
                    f"configuration requires {str(expected_value)!r}"
                )
        if global_max_model_len is not None:
            _validate_argument(tokens, name, "max-model-len", global_max_model_len)
        component = components.get(role) or {}
        for field, argument_name in (("maxModelLen", "max-model-len"), ("maxNumSeqs", "max-num-seqs")):
            if component.get(field) is not None:
                _validate_argument(tokens, name, argument_name, component[field])
        effective_custom_role = role if deployment_type in {"pd", "epd"} else "decode"
        for override in custom:
            if not isinstance(override, dict):
                raise ValueError("configuration customParameters entries must be objects")
            target = override.get("target")
            kind = override.get("kind")
            override_name = override.get("name")
            if target not in {"prefill", "decode", "encode", "both"} or kind not in {"argument", "environment"}:
                raise ValueError("configuration contains an invalid custom runtime override")
            if not isinstance(override_name, str) or not override_name or override.get("value") is None:
                raise ValueError("configuration contains an incomplete custom runtime override")
            if target not in {"both", effective_custom_role}:
                continue
            if kind == "argument":
                _validate_argument(tokens, name, override_name, override["value"])
            elif environment.get(override_name) != str(override["value"]):
                raise ValueError(
                    f"model server {name} environment variable {override_name} is "
                    f"{environment.get(override_name)!r}; configuration requires {str(override['value'])!r}"
                )
        _validate_storage(deployment, container, runtime, name)
        _validate_kv_topic(tokens, name, expected_model, required=precise_guide)

    for override in custom:
        if not isinstance(override, dict):
            continue
        target = override.get("target")
        effective_roles = {workload[3] if deployment_type in {"pd", "epd"} else "decode" for workload in workload_facts}
        if target != "both" and target not in effective_roles:
            raise ValueError(f"no model server can prove runtime override target {target!r}")
