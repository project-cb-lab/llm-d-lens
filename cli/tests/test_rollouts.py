import copy
import json
import shutil

import pytest
from conftest import FIXTURES, commit

from rollouts import testing
from rollouts.cli import main, parser
from rollouts.common import VERSION_LABEL, RolloutError, documents, read, run, write
from rollouts.prepare import content_hash, prepare, resolve_ref
from rollouts.testing import functional_job, live_preflight, model_ready, wait_job
from rollouts.validate import check_isolation, matches, validate


def test_prepare_snapshots_refs_and_ignores_dirty_checkout(repo, tmp_path):
    sha = run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    path = repo / "deployments/model/practice/router/router.values.yaml"
    path.write_text("invalid: [")
    root, state = prepare(repo, to_ref=sha, model="model", environment="practice", output=tmp_path / "out")
    assert state["noOp"]
    assert state["versions"]["v0"]["ref"] == "main"
    assert read(root / "v1/source/router/router.values.yaml")["router"]
    with pytest.raises(RolloutError, match="already exists"):
        prepare(repo, to_ref=sha, model="model", environment="practice", output=root)


def test_content_hash_survives_yaml_comments_order_and_pr_merge(prepared):
    root, state = prepared
    content = read(root / "v0/inputs/content.yaml")
    reordered = dict(reversed(list(content.items())))
    reordered = copy.deepcopy(reordered)
    configs = reordered["components"]["router"]["values"]["router"]["epp"]["pluginsCustomConfig"]
    configs["plugins.yaml"] = "# different comment\nkind: EndpointPickerConfig\n"
    assert content_hash(reordered) == state["versions"]["v0"]["sha256"]
    reordered["components"]["modelserver"]["spec"]["roles"][0]["spec"]["replicas"] = 2
    assert content_hash(reordered) != state["versions"]["v0"]["sha256"]


def test_pr_resolution_fetches_head_and_refreshes(repo, tmp_path):
    remote = tmp_path / "remote.git"
    run(["git", "clone", "--bare", repo, remote])
    old = run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    run(["git", "update-ref", "refs/pull/7/head", old], cwd=remote)
    assert resolve_ref(repo, "pr-7", target=True, remote=str(remote)) == old
    (repo / "README.md").write_text("new head")
    new = commit(repo, "PR update")
    run(["git", "push", remote, f"{new}:refs/pull/7/head"], cwd=repo)
    assert resolve_ref(repo, "pr-7", target=True, remote=str(remote)) == new


@pytest.mark.parametrize("target", ["HEAD", "main", "pr-0", "--help"])
def test_to_rejects_unsupported_refs(repo, target):
    with pytest.raises(RolloutError, match="commit SHA"):
        resolve_ref(repo, target, target=True)


def test_real_render_isolates_router_only_change(prepared):
    root, state = prepared
    _, docs = validate(root)
    assert state["versions"]["v0"]["label"] != state["versions"]["v1"]["label"]
    for name, resources in docs.items():
        label = state["versions"][name]["label"]
        model = next(doc for doc in resources if doc["kind"] == "DisaggregatedSet")
        for role in model["spec"]["roles"]:
            for key in ("workerTemplate", "leaderTemplate"):
                assert role["spec"]["leaderWorkerTemplate"][key]["metadata"]["labels"][VERSION_LABEL] == label
        pool = next(doc for doc in resources if doc["kind"] == "InferencePool")
        assert pool["spec"]["selector"]["matchLabels"][VERSION_LABEL] == label
        deployment = next(doc for doc in resources if doc["kind"] == "Deployment")
        configmap = next(doc for doc in resources if doc["kind"] == "ConfigMap")
        assert (
            deployment["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"]
            == configmap["metadata"]["name"]
        )
    check_isolation(docs["v0"], docs["v1"])
    assert read(root / "validation.yaml")["versions"]["v1"]["chartSha256"]


def test_noop_validation_and_test_rejection(repo, tmp_path):
    if not shutil.which("helm"):
        pytest.skip("requires Helm")
    old = run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    path = repo / "deployments/model/practice/router/router.values.yaml"
    path.write_text(
        "# comment only\n" + path.read_text().replace("# a comment", "# changed embedded comment")
    )
    new = commit(repo, "comment only")
    root, _ = prepare(
        repo, from_ref=old, to_ref=new, model="model", environment="practice", output=tmp_path / "out"
    )
    for slot in ("v0", "v1"):
        shutil.copytree(FIXTURES / "router", root / slot / "charts/router-1.0.0/router")
    state, _ = validate(root)
    assert state["noOp"]
    with pytest.raises(RolloutError, match="identical effective"):
        testing.test_rollout(root)


