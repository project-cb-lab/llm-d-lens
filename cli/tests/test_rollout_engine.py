"""In-place rollouts end to end on the test cluster, which rolls like lws v0.9 and holds a
scale-down until the stack's drain tool announced the group (like curvebender-tools' guards)."""

import json
import sys

import pytest

from llmd import inventory
from llmd.cli import main
from llmd.errors import ConflictError, NotReadyError, UsageError
from llmd.kube import Kube
from llmd.rollouts import engine
from llmd.rollouts.plan import plan_rollout

from .test_rollout_plan import DS, NS, POOL, STACK, cluster, new_image, rollrepo, stack_plan  # noqa: F401

#: A stand-in for the stack's drain tool: it runs until stopped and announces nothing itself; the
#: tests decide what the guards admit.
DRAIN = f"{sys.executable} -c 'import time; time.sleep(600)' {{role}} {{old_revision}} {{max_unavailable}}"


def rollout(repo, cluster):  # noqa: F811
    return plan_rollout(Kube(runner=cluster), stack_plan(repo, cluster, "import"), floors={}, pool_label=POOL)


def started(rollrepo, cluster, tmp_path, drain=DRAIN):  # noqa: F811
    new_image(rollrepo)
    planned = rollout(rollrepo, cluster)
    return planned, engine.start(Kube(runner=cluster), planned, drain=drain, state=tmp_path)


@pytest.fixture(autouse=True)
def stop_drainers(tmp_path):
    yield
    engine.Drainers(tmp_path / "rollouts" / NS / "pd").stop()


def test_start_applies_only_the_change_and_starts_one_drainer_per_role(rollrepo, cluster, tmp_path):  # noqa: F811
    before_service = json.dumps(cluster.get(NS, "services", "model-p"), sort_keys=True)
    planned, result = started(rollrepo, cluster, tmp_path)
    assert result["applied"] == ["DisaggregatedSet/model-p"] and result["rolls"] == "model-p"
    assert [item["role"] for item in result["drainers"]] == ["prefill", "decode"]
    drainer = result["drainers"][1]["command"]
    assert drainer.endswith(f"decode {planned.before.target} 1")
    assert json.dumps(cluster.get(NS, "services", "model-p"), sort_keys=True) == before_service
    live = cluster.get(NS, DS, "model-p")
    assert (
        "kubectl.kubernetes.io/last-applied-configuration" in live["metadata"]["annotations"]
    )  # client-side
    assert "lens.llm-d.ai/instance" not in json.dumps(live)
    record = inventory.require(Kube(runner=cluster), NS, "pd")
    assert (
        record["rollout"]["phase"] == "Rolling" and record["rollout"]["fromRevision"] == planned.before.target
    )


def test_a_roll_needs_a_drain_command_or_an_explicit_no_drain(rollrepo, cluster, tmp_path):  # noqa: F811
    new_image(rollrepo)
    with pytest.raises(UsageError, match="need a drain command"):
        engine.start(Kube(runner=cluster), rollout(rollrepo, cluster), drain="", state=tmp_path)
    result = engine.start(Kube(runner=cluster), rollout(rollrepo, cluster), drain="-", state=tmp_path)
    assert result["drainers"] == []


def test_the_roll_waits_on_the_guards_and_finishes_once_the_old_revision_is_gone(rollrepo, cluster, tmp_path):  # noqa: F811
    planned, _ = started(rollrepo, cluster, tmp_path)
    kube = Kube(runner=cluster)
    cluster.refuse_scale_down = lambda lws, replicas: False  # nothing drained yet
    cluster.roll_step(NS, "model-p")
    status = engine.status(kube, NS, "pd", state=tmp_path, pool_label=POOL)
    assert status["progress"]["rolling"] and not status["done"]
    assert len(status["drainers"]["running"]) == 2
    with pytest.raises(NotReadyError, match="not finished rolling"):
        engine.finish(kube, NS, "pd", state=tmp_path, pool_label=POOL)
    cluster.refuse_scale_down = None  # the drainers announce each old group as they drain it
    while cluster.roll_step(NS, "model-p"):
        pass
    status = engine.status(kube, NS, "pd", state=tmp_path, pool_label=POOL)
    assert status["done"] and status["progress"]["target"] != planned.before.target
    assert (
        engine.watch(kube, NS, "pd", state=tmp_path, pool_label=POOL, timeout=5, interval=0)["phase"]
        == "Finished"
    )
    assert inventory.require(kube, NS, "pd")["rollout"]["phase"] == "Finished"
    assert status["drainers"]["running"] and not engine.Drainers(tmp_path / "rollouts" / NS / "pd").running()


def test_pause_stops_the_drainers_and_resume_starts_them_again(rollrepo, cluster, tmp_path):  # noqa: F811
    started(rollrepo, cluster, tmp_path)
    kube = Kube(runner=cluster)
    assert sorted(engine.pause(kube, NS, "pd", state=tmp_path)["stopped"]) == ["decode", "prefill"]
    assert inventory.require(kube, NS, "pd")["rollout"]["phase"] == "Paused"
    assert len(engine.resume(kube, NS, "pd", state=tmp_path)["drainers"]) == 2
    with pytest.raises(ConflictError, match="already run"):
        engine.resume(kube, NS, "pd", state=tmp_path)


