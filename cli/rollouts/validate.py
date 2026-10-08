"""Render both versions and check identities, selectors, and input integrity."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from .common import VERSION_LABEL, digest, documents, read, require, run, write
from .prepare import content_hash, inputs, kustomization, pod_templates, renderer, version_info

CLUSTER_KINDS = {"ClusterRole", "ClusterRoleBinding"}
VERSION_KINDS = CLUSTER_KINDS | {
    "DisaggregatedSet",
    "Deployment",
    "StatefulSet",
    "Service",
    "ConfigMap",
    "ServiceAccount",
    "Role",
    "RoleBinding",
    "InferencePool",
    "InferenceObjective",
    "PodMonitor",
    "ServiceMonitor",
}


def identity(doc):
    api = doc["apiVersion"]
    return (
        api.split("/")[0] if "/" in api else "",
        doc["kind"],
        doc["metadata"].get("namespace", ""),
        doc["metadata"]["name"],
    )


def matches(selector, labels):
    simple = (
        selector.get("matchLabels", {})
        if ("matchLabels" in selector or "matchExpressions" in selector)
        else selector
    )
    if not all(labels.get(key) == value for key, value in simple.items()):
        return False
    for expr in selector.get("matchExpressions", []):
        key, operator, values = expr["key"], expr["operator"], expr.get("values", [])
        require(operator in ("In", "NotIn", "Exists", "DoesNotExist"), f"unsupported selector: {operator}")
        if not {
            "In": key in labels and labels[key] in values,
            "NotIn": labels.get(key) not in values,
            "Exists": key in labels,
            "DoesNotExist": key not in labels,
        }[operator]:
            return False
    return True


def all_pod_labels(docs):
    labels = []
    for doc in docs:
        if doc["kind"] == "DisaggregatedSet":
            labels.extend(
                template.get("metadata", {}).get("labels", {}) for _, template in pod_templates(doc)
            )
        elif doc["kind"] in ("Deployment", "StatefulSet"):
            labels.append(doc["spec"]["template"]["metadata"]["labels"])
    return labels


def check_version(docs, info):
    require(docs, "Kustomize rendered no resources")
    identities = set()
    for doc in docs:
        require(
            doc.get("kind") in VERSION_KINDS, f"unsupported version-owned resource kind: {doc.get('kind')}"
        )
        require(
            doc.get("apiVersion") and doc.get("metadata", {}).get("name"), "resource lacks apiVersion/name"
        )
        metadata = doc["metadata"]
        require(
            metadata.get("labels", {}).get(VERSION_LABEL) == info["label"], "resource missing rollout version"
        )
        if doc["kind"] in CLUSTER_KINDS:
            require(not metadata.get("namespace"), "cluster-scoped resource has a namespace")
        else:
            require(metadata.get("namespace") == info["namespace"], f"wrong namespace for {metadata['name']}")
        ident = identity(doc)
        require(ident not in identities, f"duplicate resource identity: {ident}")
        identities.add(ident)
    models = [doc for doc in docs if doc["kind"] == "DisaggregatedSet"]
    pools = [doc for doc in docs if doc["kind"] == "InferencePool"]
    require(
        len(models) == 1 and len(pools) == 1, "expected exactly one DisaggregatedSet and one InferencePool"
    )
    selector = pools[0].get("spec", {}).get("selector", {})
    require(
        selector.get("matchLabels", selector).get(VERSION_LABEL) == info["label"],
        "rendered InferencePool does not select its rollout version",
    )
    for path, template in pod_templates(models[0]):
        labels = template.get("metadata", {}).get("labels", {})
        require(
            labels.get(VERSION_LABEL) == info["label"] and matches(selector, labels),
            f"InferencePool cannot select its modelserver at {path}",
        )
    for doc in docs:
        if doc["kind"] in ("Deployment", "StatefulSet"):
            require(
                matches(doc["spec"]["selector"], doc["spec"]["template"]["metadata"]["labels"]),
                f"{doc['metadata']['name']}: workload selector does not match pod template",
            )
    return identities


def check_isolation(left, right):
    overlap = {identity(doc) for doc in left} & {identity(doc) for doc in right}
    require(not overlap, f"v0/v1 resource names collide: {sorted(overlap)}")
    for own, other in ((left, right), (right, left)):
        other_labels = all_pod_labels(other)
        for doc in own:
            if doc["kind"] in ("Service", "InferencePool", "Deployment", "StatefulSet"):
                selector = doc.get("spec", {}).get("selector")
                require(
                    not selector or not any(matches(selector, label) for label in other_labels),
                    f"{doc['kind']}/{doc['metadata']['name']} selects the other version's pods",
                )


def chart_digest(root):
    files = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), f"chart contains symlink: {path}")
        if path.is_file():
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    require(files, f"render did not produce a chart at {root}")
    return digest(files)


def validate(root, *, context=None, server_dry_run=False):
    root = Path(root).resolve()
    require(not any(p.is_symlink() for p in root.rglob("*")), "rollout directory must not contain symlinks")
    state = read(root / "rollout.yaml")
    require(state.get("schema") == 1, "unsupported rollout schema")
    rendered = {}
    report = {"schema": 1, "versions": {}, "serverDryRun": server_dry_run}
    for name in ("v0", "v1"):
        slot = root / name
        info = state["versions"][name]
        content = read(slot / "inputs/content.yaml")
        require(
            content["model"] == state["model"] and content["environment"] == state["environment"],
            f"{name}: model/environment changed; prepare again",
        )
        require(content["namespace"] == info["namespace"], f"{name}: namespace changed; prepare again")
        require(
            all(info[key] == value for key, value in version_info(content).items()),
            f"{name}: content hash mismatch; prepare a new rollout",
        )
        chart = content["components"]["router"]["chart"]
        source_content = inputs(
            slot / "source",
            model=state["model"],
            environment=state["environment"],
            chart=f"{chart['repo']}/{chart['name']}",
            chart_version=chart["version"],
        )
        require(
            content_hash(source_content) == info["sha256"], f"{name}: source snapshot changed; prepare again"
        )
        require(
            read(slot / "inputs/model-server.yaml") == content["components"]["modelserver"],
            f"{name}: generated modelserver was edited; prepare again",
        )
        require(
            read(slot / "kustomization.yaml") == kustomization(content),
            f"{name}: generated kustomization was edited; prepare again",
        )
        text = run(renderer() + [slot, "--enable-helm"])
        docs = documents(text)
        check_version(docs, info)
        sha = chart_digest(slot / "charts")
        lock = slot / "chart-lock.yaml"
        if lock.exists():
            require(
                read(lock) == {"sha256": sha, "chart": chart},
                f"{name}: cached chart changed since first validation",
            )
        else:
            write(lock, {"sha256": sha, "chart": chart})
        rendered[name] = docs
        report["versions"][name] = {
            "resources": len(docs),
            "renderedSha256": digest(docs),
            "chartSha256": sha,
        }
    require(
        state["versions"]["v0"]["namespace"] == state["versions"]["v1"]["namespace"], "namespace mismatch"
    )
    no_op = state["versions"]["v0"]["sha256"] == state["versions"]["v1"]["sha256"]
    require(state["noOp"] == no_op, "noOp state does not match content hashes")
    if not no_op:
        check_isolation(rendered["v0"], rendered["v1"])
    else:
        require(
            rendered["v0"] == rendered["v1"],
            "identical content rendered differently; check chart determinism",
        )
    if server_dry_run:
        require(context, "--server-dry-run requires an explicit --context")
        for docs in rendered.values():
            run(
                ["kubectl", "--context", context, "apply", "--dry-run=server", "-f", "-"],
                input=yaml.safe_dump_all(docs, sort_keys=False),
            )
    (root / "rendered").mkdir(exist_ok=True)
    for name, docs in rendered.items():
        (root / "rendered" / f"{name}.yaml").write_text(yaml.safe_dump_all(docs, sort_keys=False))
    write(root / "validation.yaml", report)
    return state, rendered
