import copy

import pytest

from llmd.kube import Kube
from llmd.rollouts.progress import read_progress

from .fakes import FakeCluster
from .test_rollout_change import disaggregated_set

NS = "model-p-serving"
POOL = "ops.llm-d.ai/production-released"


@pytest.fixture
def cluster():
    cluster = FakeCluster(namespaces=(NS,))
    document = disaggregated_set()
    for role in document["spec"]["roles"]:
        role["spec"]["leaderWorkerTemplate"]["workerTemplate"]["metadata"] = {"labels": {POOL: "true"}}
    cluster.put(NS, document)
    cluster.settle()
    return cluster


def live(cluster):
    return cluster.get(NS, "disaggregatedsets.disaggregatedset.x-k8s.io", "model-p")


def change_template(cluster, image="v2"):
    document = copy.deepcopy(live(cluster))
    for role in document["spec"]["roles"]:
        role["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]["containers"][0]["image"] = (
            f"vllm:{image}"
        )
    cluster.put(NS, document)


def test_a_settled_set_has_one_revision(cluster):
    progress = read_progress(Kube(runner=cluster), NS, live(cluster), pool_label=POOL)
    assert not progress.rolling and progress.done()
    assert [(role.role, role.replicas, role.serving()) for role in progress.roles] == [
        ("prefill", 4, 4),
        ("decode", 3, 3),
    ]


def test_a_roll_moves_groups_from_the_old_revision_to_the_new(cluster):
    old = read_progress(Kube(runner=cluster), NS, live(cluster)).target
    change_template(cluster)
    assert cluster.roll_step(NS, "model-p")
    progress = read_progress(Kube(runner=cluster), NS, live(cluster), pool_label=POOL)
    assert progress.rolling and progress.revisions[0] == old and progress.target != old
    summary = progress.describe()["roles"]
    assert summary["prefill"]["byRevision"] == {
        old: {"groups": 2, "serving": 2, "outOfPool": 0},
        progress.target: {"groups": 2, "serving": 2, "outOfPool": 0},
    }
    assert summary["decode"]["byRevision"][progress.target]["groups"] == 1
    while cluster.roll_step(NS, "model-p"):
        pass
    progress = read_progress(Kube(runner=cluster), NS, live(cluster), pool_label=POOL)
    assert not progress.rolling and progress.done() and progress.target != old


def test_a_group_out_of_the_pool_is_not_serving(cluster):
    pod = next(item for key, item in cluster.objects.items() if key[1] == "pods" and "-prefill-" in key[2])
    pod["metadata"]["labels"][POOL] = "false"
    progress = read_progress(Kube(runner=cluster), NS, live(cluster), pool_label=POOL)
    assert progress.roles[0].serving() == 3 and not progress.done()
    assert read_progress(Kube(runner=cluster), NS, live(cluster)).roles[0].serving() == 4  # no pool label


def test_a_refused_scale_down_holds_the_roll(cluster):
    change_template(cluster)
    cluster.refuse_scale_down = lambda lws, replicas: False
    assert not cluster.roll_step(NS, "model-p")
    progress = read_progress(Kube(runner=cluster), NS, live(cluster))
    assert progress.rolling and all(len(role.by_revision()) == 2 for role in progress.roles)
    # The raised groups wait for nodes; every old group still serves.
    prefill = progress.roles[0].by_revision()
    assert [group.serving for group in prefill[progress.target]] == [False, False]
    assert progress.roles[0].serving() == 4
