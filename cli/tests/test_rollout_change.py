import copy

from llmd.model import object_ref
from llmd.rollouts.change import compare_objects, set_change


def disaggregated_set(image="v1", prefill=4, decode=3, prefill_unavailable=2):
    def role(name, replicas, unavailable):
        return {
            "name": name,
            "spec": {
                "replicas": replicas,
                "rolloutStrategy": {
                    "type": "RollingUpdate",
                    "rollingUpdateConfiguration": {"maxSurge": 0, "maxUnavailable": unavailable},
                },
                "leaderWorkerTemplate": {
                    "size": 2,
                    "workerTemplate": {"spec": {"containers": [{"name": "vllm", "image": f"vllm:{image}"}]}},
                },
            },
        }

    return {
        "apiVersion": "disaggregatedset.x-k8s.io/v1",
        "kind": "DisaggregatedSet",
        "metadata": {"name": "model-p"},
        "spec": {"roles": [role("prefill", prefill, prefill_unavailable), role("decode", decode, 1)]},
    }


def test_a_template_change_rolls_every_group():
    change = set_change(disaggregated_set(), disaggregated_set(image="v2"))
    assert change.kind == "roll"
    assert [(role.name, role.max_unavailable, role.template_changed) for role in change.roles] == [
        ("prefill", 2, True),
        ("decode", 1, True),
    ]
    # Roles step together; decode replaces one group per step, so it sets the count.
    assert change.steps() == 3
    assert change.serving_during() == {"prefill": 2, "decode": 2}


def test_replicas_alone_scale_without_a_roll():
    change = set_change(disaggregated_set(), disaggregated_set(decode=4))
    assert change.kind == "scale" and change.steps() == 0
    assert change.roles[1].replicas == (3, 4)


def test_the_rollout_strategy_is_not_part_of_the_revision():
    change = set_change(disaggregated_set(), disaggregated_set(prefill_unavailable=1))
    assert change.kind == "strategy" and change.steps() == 0
    assert change.differences == [
        "spec.roles[0].spec.rolloutStrategy.rollingUpdateConfiguration.maxUnavailable"
    ]


def test_server_defaults_are_not_a_change():
    live = disaggregated_set()
    live["metadata"].update(uid="u", resourceVersion="9", generation=3)
    live["status"] = {"replicas": 7}
    template = live["spec"]["roles"][0]["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]
    template["restartPolicy"] = "Always"  # defaulted by the API server
    assert set_change(live, disaggregated_set()).kind == "none"


def test_percentages_follow_kubernetes_rounding():
    desired = disaggregated_set(prefill=7)
    desired["spec"]["roles"][0]["spec"]["rolloutStrategy"]["rollingUpdateConfiguration"]["maxUnavailable"] = (
        "30%"
    )
    assert set_change(disaggregated_set(prefill=7), desired).roles[0].max_unavailable == 2  # floor(2.1)


def test_compare_objects_lists_what_changes_and_never_deletes():
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "model-p"},
        "spec": {"ports": [{"port": 1}]},
    }
    old_config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "old"}}
    new_config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "new"}}
    running = [disaggregated_set(), service, old_config]
    live = {object_ref(doc): copy.deepcopy(doc) for doc in running}
    desired = [disaggregated_set(image="v2"), service, new_config]
    change = compare_objects({**live, "ConfigMap/new": None}, desired, list(live))
    assert change.objects == {
        "DisaggregatedSet/model-p": "changed",
        "Service/model-p": "same",
        "ConfigMap/new": "new",
    }
    assert change.left_alone == ["ConfigMap/old"]
    assert change.roll.name == "model-p"
    assert [object_ref(doc) for doc in change.changed_documents(desired)] == [
        "DisaggregatedSet/model-p",
        "ConfigMap/new",
    ]
