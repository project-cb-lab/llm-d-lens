import json

import pytest

from llmd import health, lifecycle
from llmd.cli import main
from llmd.errors import NotReadyError
from llmd.kube import Kube
from llmd.status import instance_status, list_instances, wait_ready

from .fakes import FakeCluster
from .test_lifecycle import plan_for
from .test_preflight import STACK, prerequisites


@pytest.fixture
def cluster():
    cluster = FakeCluster()
    prerequisites(cluster)
    return cluster


def stood_up(cbrepo, cluster):
    lifecycle.standup(Kube(runner=cluster), plan_for(cbrepo, cluster))
    deployment = cluster.get("team-a", "deployments.apps", "model-a")
    deployment["status"] = {"replicas": 1, "readyReplicas": 1, "updatedReplicas": 1}
    return Kube(runner=cluster)


def test_status_finds_pods_through_the_workload_selector(cbrepo, cluster):
    kube = stood_up(cbrepo, cluster)
    status = instance_status(kube, "team-a", "r1")
    assert status["health"] == "Ready"
    assert status["pods"] == {"ready": 1, "total": 1, "problems": []}
    assert [item["state"] for item in status["objects"]] == ["present", "ready"]
    assert status["endpoints"] == ["http://model-a.team-a.svc.cluster.local:8000"]
    pod = cluster.get("team-a", "pods", "model-a-0")
    assert "lens.llm-d.ai/instance" not in pod["metadata"]["labels"]


def test_pod_problems_and_replaced_objects(cbrepo, cluster):
    kube = stood_up(cbrepo, cluster)
    pod = cluster.get("team-a", "pods", "model-a-0")
    pod["status"] = {
        "phase": "Pending",
        "conditions": [
            {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/3 nodes"}
        ],
    }
    status = instance_status(kube, "team-a", "r1")
    assert status["pods"]["problems"] == [
        {"pod": "model-a-0", "reason": "Unschedulable", "message": "0/3 nodes"}
    ]
    service = cluster.get("team-a", "services", "model-a")
    service["metadata"]["uid"] = "someone-else"
    status = instance_status(kube, "team-a", "r1")
    assert status["objects"][0]["state"] == "replaced" and status["health"] == "Degraded"


def test_a_leader_elected_endpoint_picker_is_ready_with_one_replica_ready():
    """Only the lease holder passes the EPP's readiness check; the standby never does."""
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "epp", "generation": 2},
        "spec": {
            "replicas": 2,
            "template": {
                "spec": {
                    "containers": [
                        {"name": "epp", "args": ["--pool-name", "p", "--ha-enable-leader-election"]}
                    ]
                }
            },
        },
        "status": {"observedGeneration": 2, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 1},
    }
    assert health.evaluate(deployment, [])[0] == "ready"
    deployment["status"]["readyReplicas"] = 0
    assert health.evaluate(deployment, [])[0] == "progressing"
    deployment["status"].update(readyReplicas=1, updatedReplicas=1)  # an update still rolling
    assert health.evaluate(deployment, [])[0] == "progressing"
    deployment["status"]["updatedReplicas"] = 2
    deployment["spec"]["template"]["spec"]["containers"][0]["args"].pop()
    assert health.evaluate(deployment, [])[0] == "progressing"  # without leader election, all count


def test_a_disaggregated_set_is_judged_by_its_pods():
    item = {
        "kind": "DisaggregatedSet",
        "metadata": {"name": "model-p"},
        "spec": {"roles": [{"name": "decode", "spec": {"replicas": 1, "leaderWorkerTemplate": {"size": 2}}}]},
    }
    pod = {
        "metadata": {"labels": {"disaggregatedset.x-k8s.io/role": "decode"}},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    assert health.evaluate(item, [pod, pod]) == ("ready", "decode 2/2 ready")
    assert health.evaluate(item, [pod])[0] == "progressing"


def test_stood_up_with_a_leader_elected_endpoint_picker_the_instance_is_ready(cbrepo, cluster):
    """The instance is Ready although the EPP's standby pod never is."""
    kube = stood_up(cbrepo, cluster)
    deployment = cluster.get("team-a", "deployments.apps", "model-a")
    deployment["spec"]["replicas"] = 2
    deployment["spec"]["template"]["spec"]["containers"][0]["args"] = ["--ha-enable-leader-election"]
    deployment["status"] = {"replicas": 2, "readyReplicas": 1, "updatedReplicas": 2}
    assert wait_ready(kube, "team-a", "r1", timeout=0, interval=0)["health"] == "Ready"


def test_wait_returns_once_ready_and_times_out_otherwise(cbrepo, cluster):
    kube = stood_up(cbrepo, cluster)
    assert wait_ready(kube, "team-a", "r1", timeout=1, interval=0)["health"] == "Ready"
    cluster.get("team-a", "deployments.apps", "model-a")["status"] = {}
    with pytest.raises(NotReadyError, match="not ready after"):
        wait_ready(kube, "team-a", "r1", timeout=0, interval=0)


def test_list_counts_pods_and_gpus(cbrepo, cluster):
    kube = stood_up(cbrepo, cluster)
    [item] = list_instances(kube, "team-a")
    assert item["instance"] == "r1" and item["management"] == "llmd"
    assert item["pods"] == {"ready": 1, "total": 1}
    assert item["accelerators"] == {"nvidia.com/gpu": 1}


def test_status_and_list_commands(cbrepo, cluster, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("llmd.commands.RUNNER", cluster)
    stood_up(cbrepo, cluster)
    state = ["--state-dir", str(tmp_path)]
    assert main([*state, "status", "r1"]) == 0
    assert "r1 in team-a: Ready (pods 1/1 ready)" in capsys.readouterr().out
    assert main([*state, "-o", "json", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["instances"][0]["stack"] == STACK
    assert main([*state, "status", "missing"]) == 6


def test_pods_refused_at_creation_fail_the_deployment():
    refused = {
        "type": "ReplicaFailure",
        "status": "True",
        "reason": "FailedCreate",
        "message": 'pods "m-1" is forbidden: unable to validate against any security context constraint',
    }
    deployment = {
        "kind": "Deployment",
        "metadata": {"generation": 1},
        "spec": {"replicas": 2},
        "status": {"observedGeneration": 1, "conditions": [refused]},
    }
    state, detail = health.evaluate(deployment, [])
    assert state == "failed" and detail.startswith("FailedCreate: pods") and "security context" in detail
