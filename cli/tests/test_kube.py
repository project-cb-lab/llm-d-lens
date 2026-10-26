import pytest

from llmd.errors import CommandError, NotFoundError
from llmd.kube import Kube, Target, guess_plural

from .fakes import FakeCluster

CONFIGMAP = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a"}, "data": {"k": "v"}}


@pytest.fixture
def cluster():
    return FakeCluster()


@pytest.fixture
def kube(cluster):
    return Kube(Target(context="ctx"), runner=cluster)


def test_every_call_names_the_context(kube, cluster):
    kube.get("configmaps", "a", "team-a")
    assert cluster.calls[-1][:2] == ["get", "configmaps"] and "-n" in cluster.calls[-1]


def test_discovery_gives_scope_and_plural(kube):
    assert kube.resource_name({"apiVersion": "apps/v1", "kind": "Deployment"}) == "deployments.apps"
    assert kube.scope("v1", "PersistentVolume") is False
    assert kube.scope("v1", "Service") is True
    assert kube.scope("example.invalid/v1", "Widget") is None
    assert kube.resource_name({"apiVersion": "example.invalid/v1", "kind": "NetworkPolicy"}) == (
        "networkpolicies.example.invalid"
    )


def test_guess_plural():
    assert [guess_plural(kind) for kind in ("Ingress", "NetworkPolicy", "Route")] == [
        "ingresses",
        "networkpolicies",
        "routes",
    ]


def test_server_side_apply_and_client_side_apply(kube, cluster):
    kube.apply([CONFIGMAP], "team-a")
    stored = cluster.get("team-a", "configmaps", "a")
    assert "kubectl.kubernetes.io/last-applied-configuration" not in stored["metadata"].get("annotations", {})
    assert "--server-side" in cluster.calls[-1]
    kube.apply([CONFIGMAP], "team-a", server_side=False)
    assert "--server-side" not in cluster.calls[-1]
    stored = cluster.get("team-a", "configmaps", "a")
    assert "kubectl.kubernetes.io/last-applied-configuration" in stored["metadata"]["annotations"]


def test_a_dry_run_changes_nothing(kube, cluster):
    kube.apply([CONFIGMAP], "team-a", dry_run=True)
    assert cluster.get("team-a", "configmaps", "a") is None
    assert "--dry-run=server" in cluster.calls[-1]


def test_a_refused_apply_is_a_command_error(kube, cluster):
    cluster.denied.add(("create", "configmaps"))
    with pytest.raises(CommandError, match="Forbidden"):
        kube.apply([CONFIGMAP], "team-a")


def test_access_reviews(kube, cluster):
    cluster.denied.add(("delete", "deployments.apps"))
    checks = [("create", "", "configmaps", "team-a"), ("delete", "apps", "deployments", "team-a")]
    assert kube.access_reviews(checks) == [True, False]


def test_reads_and_deletes(kube, cluster):
    cluster.put("team-a", {**CONFIGMAP, "metadata": {"name": "a", "labels": {"app": "x"}}})
    assert [item["metadata"]["name"] for item in kube.get_json("configmaps", "team-a", selector="app=x")] == [
        "a"
    ]
    assert kube.get_json("configmaps", "team-a", selector="app=y") == []
    assert kube.require("configmaps", "a", "team-a")["metadata"]["uid"]
    assert kube.delete("configmaps", "a", "team-a") is True
    assert kube.delete("configmaps", "a", "team-a") is False
    with pytest.raises(NotFoundError):
        kube.require("configmaps", "a", "team-a")


def test_identity(kube):
    assert kube.whoami() == "alice"
    assert kube.current_context() == "ctx"
    assert kube.namespace_state("team-a") == "exists" and kube.namespace_state("other") == "missing"