def test_watch_stops_when_the_drainers_exit_early(rollrepo, cluster, tmp_path):  # noqa: F811
    started(rollrepo, cluster, tmp_path, drain=f"{sys.executable} -c pass {{role}}")
    cluster.refuse_scale_down = lambda lws, replicas: False
    cluster.roll_step(NS, "model-p")
    kube = Kube(runner=cluster)
    with pytest.raises(NotReadyError, match="drainers exited"):
        engine.watch(kube, NS, "pd", state=tmp_path, pool_label=POOL, timeout=10, interval=0.2)


def test_abort_restores_the_previous_template(rollrepo, cluster, tmp_path):  # noqa: F811
    planned, _ = started(rollrepo, cluster, tmp_path)
    kube = Kube(runner=cluster)
    cluster.roll_step(NS, "model-p")
    result = engine.abort(kube, NS, "pd", server_side=False)
    assert result["restored"] == ["DisaggregatedSet/model-p"]
    assert cluster.revision(cluster.get(NS, DS, "model-p")) == planned.before.target
    while cluster.roll_step(NS, "model-p"):
        pass
    assert engine.finish(kube, NS, "pd", state=tmp_path, pool_label=POOL)["phase"] == "Aborted"


def test_one_rollout_at_a_time_and_a_reviewed_digest(rollrepo, cluster, tmp_path):  # noqa: F811
    new_image(rollrepo)
    planned = rollout(rollrepo, cluster)
    with pytest.raises(ConflictError, match="changed since it was reviewed"):
        engine.start(Kube(runner=cluster), planned, drain="-", state=tmp_path, expected_digest="0" * 64)
    engine.start(Kube(runner=cluster), planned, drain="-", state=tmp_path)
    with pytest.raises(ConflictError, match="already has a rollout in progress"):
        rollout(rollrepo, cluster)


def test_rollout_commands(rollrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr("llmd.commands.RUNNER", cluster)
    new_image(rollrepo)
    state = ["--state-dir", str(tmp_path)]
    target = [STACK, "--source", str(rollrepo), "-n", NS, "--name", "pd"]
    assert main([*state, "rollouts", "start", *target]) == 2  # no --drain
    assert "drain command" in capsys.readouterr().err
    assert main([*state, "rollouts", "start", *target, "--drain", DRAIN]) == 0
    assert "model-p is rolling" in capsys.readouterr().out
    assert main([*state, "rollouts", "status", "pd", "-n", NS]) == 0
    assert "rollout Rolling" in capsys.readouterr().out
    assert main([*state, "rollouts", "pause", "pd", "-n", NS]) == 0
    while cluster.roll_step(NS, "model-p"):
        pass
    assert main([*state, "rollouts", "finish", "pd", "-n", NS]) == 0
    assert "finished at revision" in capsys.readouterr().out


def test_a_privileged_imported_stack_rolls_through_the_cli(rollrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    """GLM-5.3 production runs privileged pods with hostPath mounts; an imported stack's findings
    describe what runs and do not block its rollout."""
    path = rollrepo / "manifests/production/model-p/base/disaggregatedset.yaml"
    text = path.read_text().replace(
        "                - name: vllm\n",
        "                - name: vllm\n                  securityContext: {privileged: true}\n",
    )
    path.write_text(text)
    git_commit(rollrepo)
    monkeypatch.setattr("llmd.commands.RUNNER", cluster)
    state = ["--state-dir", str(tmp_path)]
    target = [STACK, "--source", str(rollrepo), "-n", NS, "--name", "pd"]
    assert main([*state, "rollouts", "plan", *target]) == 0
    assert "Result ready to start" in capsys.readouterr().out
    assert main([*state, "rollouts", "start", *target, "--no-drain"]) == 0


def git_commit(repo):
    from .conftest import git

    git(repo, "commit", "-q", "-am", "privileged")


def test_a_drain_command_with_another_context_is_refused(rollrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr("llmd.commands.RUNNER", cluster)
    new_image(rollrepo)
    argv = ["--state-dir", str(tmp_path), "rollouts", "start", STACK, "--source", str(rollrepo), "-n", NS]
    assert main([*argv, "--name", "pd", "--context", "other", "--drain", DRAIN]) == 2
    assert "use-context other" in capsys.readouterr().err


def test_an_instance_llmd_stood_up_rolls_with_server_side_apply(rollrepo, tmp_path, capsys, monkeypatch):  # noqa: F811
    from .fakes import FakeCluster

    fresh = FakeCluster(namespaces=("team-a",))
    monkeypatch.setattr("llmd.commands.RUNNER", fresh)
    state = ["--state-dir", str(tmp_path)]
    target = [STACK, "--source", str(rollrepo), "-n", "team-a", "--name", "pd"]
    assert main([*state, "standup", *target]) == 0
    new_image(rollrepo)
    assert main([*state, "rollouts", "plan", *target]) == 0
    assert "model-p: roll" in capsys.readouterr().out
    assert main([*state, "rollouts", "start", *target, "--no-drain"]) == 0
    live = fresh.get("team-a", DS, "model-p")
    assert live["metadata"]["labels"]["lens.llm-d.ai/instance"] == "pd"  # still llmd's
    assert "kubectl.kubernetes.io/last-applied-configuration" not in live["metadata"].get("annotations", {})
    while fresh.roll_step("team-a", "model-p"):
        pass
    assert main([*state, "rollouts", "finish", "pd", "-n", "team-a"]) == 0
    assert main([*state, "teardown", "pd", "-n", "team-a", "-y"]) == 0
    assert fresh.get("team-a", DS, "model-p") is None
