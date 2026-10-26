import pytest

from llmd import inventory, lifecycle
from llmd.cli import main
from llmd.errors import PolicyError

from .test_status import cluster, stood_up  # noqa: F401 - the cluster fixture

CLAIM = {
    "apiVersion": "v1",
    "kind": "PersistentVolumeClaim",
    "name": "cache",
    "resource": "persistentvolumeclaims",
}


def test_teardown_removes_exactly_what_the_instance_owns(cbrepo, cluster):  # noqa: F811
    kube = stood_up(cbrepo, cluster)
    cluster.put("team-a", {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "unrelated"}})
    result = lifecycle.teardown(kube, "team-a", "r1")
    assert result["deleted"] == ["Deployment/model-a", "Service/model-a"]
    assert cluster.get("team-a", "deployments.apps", "model-a") is None
    assert cluster.names("team-a", "pods") == []  # gone with their Deployment
    assert cluster.get("team-a", "configmaps", "unrelated") is not None
    assert inventory.read(kube, "team-a", "r1") is None


def test_claims_are_kept_unless_asked_for(cbrepo, cluster):  # noqa: F811
    kube = stood_up(cbrepo, cluster)
    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "cache"}}
    stored = cluster.put("team-a", claim)
    stored["metadata"]["labels"] = {
        "app.kubernetes.io/managed-by": "llm-d-lens",
        "lens.llm-d.ai/instance": "r1",
    }
    record = inventory.require(kube, "team-a", "r1")
    objects = [*record["objects"], {**CLAIM, "uid": stored["metadata"]["uid"]}]
    inventory.write(kube, {**record, "objects": objects})
    result = lifecycle.teardown(kube, "team-a", "r1")
    assert result["kept"] == ["PersistentVolumeClaim/cache"]
    assert inventory.require(kube, "team-a", "r1")["phase"] == "Removed"
    assert lifecycle.teardown(kube, "team-a", "r1", delete_volumes=True)["deleted"] == [
        "PersistentVolumeClaim/cache"
    ]
    assert inventory.read(kube, "team-a", "r1") is None


def test_an_object_replaced_or_taken_over_is_skipped(cbrepo, cluster):  # noqa: F811
    kube = stood_up(cbrepo, cluster)
    cluster.get("team-a", "services", "model-a")["metadata"]["uid"] = "recreated"
    cluster.get("team-a", "deployments.apps", "model-a")["metadata"]["labels"] = {}
    result = lifecycle.teardown(kube, "team-a", "r1")
    assert result["deleted"] == [] and sorted(result["skipped"]) == ["Deployment/model-a", "Service/model-a"]
    assert cluster.get("team-a", "services", "model-a") is not None


def test_teardown_checks_delete_permission_before_anything(cbrepo, cluster):  # noqa: F811
    kube = stood_up(cbrepo, cluster)
    cluster.denied = {("delete", "deployments.apps")}
    with pytest.raises(PolicyError, match="nothing was removed"):
        lifecycle.teardown(kube, "team-a", "r1")
    assert inventory.require(kube, "team-a", "r1")["phase"] == "Applied"


def test_forget_leaves_every_object(cbrepo, cluster):  # noqa: F811
    kube = stood_up(cbrepo, cluster)
    assert lifecycle.teardown(kube, "team-a", "r1", forget=True)["forgot"]
    assert cluster.get("team-a", "deployments.apps", "model-a") is not None
    assert inventory.read(kube, "team-a", "r1") is None


def test_teardown_command_asks_unless_yes(cbrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr("llmd.commands.RUNNER", cluster)
    stood_up(cbrepo, cluster)
    state = ["--state-dir", str(tmp_path)]
    assert main([*state, "teardown", "r1"]) == 2  # no terminal to confirm
    assert main([*state, "teardown", "r1", "-y"]) == 0
    assert "Removed 2 object(s) of r1" in capsys.readouterr().out
