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


def warning(pod, reason, message, at, kind="Pod", type_="Warning"):
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {"name": f"{pod}.{reason}.{at}"},
        "involvedObject": {"kind": kind, "name": pod},
        "type": type_,
        "reason": reason,
        "message": message,
        "lastTimestamp": f"2026-10-29T18:{at}:00Z",
    }


def test_a_pending_pod_with_no_other_reason_shows_its_latest_warning(cbrepo, cluster):
    kube = stood_up(cbrepo, cluster)
    pod = cluster.get("team-a", "pods", "model-a-0")
    pod["status"] = {
        "phase": "Pending",
        "conditions": [{"type": "PodScheduled", "status": "True"}],
        "containerStatuses": [{"name": "vllm", "state": {"waiting": {"reason": "ContainerCreating"}}}],
    }
    mount = "MountVolume.SetUp failed for volume model-cache: hostPath type check failed"
    for event in (
        warning("model-a-0", "FailedScheduling", "0/3 nodes are available", "01"),
        warning("model-a-0", "FailedMount", mount, "02"),
        warning("model-a-0", "FailedScheduling", "an old one, listed last", "00"),
        warning("model-a-0", "Pulled", "a normal event", "03", type_="Normal"),
        warning("model-a-1", "FailedMount", "another pod", "04"),
    ):
        cluster.put("team-a", event)
    status = instance_status(kube, "team-a", "r1")
    assert status["pods"]["problems"] == [
        {"pod": "model-a-0", "reason": "FailedMount", "message": mount, "lastSeen": "2026-10-29T18:02:00Z"}
    ]
    pod["status"]["containerStatuses"][0]["state"]["waiting"] = {"reason": "ImagePullBackOff", "message": "x"}
    calls = len(cluster.calls)
    [problem] = instance_status(kube, "team-a", "r1")["pods"]["problems"]
    assert problem["reason"] == "ImagePullBackOff" and "log" not in problem
    assert not [call for call in cluster.calls[calls:] if call[:2] == ["get", "events"]]


def test_status_without_permission_to_read_events_shows_what_the_pods_say(cbrepo, cluster):
    kube = stood_up(cbrepo, cluster)
    pod = cluster.get("team-a", "pods", "model-a-0")
    pod["status"] = {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "True"}]}
    cluster.denied.add(("list", "events"))
    status = instance_status(kube, "team-a", "r1")
    assert status["pods"]["problems"] == []


def crashing(cluster, name="model-a-0"):
    pod = cluster.get("team-a", "pods", name)
    pod["status"] = {
        "phase": "Running",
        "conditions": [{"type": "PodScheduled", "status": "True"}],
        "containerStatuses": [
            {
                "name": "vllm",
                "restartCount": 3,
                "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 1m20s"}},
                "lastState": {"terminated": {"exitCode": 1, "reason": "Error"}},
            }
        ],
    }
    return pod


def test_a_crashing_container_shows_its_exit_code_and_last_lines(
    cbrepo, cluster, tmp_path, capsys, monkeypatch
):
    kube = stood_up(cbrepo, cluster)
    crashing(cluster)
    error = (
        "huggingface_hub.errors.HFValidationError: Repo id must be in the form 'repo_name' or "
        "'namespace/repo_name': '/model-cache/models/model-a'. Use `repo_type` argument if needed."
    )
    cluster.previous_logs[("model-a-0", "vllm")] = "\n".join(
        [*(f"  frame {n}" for n in range(30)), "", error]
    )
    [problem] = instance_status(kube, "team-a", "r1")["pods"]["problems"]
    assert problem["container"] == "vllm" and problem["exitCode"] == "1"
    assert problem["log"].splitlines() == [*(f"  frame {n}" for n in range(24, 30)), error]
    assert ["logs", "model-a-0", "-c", "vllm", "-n", "team-a", "--previous", "--tail=8"] in cluster.calls
    monkeypatch.setattr("llmd.commands.RUNNER", cluster)
    assert main(["--state-dir", str(tmp_path), "status", "r1"]) == 0
    out = capsys.readouterr().out
    assert "pod model-a-0 container vllm: CrashLoopBackOff, exit code 1 back-off 1m20s" in out
    assert "last lines before it exited:" in out and f"      {error}" in out


def test_crash_logs_are_read_once_per_workload_and_only_when_allowed(cbrepo, cluster):
    kube = stood_up(cbrepo, cluster)
    deployment = cluster.get("team-a", "deployments.apps", "model-a")
    deployment["spec"]["replicas"] = 2
    cluster.settle()
    crashing(cluster, "model-a-0")
    crashing(cluster, "model-a-1")
    cluster.previous_logs[("model-a-0", "vllm")] = "boom"
    cluster.previous_logs[("model-a-1", "vllm")] = "boom"
    calls = len(cluster.calls)
    problems = instance_status(kube, "team-a", "r1")["pods"]["problems"]
    assert [problem.get("log") for problem in problems] == ["boom", None]
    assert len([call for call in cluster.calls[calls:] if call[0] == "logs"]) == 1
    cluster.denied.add(("get", "pods/log"))
    problems = instance_status(kube, "team-a", "r1")["pods"]["problems"]
    assert [problem["reason"] for problem in problems] == ["CrashLoopBackOff"] * 2
    assert not any("log" in problem for problem in problems)


def test_a_viewer_who_cannot_read_some_kinds_still_sees_the_rest(
    cbrepo, cluster, tmp_path, capsys, monkeypatch
):
    kube = stood_up(cbrepo, cluster)
    cluster.denied.add(("get", "services"))
    status = instance_status(kube, "team-a", "r1")
    assert [(item["object"], item["state"]) for item in status["objects"]] == [
        ("Service/model-a", "unreadable"),
        ("Deployment/model-a", "ready"),
    ]
    assert status["objects"][0]["detail"] == "not allowed to read it"
    assert status["unreadable"] == ["Service/model-a"] and status["health"] == "Unknown"
    assert status["pods"]["ready"] == 1 and status["endpoints"] == []
    [item] = list_instances(kube, "team-a")
    assert item["unreadable"] == ["Service/model-a"] and item["pods"] == {"ready": 1, "total": 1}

    cluster.denied = {("get", "pods")}
    status = instance_status(kube, "team-a", "r1")
    assert [item["state"] for item in status["objects"]] == ["present", "ready"]
    assert status["unreadable"] == ["pods"] and status["health"] == "Unknown"
    assert status["pods"]["problems"] == [
        {"pod": "*", "reason": "PodsUnreadable", "message": "not allowed to read it"}
    ]

    monkeypatch.setattr("llmd.commands.RUNNER", cluster)
    cluster.denied = {("get", "services")}
    assert main(["--state-dir", str(tmp_path), "status", "r1"]) == 0
    out = capsys.readouterr().out
    assert "r1 in team-a: Unknown" in out and "unreadable   Service/model-a  not allowed to read it" in out
    assert "could not read 1 part(s) with your permissions in team-a" in out
