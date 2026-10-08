"""Snapshot Git inputs and generate immutable, content-addressed serving versions."""

from __future__ import annotations

import copy
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

import yaml

from .common import (
    ROLE_LABEL,
    VERSION_LABEL,
    digest,
    dns_label,
    documents,
    local_path,
    read,
    require,
    run,
    segment,
    write,
)


def resolve_ref(repo, ref, *, target=False, remote="origin"):
    if re.fullmatch(r"pr-[1-9][0-9]*", ref):
        number = ref[3:]
        destination = f"refs/lens-rollouts/pr/{number}"
        # Always fetch the current PR head, never a stale local PR ref or merge ref.
        run(
            [
                "git",
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--",
                remote,
                f"+refs/pull/{number}/head:{destination}",
            ],
            cwd=repo,
        )
        ref = destination
    elif target:
        require(re.fullmatch(r"[0-9a-fA-F]{7,40}", ref), "--to must be a commit SHA or pr-<number>")
    return run(["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], cwd=repo).strip()


def snapshot(repo, commit, relative, destination):
    with tempfile.TemporaryDirectory(prefix="lens-git-") as temporary:
        archive = Path(temporary) / "source.tar"
        run(["git", "archive", "--format=tar", f"--output={archive}", f"{commit}:{relative}"], cwd=repo)
        destination.mkdir(parents=True)
        with tarfile.open(archive) as source:
            for member in source:
                target = destination / member.name
                require(
                    target.resolve().is_relative_to(destination.resolve()), "archive path escapes snapshot"
                )
                require(member.isdir() or member.isfile(), f"unsupported Git entry: {member.name}")
                if member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as stream:
                        target.write_bytes(stream.read())


def renderer():
    if shutil.which("kustomize"):
        return ["kustomize", "build"]
    require(shutil.which("kubectl"), "install kustomize or kubectl to render manifests")
    return ["kubectl", "kustomize"]


def check_local_kustomizations(source):
    """Require snapshots to be self-contained; never resolve bases against the live checkout."""
    for file in source.rglob("*"):
        if file.name not in ("kustomization.yaml", "kustomization.yml", "Kustomization"):
            continue
        config = read(file)
        require(
            not config.get("helmCharts") and not config.get("generators"),
            "source kustomizations must contain modelserver resources only; "
            "configure router in rollout-config.yaml",
        )
        paths = []
        for key in ("resources", "bases", "components", "crds", "configurations", "transformers"):
            paths.extend(config.get(key, []))
        for item in config.get("patches", []) + config.get("patchesJson6902", []):
            if "path" in item:
                paths.append(item["path"])
        paths.extend(
            p for p in config.get("patchesStrategicMerge", []) if isinstance(p, str) and "\n" not in p
        )
        for generator in config.get("configMapGenerator", []) + config.get("secretGenerator", []):
            paths.extend(p.split("=", 1)[-1] for p in generator.get("files", []))
            paths.extend(generator.get("envs", []))
        for path in paths:
            require(
                isinstance(path, str) and not Path(path).is_absolute() and ":" not in path,
                f"{file}: only snapshot-local Kustomize inputs are supported: {path}",
            )
            require(
                (file.parent / path).resolve().is_relative_to(source.resolve()),
                f"{file}: dependency escapes the environment snapshot: {path}",
            )


def merge_values(left, right):
    result = copy.deepcopy(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_values(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def hash_values(values):
    result = copy.deepcopy(values)
    # These documented chart fields contain YAML, despite being scalar strings in values.yaml.
    # Shell scripts, prompts, and other arbitrary strings retain their exact bytes.
    configs = result.get("router", {}).get("epp", {}).get("pluginsCustomConfig", {})
    for key, value in configs.items():
        require(isinstance(value, str), f"pluginsCustomConfig.{key} must be a YAML string")
        configs[key] = documents(value)
    return result


def pod_templates(model):
    require(model.get("kind") == "DisaggregatedSet", "expected a DisaggregatedSet")
    roles = model.get("spec", {}).get("roles", [])
    require(isinstance(roles, list) and roles, "DisaggregatedSet must have roles")
    names = [role.get("name") for role in roles]
    require(
        all(dns_label(name) for name in names) and len(set(names)) == len(names),
        "DisaggregatedSet role names must be unique DNS labels",
    )
    for index, role in enumerate(roles):
        template = role.get("spec", {}).get("leaderWorkerTemplate", {})
        require("workerTemplate" in template, f"role {role.get('name', index)} has no workerTemplate")
        for kind in ("workerTemplate", "leaderTemplate"):
            if kind in template:
                yield f"/spec/roles/{index}/spec/leaderWorkerTemplate/{kind}", template[kind]


def inputs(source, *, model, environment, chart=None, chart_version=None):
    config_path = source / "rollout-config.yaml"
    config = read(config_path) if config_path.exists() else {}
    require(config.get("schema", 1) == 1, "unsupported rollout-config schema")
    require(not config.get("litellm"), "LiteLLM rollout changes are not supported yet")
    check_local_kustomizations(source)
    rendered = documents(run(renderer() + [source]))
    require(
        len(rendered) == 1 and rendered[0].get("kind") == "DisaggregatedSet",
        "the source kustomization must render exactly one DisaggregatedSet; "
        "provision shared resources separately",
    )
    modelserver = rendered[0]
    namespace = config.get("namespace", modelserver.get("metadata", {}).get("namespace"))
    require(dns_label(namespace), "set a valid namespace in the manifest or rollout-config.yaml")
    require(
        modelserver.get("metadata", {}).get("namespace", namespace) == namespace,
        "modelserver namespace differs from rollout-config.yaml",
    )
    router = config.get("router", {})
    values_files = router.get("values")
    if values_files is None:
        values_files = sorted(str(p.relative_to(source)) for p in (source / "router").glob("*.values.yaml"))
        require(
            len(values_files) == 1,
            "list router.values in rollout-config.yaml when there isn't exactly one values file",
        )
    require(isinstance(values_files, list) and values_files, "router.values must be a nonempty ordered list")
    values = {}
    for file in values_files:
        values = merge_values(values, read(local_path(source, file)))
    chart_info = copy.deepcopy(router.get("chart", {}))
    if chart:
        repo, separator, name = chart.rstrip("/").rpartition("/")
        require(
            separator and repo.startswith(("oci://", "https://")),
            "--chart must be <oci://registry/repository|https://chart-repository>/<chart-name>",
        )
        chart_info.update(name=name, repo=repo)
    if chart_version:
        chart_info["version"] = chart_version
    require(dns_label(chart_info.get("name")), "set router.chart.name or pass --chart")
    require(
        str(chart_info.get("repo", "")).startswith(("oci://", "https://")),
        "set router.chart.repo or pass --chart",
    )
    require(
        isinstance(chart_info.get("version"), str)
        and re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", chart_info["version"]),
        "pin router.chart.version in rollout-config.yaml or pass --chart-version (e.g. v0.11.0)",
    )
    require(set(chart_info) == {"name", "repo", "version"}, "router.chart supports name, repo, and version")
    selector = values.get("router", {}).get("modelServers", {}).get("matchLabels", {})
    require(isinstance(selector, dict) and selector, "router.modelServers.matchLabels must be explicit")
    require(VERSION_LABEL not in selector, f"{VERSION_LABEL} is generated; remove it from authored values")
    for path, template in pod_templates(modelserver):
        labels = template.get("metadata", {}).get("labels", {})
        require(
            VERSION_LABEL not in labels and ROLE_LABEL not in labels,
            f"{path}: remove generated rollout labels from authored manifests",
        )
        require(
            all(isinstance(v, str) and labels.get(k) == v for k, v in selector.items()),
            f"{path}: pod labels do not match router.modelServers.matchLabels {selector}",
        )
    content = {
        "schema": 1,
        "model": model,
        "environment": environment,
        "namespace": namespace,
        "components": {"modelserver": modelserver, "router": {"chart": chart_info, "values": values}},
    }
    return content


def content_hash(content):
    canonical = copy.deepcopy(content)
    canonical["components"]["router"]["values"] = hash_values(canonical["components"]["router"]["values"])
    return digest(canonical)


def version_info(content):
    sha = content_hash(content)
    return {"sha256": sha, "label": f"h-{sha[:40]}", "release": f"r-{sha[:24]}"}


def kustomization(content):
    info = version_info(content)
    version = info["label"]
    release = info["release"]  # Short enough that charts truncating at 40 chars retain the hash.
    modelserver = content["components"]["modelserver"]
    values = copy.deepcopy(content["components"]["router"]["values"])
    router = values["router"]
    # Content ignored by the hash must also render identically (especially when a
    # comment-only PR has the same identity as its baseline).
    configs = router.get("epp", {}).get("pluginsCustomConfig", {})
    for key, value in configs.items():
        configs[key] = yaml.safe_dump_all(documents(value), sort_keys=True)
    router["modelServers"]["matchLabels"][VERSION_LABEL] = version
    require(
        router.get("inferencePool", {}).get("create", True) is not False,
        "an isolated rollout requires the router chart to create an InferencePool",
    )
    # InferenceObjectives and Envoy ConfigMaps have literal names in the standalone chart.
    for objective in router.get("inferenceObjectives", []):
        require(dns_label(objective.get("name")), "InferenceObjective name must be a DNS label")
        objective["name"] = f"{objective['name'][:30].rstrip('-')}-{info['sha256'][:16]}"
    proxy = router.setdefault("proxy", {})
    require(proxy.get("proxyType", "envoy") == "envoy", "initial rollout support requires the Envoy proxy")
    preset = proxy.setdefault("presets", {}).setdefault("envoy", {})
    envoy_name = f"{release}-envoy"
    old_names = {
        preset.get("configMap", {}).get("name", "envoy"),
        proxy.get("configMap", {}).get("name", "envoy"),
    }
    preset.setdefault("configMap", {})["name"] = envoy_name
    # Defaults use a Helm template reference. Literal references in authored overrides need rewriting.
    for section in (preset, proxy):
        if section.get("configMap", {}).get("name"):
            section["configMap"]["name"] = envoy_name
        for volume in section.get("volumes", []):
            if volume.get("configMap", {}).get("name") in old_names:
                volume["configMap"]["name"] = envoy_name
    patches = [{"op": "replace", "path": "/metadata/name", "value": f"{release}-model"}]
    for path, template in pod_templates(modelserver):
        metadata = copy.deepcopy(template.get("metadata", {}))
        metadata.setdefault("labels", {})[VERSION_LABEL] = version
        # Keep readiness counts separate even if authored role labels are identical.
        role_index = int(path.split("/")[3])
        metadata["labels"][ROLE_LABEL] = modelserver["spec"]["roles"][role_index]["name"]
        patches.append({"op": "add", "path": f"{path}/metadata", "value": metadata})

    return {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": content["namespace"],
        "resources": ["inputs/model-server.yaml"],
        "labels": [{"pairs": {VERSION_LABEL: version}, "includeTemplates": True}],
        "patches": [
            {
                "target": {"kind": "DisaggregatedSet", "name": modelserver["metadata"]["name"]},
                "patch": yaml.safe_dump(patches, sort_keys=False),
            }
        ],
        "helmCharts": [
            {
                **content["components"]["router"]["chart"],
                "releaseName": release,
                "namespace": content["namespace"],
                "valuesInline": values,
                "valuesMerge": "override",
                "skipTests": True,
            }
        ],
    }


def generate(slot, content):
    write(slot / "inputs/model-server.yaml", content["components"]["modelserver"])
    write(slot / "inputs/content.yaml", content)
    write(slot / "kustomization.yaml", kustomization(content))
    return version_info(content)


def prepare(
    repo,
    *,
    from_ref="main",
    to_ref,
    model,
    environment,
    output=None,
    remote="origin",
    chart=None,
    chart_version=None,
):
    repo = Path(repo).resolve()
    relative = f"deployments/{segment(model)}/{segment(environment)}"
    before = resolve_ref(repo, from_ref, remote=remote)
    after = resolve_ref(repo, to_ref, target=True, remote=remote)
    output = (
        Path(output).absolute()
        if output
        else repo / ".rollouts" / model / environment / f"{before[:12]}-{after[:12]}"
    )
    require(not output.exists(), f"output already exists: {output}; choose a new --output")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=output.parent) as temporary:
        stage = Path(temporary) / "rollout"
        state = {
            "schema": 1,
            "model": model,
            "environment": environment,
            "sourceRepository": str(repo),
            "versions": {},
        }
        for name, ref, commit in (("v0", from_ref, before), ("v1", to_ref, after)):
            slot = stage / name
            snapshot(repo, commit, relative, slot / "source")
            content = inputs(
                slot / "source",
                model=model,
                environment=environment,
                chart=chart,
                chart_version=chart_version,
            )
            info = generate(slot, content)
            state["versions"][name] = {
                "ref": ref,
                "commit": commit,
                **info,
                "namespace": content["namespace"],
            }
        require(
            state["versions"]["v0"]["namespace"] == state["versions"]["v1"]["namespace"],
            "v0 and v1 must use the same namespace",
        )
        state["noOp"] = state["versions"]["v0"]["sha256"] == state["versions"]["v1"]["sha256"]
        write(stage / "rollout.yaml", state)
        stage.rename(output)
    return output, state
