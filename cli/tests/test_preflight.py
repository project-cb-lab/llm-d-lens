import json

import pytest

from llmd.catalog import find_stack
from llmd.cli import main
from llmd.commands import plan as plan_command
from llmd.kube import Kube
from llmd.plan import Options, make_plan
from llmd.policy import PolicyContext, apply_policy
from llmd.preflight import preflight
from llmd.sources import resolve_source

from .fakes import FakeCluster

STACK = "manifests/production/model-a/cluster-a"


@pytest.fixture
def cluster():
    return FakeCluster()


def prerequisites(cluster):
    cluster.put("team-a", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "hf-token"}})
    cluster.put("team-a", {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "model-a"}})
    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "model-a-cache"}}
    cluster.put("team-a", claim)


def checked(cbrepo, cluster, namespace="team-a"):
    stack = find_stack(resolve_source(str(cbrepo)), STACK)
    plan = make_plan(stack, Options(namespace=namespace, instance="r1"), runner=cluster)
    preflight(Kube(runner=cluster), plan)
    return plan


def codes(plan, severity="error"):
    return sorted({finding.code for finding in plan.findings if finding.severity == severity})


def test_a_ready_namespace_passes_and_nothing_changes(cbrepo, cluster):
    prerequisites(cluster)
    before = dict(cluster.objects)
    plan = checked(cbrepo, cluster)
    assert not plan.blocking
    assert plan.cluster["dryRun"] == "passed"
    assert plan.cluster["objects"] == {"create": 2, "update": 0, "foreign": 0}
    assert cluster.objects == before
    assert all(item["state"] == "present" for item in plan.policy.prerequisites)


def test_missing_prerequisites_block_before_any_change(cbrepo, cluster):
    plan = checked(cbrepo, cluster)
    assert codes(plan) == ["prerequisite-missing"]
    assert plan.cluster["dryRun"] == "skipped"
    missing = {item["name"] for item in plan.policy.prerequisites if item["state"] == "missing"}
    assert missing == {"hf-token", "model-a", "model-a-cache"}


def test_a_missing_scrape_secret_warns_and_does_not_block(cbrepo, cluster):
    """The stack serves without it; only its metrics stay empty. A Secret a pod also needs still blocks."""
    prerequisites(cluster)
    cluster.serve("monitoring.coreos.com/v1", "PodMonitor", "podmonitors")
    plan = checked(cbrepo, cluster)
    monitor = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {"name": "model-a", "namespace": "team-a"},
        "spec": {
            "podMetricsEndpoints": [
                {"port": "metrics", "authorization": {"credentials": {"name": "scrape-token", "key": "t"}}},
                {"port": "other", "bearerTokenSecret": {"name": "hf-token", "key": "t"}},
            ]
        },
    }
    plan.policy = apply_policy([*plan.documents, monitor], PolicyContext(namespace="team-a", instance="r1"))
    preflight(Kube(runner=cluster), plan)
    assert not plan.blocking
    assert codes(plan, "warning") == ["monitor-secret-missing"]
    states = {item["name"]: item["state"] for item in plan.policy.prerequisites}
    assert states["scrape-token"] == "missing" and states["hf-token"] == "present"
    cluster.objects.pop(("team-a", "secrets", "hf-token"))
    plan.findings[:] = [finding for finding in plan.findings if finding.severity == "info"]
    preflight(Kube(runner=cluster), plan)
    assert codes(plan) == ["prerequisite-missing"]  # the pods need hf-token too


def test_a_missing_namespace_or_api_blocks(cbrepo, cluster):
    assert codes(checked(cbrepo, cluster, namespace="not-there")) == ["namespace-missing"]
    del cluster.served["apps/v1"]
    assert codes(checked(cbrepo, cluster)) == ["api-missing"]


def test_denied_permissions_are_reported(cbrepo, cluster):
    prerequisites(cluster)
    cluster.denied = {("create", "deployments.apps"), ("delete", "services")}
    plan = checked(cbrepo, cluster)
    assert codes(plan) == ["permission-denied"]
    assert codes(plan, "warning") == ["permission-delete"]


def test_an_object_another_tool_deployed_is_never_overwritten(cbrepo, cluster):
    prerequisites(cluster)
    cluster.put(
        "team-a", {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "model-a"}, "spec": {}}
    )
    plan = checked(cbrepo, cluster)
    assert codes(plan) == ["object-exists"]
    assert "llmd import" in next(f.hint for f in plan.findings if f.code == "object-exists")
    assert plan.cluster["objects"]["foreign"] == 1


def test_discovery_decides_what_is_cluster_scoped(cbrepo, cluster):
    overlay = cbrepo / STACK
    (overlay / "widget.yaml").write_text(
        "apiVersion: example.invalid/v1\nkind: Widget\nmetadata: {name: w}\n"
    )
    kustomization = overlay / "kustomization.yaml"
    kustomization.write_text(kustomization.read_text().replace("- ../base", "- ../base\n  - widget.yaml"))
    cluster.serve("example.invalid/v1", "Widget", "widgets", namespaced=False)
    stack = find_stack(resolve_source(str(cbrepo)), STACK)
    offline = make_plan(stack, Options(namespace="team-a", instance="r1"), runner=cluster)
    assert "cluster-scoped" not in codes(offline)  # unknown offline, so it stays in the plan
    scope = plan_command._discovered_scope(Kube(runner=cluster))
    online = make_plan(stack, Options(namespace="team-a", instance="r1"), runner=cluster, scope=scope)
    assert "cluster-scoped" in codes(online)


def test_plan_command_against_a_cluster(cbrepo, cluster, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("llmd.commands.RUNNER", cluster)
    prerequisites(cluster)
    argv = ["--state-dir", str(tmp_path), "-o", "json", "plan", STACK, "--source", str(cbrepo)]
    assert main(argv) == 0  # the namespace comes from the kube context
    summary = json.loads(capsys.readouterr().out)
    assert summary["namespace"] == "team-a" and summary["cluster"]["dryRun"] == "passed"
    assert summary["cluster"]["user"] == "alice"
