"""Explicit candidate deployment and an opaque, caller-supplied Kubernetes Job."""

from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .common import VERSION_LABEL, RolloutError, read, require, run, write
from .validate import all_pod_labels, matches, validate


def list_items(value):
    return value.get("items", []) if value.get("kind") == "List" else [value]


def router_endpoint(docs, info):
    service_name = f"{info['release']}-epp"
    service = next(
        (doc for doc in docs if doc["kind"] == "Service" and doc["metadata"]["name"] == service_name), None
    )
    require(service is not None, f"router Service {service_name} is missing")
    ports = [
        port
        for port in service["spec"]["ports"]
        if port.get("name") in ("http", "http-8081") or port["port"] in (80, 8081)
    ]
    require(len(ports) == 1, "cannot determine the router HTTP port; expose one http port on the EPP Service")
    return f"http://{service_name}.{info['namespace']}.svc.cluster.local:{ports[0]['port']}"


def functional_job(path, state, docs):
    job = copy.deepcopy(read(path))
    require(
        job.get("apiVersion") == "batch/v1" and job.get("kind") == "Job",
        "--job must contain one batch/v1 Job",
    )
    info = state["versions"]["v1"]
    metadata = job.setdefault("metadata", {})
    require(
        metadata.get("namespace", info["namespace"]) == info["namespace"],
        "test Job has a different namespace",
    )
    metadata.pop("name", None)
    metadata["generateName"] = f"{info['release']}-test-"
    metadata["namespace"] = info["namespace"]
    metadata.setdefault("labels", {})[VERSION_LABEL] = info["label"]
    spec = job["spec"]["template"]["spec"]
    require(spec.get("containers"), "test Job needs at least one container")
    require(
        spec.get("restartPolicy") in ("Never", "OnFailure"),
        "test Job restartPolicy must be Never or OnFailure",
    )
    values = {
        "ROLLOUT_ROUTER_URL": router_endpoint(docs, info),
        "ROLLOUT_VERSION": info["label"],
        "ROLLOUT_MODEL": state["model"],
        "ROLLOUT_ENVIRONMENT": state["environment"],
    }
    for container in spec.get("containers", []) + spec.get("initContainers", []):
        container["env"] = [env for env in container.get("env", []) if env["name"] not in values]
        container["env"].extend({"name": key, "value": value} for key, value in values.items())
    return job


def live_preflight(kube, root, state, rendered, *, bootstrap_v0):
    # Check BOTH directions against real selectors. A legacy broad v0 pool would
    # otherwise discover v1 even though v1's new selector correctly excludes v0.
    pools = json.loads(run(kube + ["get", "inferencepools.inference.networking.k8s.io", "-o", "json"]))[
        "items"
    ]
    planned = ("v0", "v1") if bootstrap_v0 else ("v1",)
    for name in planned:
        labels = all_pod_labels(rendered[name])
        for pool in pools:
            selector = pool.get("spec", {}).get("selector", {})
            if any(matches(selector, pod) for pod in labels):
                require(
                    pool["metadata"].get("labels", {}).get(VERSION_LABEL) == state["versions"][name]["label"],
                    f"live pool {pool['metadata']['name']} would select {name}; isolate existing pools first",
                )
        found = json.loads(
            run(kube + ["get", "-f", root / "rendered" / f"{name}.yaml", "--ignore-not-found", "-o", "json"])
        )
        for doc in list_items(found):
            if doc:
                require(
                    doc["metadata"].get("labels", {}).get(VERSION_LABEL) == state["versions"][name]["label"],
                    f"refusing to overwrite an unmanaged resource: {doc['kind']}/{doc['metadata']['name']}",
                )
    if not bootstrap_v0:
        existing = json.loads(run(kube + ["get", "-f", root / "rendered/v0.yaml", "-o", "json"]))
        require(
            all(
                doc["metadata"].get("labels", {}).get(VERSION_LABEL) == state["versions"]["v0"]["label"]
                for doc in list_items(existing)
            ),
            "live v0 is not the prepared baseline",
        )
    # Also ensure a candidate pool cannot discover unrelated live pods carrying reused labels.
    pods = json.loads(run(kube + ["get", "pods", "-o", "json"]))["items"]
    for name in planned:
        selector = next(doc["spec"]["selector"] for doc in rendered[name] if doc["kind"] == "InferencePool")
        for pod in pods:
            if matches(selector, pod["metadata"].get("labels", {})):
                require(
                    pod["metadata"]["labels"].get(VERSION_LABEL) == state["versions"][name]["label"],
                    f"{name} would select an unrelated live pod",
                )