@pytest.mark.parametrize("file", ["kustomization.yaml", "inputs/model-server.yaml", "inputs/content.yaml"])
def test_generated_tampering_is_rejected(prepared, file):
    root, _ = prepared
    path = root / "v1" / file
    value = read(path)
    value["edited"] = True
    write(path, value)
    with pytest.raises(RolloutError, match="edited|hash mismatch"):
        validate(root)


def test_chart_drift_is_rejected(prepared):
    root, _ = prepared
    validate(root)
    path = root / "v1/charts/router-1.0.0/router/Chart.yaml"
    path.write_text(path.read_text() + "description: changed\n")
    with pytest.raises(RolloutError, match="cached chart changed"):
        validate(root)


def test_source_selector_mismatch_is_reported(repo, tmp_path):
    file = repo / "deployments/model/practice/router/router.values.yaml"
    value = read(file)
    value["router"]["modelServers"]["matchLabels"]["missing"] = "true"
    write(file, value)
    sha = commit(repo, "selector mismatch")
    with pytest.raises(RolloutError, match="pod labels do not match"):
        prepare(repo, to_ref=sha, model="model", environment="practice", output=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_snapshot_rejects_symlinks(repo, tmp_path):
    (repo / "deployments/model/practice/link").symlink_to("/etc/passwd")
    sha = commit(repo, "symlink")
    with pytest.raises(RolloutError, match="unsupported Git entry"):
        prepare(repo, to_ref=sha, model="model", environment="practice", output=tmp_path / "out")


def test_duplicate_yaml_keys_fail():
    with pytest.raises(RolloutError, match="duplicate YAML key"):
        documents("router: {}\nrouter: {}")


def test_cli_defaults_and_error(repo, capsys):
    args = parser().parse_args(
        ["prepare", "--repo", str(repo), "--to", "abcdef0", "--model", "m", "--environment", "e"]
    )
    assert args.from_ref == "main"
    assert main(["prepare", "--repo", str(repo), "--to", "HEAD", "--model", "m", "--environment", "e"]) == 1
    assert "commit SHA" in capsys.readouterr().err


def test_cross_version_selection_and_name_collision(prepared):
    root, _ = prepared
    _, docs = validate(root)
    bad = copy.deepcopy(docs["v1"])
    pool = next(doc for doc in bad if doc["kind"] == "InferencePool")
    pool["spec"]["selector"] = {"matchLabels": {"app": "model"}}
    with pytest.raises(RolloutError, match="other version"):
        check_isolation(docs["v0"], bad)
    with pytest.raises(RolloutError, match="names collide"):
        check_isolation(docs["v0"], docs["v0"])


def test_plan_never_contacts_cluster(prepared, monkeypatch):
    root, _ = prepared

    def forbidden(*args, **kwargs):
        pytest.fail("cluster command used during planning")

    monkeypatch.setattr(testing, "run", forbidden)
    result = testing.test_rollout(root)
    assert result["versionsToDeploy"] == ["v1"]
    assert not result["apply"]


def test_legacy_pool_is_rejected_before_any_write(prepared, monkeypatch):
    root, state = prepared
    _, docs = validate(root)
    legacy = {"metadata": {"name": "legacy"}, "spec": {"selector": {"matchLabels": {"app": "model"}}}}
    monkeypatch.setattr(testing, "run", lambda *a, **k: json.dumps({"items": [legacy]}))
    with pytest.raises(RolloutError, match="isolate existing pools"):
        live_preflight(["kubectl"], root, state, docs, bootstrap_v0=False)


def test_job_receives_candidate_target_and_unique_name(prepared, tmp_path):
    root, state = prepared
    _, docs = validate(root)
    path = tmp_path / "job.yaml"
    write(
        path,
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "tests"},
            "spec": {
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "tests",
                                "image": "example.invalid/tests:v1",
                                "env": [{"name": "ROLLOUT_ROUTER_URL", "value": "old"}],
                            }
                        ],
                    }
                }
            },
        },
    )
    job = functional_job(path, state, docs["v1"])
    env = {entry["name"]: entry["value"] for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert state["versions"]["v1"]["release"] in env["ROLLOUT_ROUTER_URL"]
    assert env["ROLLOUT_VERSION"] == state["versions"]["v1"]["label"]
    assert "name" not in job["metadata"]
    assert job["metadata"]["generateName"].endswith("-test-")


def test_job_failure_is_not_a_success(monkeypatch):
    monkeypatch.setattr(
        testing,
        "run",
        lambda *a, **k: json.dumps(
            {"status": {"conditions": [{"type": "Failed", "status": "True", "reason": "test failed"}]}}
        ),
    )
    with pytest.raises(RolloutError, match="failed"):
        wait_job(["kubectl"], "functional-tests", 30)


def test_readiness_requires_all_role_pods(prepared):
    root, _ = prepared
    _, docs = validate(root)
    model = next(doc for doc in docs["v1"] if doc["kind"] == "DisaggregatedSet")
    pods = []
    for role in model["spec"]["roles"]:
        labels = role["spec"]["leaderWorkerTemplate"]["workerTemplate"]["metadata"]["labels"]
        for _ in range(2):
            pods.append(
                {
                    "metadata": {"labels": labels},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            )
    assert model_ready(docs["v1"], pods)
    assert not model_ready(docs["v1"], pods[:-1])
    assert not model_ready(docs["v1"], [])


def test_selector_expressions():
    assert matches({"matchExpressions": [{"key": "x", "operator": "NotIn", "values": ["y"]}]}, {})
    assert not matches({"matchExpressions": [{"key": "x", "operator": "In", "values": ["y"]}]}, {})


@pytest.mark.parametrize("fail_at", [None, "admission", "readiness", "job"])
def test_live_workflow_orders_writes_and_records_failures(prepared, tmp_path, monkeypatch, fail_at):
    root, _ = prepared
    path = tmp_path / "job.yaml"
    write(
        path,
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "spec": {
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [{"name": "test", "image": "example.invalid/tests:v1"}],
                    }
                }
            },
        },
    )
    events = []

    def preflight(*args, **kwargs):
        events.append("preflight")

    def ready(*args, **kwargs):
        events.append("readiness")
        if fail_at == "readiness":
            raise RolloutError("readiness failed")

    def job(*args, **kwargs):
        events.append("job")
        if fail_at == "job":
            raise RolloutError("job failed")

    def command(argv, **kwargs):
        argv = list(map(str, argv))
        if "--dry-run=server" in argv:
            events.append("admission")
            if fail_at == "admission":
                raise RolloutError("admission failed")
            return ""
        if "apply" in argv:
            assert argv[-1].endswith("rendered/v1.yaml")
            events.append("apply-v1")
            return ""
        assert "create" in argv
        events.append("create-job")
        return json.dumps({"metadata": {"name": "test-123"}})

    monkeypatch.setattr(testing, "live_preflight", preflight)
    monkeypatch.setattr(testing, "wait_ready", ready)
    monkeypatch.setattr(testing, "wait_job", job)
    monkeypatch.setattr(testing, "run", command)
    if fail_at:
        with pytest.raises(RolloutError, match=f"{fail_at} failed"):
            testing.test_rollout(root, apply=True, context="practice", job_path=path)
        assert read(root / "test-result.yaml")["status"] == "failed"
        if fail_at == "admission":
            assert "apply-v1" not in events and "create-job" not in events
        if fail_at == "readiness":
            assert "create-job" not in events
    else:
        result = testing.test_rollout(root, apply=True, context="practice", job_path=path)
        assert result["status"] == "passed"
        assert events == ["preflight", "admission", "admission", "apply-v1", "readiness", "create-job", "job"]


def test_chart_pin_is_required(repo, tmp_path):
    config = repo / "deployments/model/practice/rollout-config.yaml"
    value = read(config)
    del value["router"]["chart"]["version"]
    write(config, value)
    sha = commit(repo, "missing chart pin")
    with pytest.raises(RolloutError, match="pin router.chart.version"):
        prepare(repo, to_ref=sha, model="model", environment="practice", output=tmp_path / "out")


def test_environment_cannot_escape_deployments(repo, tmp_path):
    sha = run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    with pytest.raises(RolloutError, match="path segment"):
        prepare(repo, to_ref=sha, model="../bad", environment="practice", output=tmp_path / "out")
