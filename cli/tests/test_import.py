import copy
import json

import pytest

from llmd import inventory, lifecycle
from llmd.catalog import find_stack
from llmd.cli import main
from llmd.errors import ConflictError, NotFoundError, PolicyError
from llmd.importing import compare, drift, import_stack
from llmd.kube import Kube
from llmd.plan import Options, make_plan
from llmd.preflight import preflight
from llmd.render import render
from llmd.sources import resolve_source
from llmd.status import instance_status

from .fakes import FakeCluster
from .test_preflight import STACK


def deployed_with_oc_apply(cbrepo, cluster, namespace="model-a-serving"):
    """Apply the stack the way a team does: `oc apply -k` of the overlay, no llmd labels."""
    cluster.namespaces.add(namespace)
    for document in render(find_stack(resolve_source(str(cbrepo)), STACK), runner=cluster).documents:
        live = copy.deepcopy(document)
        live["metadata"].setdefault("annotations", {})["kubectl.kubernetes.io/last-applied-configuration"] = (
            "{}"
        )
        live.setdefault("spec", {}).setdefault("revisionHistoryLimit", 10)  # a server default
        cluster.put(namespace, live)
    cluster.settle()
    for name in ("hf-token",):
        cluster.put(namespace, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}})
    cluster.put(namespace, {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "model-a"}})
    cluster.put(
        namespace,
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "model-a-cache"}},
    )


def import_plan(cbrepo, cluster, namespace="model-a-serving"):
    stack = find_stack(resolve_source(str(cbrepo)), STACK)
    plan = make_plan(stack, Options(namespace=namespace, instance="glm"), runner=cluster, label=False)
    preflight(Kube(runner=cluster), plan, existing="import")
    return plan


@pytest.fixture
def cluster():
    return FakeCluster(namespaces=())


def test_import_records_running_objects_and_changes_nothing_else(cbrepo, cluster):
    deployed_with_oc_apply(cbrepo, cluster)
    before = copy.deepcopy(cluster.objects)
    plan = import_plan(cbrepo, cluster)
    assert not plan.blocking and plan.cluster["objects"]["foreign"] == 2
    result = import_stack(Kube(runner=cluster), plan, manager="kubectl")
    assert result == {"instance": "glm", "namespace": "model-a-serving", "imported": 2, "manager": "kubectl"}
    changed = {key for key in cluster.objects if cluster.objects[key] != before.get(key)}
    assert changed == {("model-a-serving", "configmaps", "lens-instance-glm")}
    record = inventory.require(Kube(runner=cluster), "model-a-serving", "glm")
    assert record["management"] == {"mode": "imported", "manager": "kubectl"}
    assert all(item["uid"] for item in record["objects"])


def test_an_imported_stack_reports_its_health(cbrepo, cluster):
    deployed_with_oc_apply(cbrepo, cluster)
    import_stack(Kube(runner=cluster), import_plan(cbrepo, cluster), manager="kubectl")
    cluster.get("model-a-serving", "deployments.apps", "model-a")["status"] = {
        "replicas": 1,
        "readyReplicas": 1,
        "updatedReplicas": 1,
    }
    status = instance_status(Kube(runner=cluster), "model-a-serving", "glm")
    assert status["health"] == "Ready" and status["pods"]["ready"] == status["pods"]["total"] == 1
    assert status["management"]["mode"] == "imported"


def test_privileged_findings_do_not_block_an_import(cbrepo, cluster):
    deployment = cbrepo / "manifests/production/model-a/base/model-server/deployment.yaml"
    deployment.write_text(
        deployment.read_text().replace(
            "        - name: vllm\n", "        - name: vllm\n          securityContext: {privileged: true}\n"
        )
    )
    deployed_with_oc_apply(cbrepo, cluster)
    plan = import_plan(cbrepo, cluster)
    assert not plan.blocking
    assert [f.severity for f in plan.findings if f.code == "privileged"] == ["info"]


def test_an_import_needs_every_object_to_run(cbrepo, cluster):
    deployed_with_oc_apply(cbrepo, cluster)
    cluster.objects.pop(("model-a-serving", "services", "model-a"))
    plan = import_plan(cbrepo, cluster)
    with pytest.raises(NotFoundError, match="1 object"):
        import_stack(Kube(runner=cluster), plan, manager="kubectl")
    assert compare(Kube(runner=cluster), plan)[0] == {
        "object": "Service/model-a",
        "state": "missing",
        "differences": [],
    }


def test_drift_ignores_server_defaults_and_finds_real_changes(cbrepo, cluster):
    deployed_with_oc_apply(cbrepo, cluster)
    plan = import_plan(cbrepo, cluster)
    assert {item["state"] for item in compare(Kube(runner=cluster), plan)} == {"matches"}
    cluster.get("model-a-serving", "deployments.apps", "model-a")["spec"]["replicas"] = 4
    report = {item["object"]: item for item in compare(Kube(runner=cluster), plan)}
    assert report["Deployment/model-a"]["differences"] == ["spec.replicas"]
    assert drift({"a": [1, {"b": 2}]}, {"a": [1, {"b": 3}], "c": 1}) == ["a[1].b"]


def test_an_imported_instance_is_never_stood_up_or_imported_twice(cbrepo, cluster):
    deployed_with_oc_apply(cbrepo, cluster)
    kube = Kube(runner=cluster)
    import_stack(kube, import_plan(cbrepo, cluster), manager="helm")
    with pytest.raises(ConflictError, match="already recorded"):
        import_stack(kube, import_plan(cbrepo, cluster), manager="helm")
    stack = find_stack(resolve_source(str(cbrepo)), STACK)
    standup_plan = make_plan(stack, Options(namespace="model-a-serving", instance="glm"), runner=cluster)
    preflight(kube, standup_plan)
    assert {f.code for f in standup_plan.blocking} == {"instance-imported"}
    with pytest.raises(PolicyError):
        lifecycle.standup(kube, standup_plan)


def test_teardown_of_an_imported_stack(cbrepo, cluster):
    deployed_with_oc_apply(cbrepo, cluster)
    kube = Kube(runner=cluster)
    import_stack(kube, import_plan(cbrepo, cluster), manager="kubectl")
    assert lifecycle.teardown(kube, "model-a-serving", "glm", forget=True)["forgot"]
    assert cluster.get("model-a-serving", "deployments.apps", "model-a") is not None
    import_stack(kube, import_plan(cbrepo, cluster), manager="kubectl")
    result = lifecycle.teardown(kube, "model-a-serving", "glm")
    assert sorted(result["deleted"]) == ["Deployment/model-a", "Service/model-a"]


def test_import_command(cbrepo, cluster, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("llmd.commands.RUNNER", cluster)
    deployed_with_oc_apply(cbrepo, cluster)
    base = ["--state-dir", str(tmp_path), "import", STACK, "--source", str(cbrepo), "-n", "model-a-serving"]
    assert main([*base, "--name", "glm", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "matches   Deployment/model-a" in out
    assert inventory.read(Kube(runner=cluster), "model-a-serving", "glm") is None
    assert main(["-o", "json", *base, "--name", "glm"]) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == 2