def model_ready(docs, pods):
    model = next(doc for doc in docs if doc["kind"] == "DisaggregatedSet")
    slices = model["spec"].get("slices", 1)
    require(type(slices) is int and slices > 0, "DisaggregatedSet slices must be positive")
    for role in model["spec"]["roles"]:
        role_spec = role["spec"]
        template = role_spec["leaderWorkerTemplate"]
        count = slices * role_spec.get("replicas", 1) * template.get("size", 1)
        require(type(count) is int and count > 0, "DisaggregatedSet role must request at least one pod")
        templates = [
            template[key].get("metadata", {}).get("labels", {})
            for key in ("workerTemplate", "leaderTemplate")
            if key in template
        ]
        matching = [
            pod
            for pod in pods
            if not pod["metadata"].get("deletionTimestamp")
            and any(matches(labels, pod["metadata"].get("labels", {})) for labels in templates)
        ]
        if len(matching) < count or not all(
            any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
            for pod in matching
        ):
            return False
    return True


def wait_ready(kube, docs, info, timeout):
    deadline = time.monotonic() + timeout
    while True:
        pods = json.loads(
            run(kube + ["get", "pods", "-l", f"{VERSION_LABEL}={info['label']}", "-o", "json"])
        )["items"]
        if model_ready(docs, pods):
            break
        require(
            time.monotonic() < deadline,
            "timed out waiting for all DisaggregatedSet role pods to become Ready",
        )
        time.sleep(5)
    for doc in docs:
        if doc["kind"] in ("Deployment", "StatefulSet"):
            remaining = max(1, int(deadline - time.monotonic()))
            run(
                kube
                + [
                    "rollout",
                    "status",
                    f"{doc['kind'].lower()}/{doc['metadata']['name']}",
                    f"--timeout={remaining}s",
                ],
                timeout=remaining + 15,
            )


def wait_job(kube, name, timeout):
    deadline = time.monotonic() + timeout
    while True:
        job = json.loads(run(kube + ["get", "job", name, "-o", "json"]))
        for condition in job.get("status", {}).get("conditions", []):
            if condition.get("status") == "True":
                if condition["type"] == "Complete":
                    return
                if condition["type"] in ("Failed", "FailureTarget"):
                    raise RolloutError(
                        f"functional Job {name} failed: {condition.get('message', condition.get('reason'))}"
                    )
        require(time.monotonic() < deadline, f"functional Job {name} timed out")
        time.sleep(5)


def test_rollout(root, *, context=None, apply=False, bootstrap_v0=False, job_path=None, timeout=1800):
    root = Path(root).resolve()
    require(timeout > 0, "timeout must be positive")
    state, rendered = validate(root)
    require(not state["noOp"], "v0 and v1 have identical effective content; there is no rollout to test")
    require(not apply or context, "--apply requires an explicit --context")
    require(not apply or job_path, "--apply requires --job with the functional-test Job manifest")
    namespace = state["versions"]["v1"]["namespace"]
    kube = ["kubectl", "--context", context or "<context>", "--namespace", namespace]
    slots = ["v0", "v1"] if bootstrap_v0 else ["v1"]
    job = functional_job(job_path, state, rendered["v1"]) if job_path else None
    plan = {
        "context": context,
        "namespace": namespace,
        "apply": apply,
        "versionsToDeploy": slots,
        "routerURL": router_endpoint(rendered["v1"], state["versions"]["v1"]),
        "commands": [
            list(map(str, kube + ["apply", "-f", root / "rendered" / f"{name}.yaml"])) for name in slots
        ],
        "functionalJob": job,
    }
    write(root / "test-plan.yaml", plan)
    if not apply:
        return plan
    result = {"status": "running", "context": context, "startedAt": datetime.now(UTC).isoformat()}
    write(root / "test-result.yaml", result)
    try:
        live_preflight(kube, root, state, rendered, bootstrap_v0=bootstrap_v0)
        # Admission-check every intended write before the first deployment write.
        for name in slots:
            run(kube + ["apply", "--dry-run=server", "-f", root / "rendered" / f"{name}.yaml"])
        run(kube + ["create", "--dry-run=server", "-f", "-"], input=yaml.safe_dump(job))
        for name in slots:
            run(kube + ["apply", "-f", root / "rendered" / f"{name}.yaml"])
            wait_ready(kube, rendered[name], state["versions"][name], timeout)
        created = json.loads(run(kube + ["create", "-f", "-", "-o", "json"], input=yaml.safe_dump(job)))
        result["job"] = created["metadata"]["name"]
        write(root / "test-result.yaml", result)
        wait_job(kube, result["job"], timeout)
        result["status"] = "passed"
    except (RolloutError, OSError, ValueError, KeyError, TypeError, KeyboardInterrupt) as exc:
        result.update(status="failed", error=str(exc) or "interrupted")
        raise
    finally:
        result["finishedAt"] = datetime.now(UTC).isoformat()
        write(root / "test-result.yaml", result)
    return result
