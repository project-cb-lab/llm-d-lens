import copy

from llmd.model import INSTANCE_LABEL, MANAGED_BY_LABEL
from llmd.policy import PolicyContext, apply_policy, pod_templates


def deployment(name="model", namespace="written-for", **pod_spec):
    spec = {
        "serviceAccountName": "model-sa",
        "containers": [
            {
                "name": "vllm",
                "image": "registry.example.invalid/vllm:v1",
                "args": ["--epp", f"epp.{namespace}.svc.cluster.local:9002", "-n", namespace],
                "env": [{"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "hf-token", "key": "t"}}}],
                "resources": {"limits": {"nvidia.com/gpu": "2"}},
            }
        ],
        "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": "model-cache"}}],
        **pod_spec,
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"replicas": 3, "template": {"metadata": {"labels": {"app": name}}, "spec": spec}},
    }


def disaggregated_set():
    def role(name, replicas):
        template = {
            "metadata": {"labels": {"llm-d.ai/role": name}},
            "spec": {
                "containers": [{"name": "vllm", "image": "v", "resources": {"limits": {"nvidia.com/gpu": 8}}}]
            },
        }
        return {
            "name": name,
            "spec": {"replicas": replicas, "leaderWorkerTemplate": {"size": 4, "workerTemplate": template}},
        }

    return {
        "apiVersion": "disaggregatedset.x-k8s.io/v1",
        "kind": "DisaggregatedSet",
        "metadata": {"name": "model-pd"},
        "spec": {"roles": [role("prefill", 2), role("decode", 3)]},
    }


def codes(result, severity="error"):
    return sorted({finding.code for finding in result.findings if finding.severity == severity})


def test_objects_move_into_the_target_namespace_and_references_follow():
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "epp", "namespace": "written-for"},
        "roleRef": {"kind": "Role", "name": "epp"},
        "subjects": [{"kind": "ServiceAccount", "name": "epp", "namespace": "written-for"}],
    }
    route = {
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": {"name": "api", "namespace": "written-for"},
        "spec": {"host": "api-written-for.apps.example.invalid"},
    }
    result = apply_policy([deployment(), binding, route], PolicyContext(namespace="team-a", instance="r1"))
    assert {doc["metadata"]["namespace"] for doc in result.documents} == {"team-a"}
    args = next(doc for doc in result.documents if doc["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert args["containers"][0]["args"] == ["--epp", "epp.team-a.svc.cluster.local:9002", "-n", "team-a"]
    rolebinding = next(doc for doc in result.documents if doc["kind"] == "RoleBinding")
    assert rolebinding["subjects"][0]["namespace"] == "team-a"
    assert "host" not in next(doc for doc in result.documents if doc["kind"] == "Route")["spec"]
    assert result.source_namespaces == ["written-for"]
    assert {"namespace-rewritten", "route-host-dropped"} <= set(codes(result, "info"))


def test_a_monitor_follows_the_stack_into_the_target_namespace():
    """A PodMonitor that still named the old namespace would scrape nothing where the stack runs."""
    monitor = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {"name": "epp", "namespace": "written-for"},
        "spec": {
            "namespaceSelector": {"matchNames": ["written-for", "shared-metrics"]},
            "selector": {"matchLabels": {"app": "epp"}},
            "podMetricsEndpoints": [{"port": "metrics"}],
        },
    }
    result = apply_policy([monitor], PolicyContext(namespace="team-a", instance="r1"))
    [moved] = result.documents
    assert moved["spec"]["namespaceSelector"]["matchNames"] == ["team-a", "shared-metrics"]
    assert monitor["spec"]["namespaceSelector"]["matchNames"] == ["written-for", "shared-metrics"]


def test_a_stack_deployed_where_it_was_written_for_is_unchanged():
    original = deployment(namespace="team-a")
    result = apply_policy([original], PolicyContext(namespace="team-a", instance="r1", label=False))
    assert result.documents == [original] and not result.source_namespaces


def test_cluster_scoped_objects_and_scc_bindings_are_for_an_administrator():
    volume = {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "cache"}, "spec": {}}
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "written-for"}}
    scc = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "system:openshift:scc:privileged"},
        "roleRef": {"kind": "ClusterRole", "name": "system:openshift:scc:privileged"},
        "subjects": [{"kind": "ServiceAccount", "name": "model-sa"}],
    }
    context = PolicyContext(namespace="team-a", instance="r1")
    result = apply_policy([volume, namespace, scc], context)
    assert result.documents == []
    assert [part["reason"] for part in result.admin_parts] == ["cluster-scoped", "scc-binding"]
    assert codes(result) == ["cluster-scoped", "scc-binding"]
    assert "namespace-dropped" in codes(result, "info")

    skipped = apply_policy(
        [volume, scc], PolicyContext(namespace="team-a", instance="r1", skip_admin_parts=True)
    )
    assert (
        skipped.documents == [] and not codes(skipped) and codes(skipped, "warning") == ["admin-part-skipped"]
    )

    allowed = apply_policy([scc], PolicyContext(namespace="team-a", instance="r1", allow_privileged=True))
    assert [doc["kind"] for doc in allowed.documents] == ["RoleBinding"] and not codes(allowed)

    # An administrator applied the binding; the team acknowledges the privileged pods it admits.
    both = apply_policy(
        [scc], PolicyContext(namespace="team-a", instance="r1", allow_privileged=True, skip_admin_parts=True)
    )
    assert both.documents == [] and codes(both, "warning") == ["admin-part-skipped"]


def test_privileged_pods_and_host_paths_block_unless_acknowledged():
    risky = deployment(hostNetwork=True)
    container = risky["spec"]["template"]["spec"]["containers"][0]
    container["securityContext"] = {"privileged": True, "capabilities": {"add": ["IPC_LOCK"]}}
    risky["spec"]["template"]["spec"]["volumes"].append(
        {"name": "dev", "hostPath": {"path": "/dev/infiniband"}}
    )
    blocked = apply_policy([risky], PolicyContext(namespace="team-a", instance="r1"))
    assert codes(blocked) == ["host-namespace", "host-path", "privileged"]
    assert codes(blocked, "warning") == ["capabilities"]
    allowed = apply_policy([risky], PolicyContext(namespace="team-a", instance="r1", allow_privileged=True))
    assert not codes(allowed) and {"host-path", "privileged"} <= set(codes(allowed, "warning"))
    assert all(finding.hint for finding in blocked.findings if finding.severity == "error")
    acknowledged = [finding for finding in allowed.findings if finding.code in ("host-path", "privileged")]
    assert acknowledged and not any(finding.hint for finding in acknowledged)


def test_what_the_stack_uses_but_does_not_ship_is_a_prerequisite():
    pull = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "model-sa"},
        "imagePullSecrets": [{"name": "pull-secret"}],
    }
    pd = disaggregated_set()
    pd["spec"]["roles"][0]["spec"]["leaderWorkerTemplate"]["workerTemplate"]["metadata"]["annotations"] = {
        "k8s.v1.cni.cncf.io/networks": "port-1,port-2,other-ns/shared"
    }
    result = apply_policy([deployment(), pull, pd], PolicyContext(namespace="team-a", instance="r1"))
    assert [(item["kind"], item["name"]) for item in result.prerequisites] == [
        ("NetworkAttachmentDefinition", "port-1"),
        ("NetworkAttachmentDefinition", "port-2"),
        ("PersistentVolumeClaim", "model-cache"),
        ("Secret", "hf-token"),
        ("Secret", "pull-secret"),
    ]


def test_a_secret_a_monitor_scrapes_with_is_a_prerequisite():
    """Without it the scrape fails, and a dashboard over those metrics stays empty."""

    def selector(name, optional=False):
        return {"name": name, "key": "k", **({"optional": True} if optional else {})}

    pods = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {"name": "epp"},
        "spec": {
            "podMetricsEndpoints": [
                {"port": "metrics", "authorization": {"credentials": selector("scrape-token")}},
                {
                    "port": "tls",
                    "tlsConfig": {"ca": {"secret": selector("scrape-ca")}, "keySecret": selector("k", True)},
                },
            ]
        },
    }
    services = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {"name": "proxy"},
        "spec": {
            "endpoints": [
                {
                    "port": "a",
                    "basicAuth": {"username": selector("scrape-user"), "password": selector("scrape-user")},
                },
                {"port": "b", "bearerTokenSecret": selector("legacy-token")},
                {
                    "port": "c",
                    "oauth2": {"clientId": {"configMap": selector("id")}, "clientSecret": selector("oauth")},
                },
            ]
        },
    }
    shipped = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "scrape-ca"}}
    result = apply_policy([pods, services, shipped], PolicyContext(namespace="team-a", instance="r1"))
    assert [(item["name"], item["usedBy"]) for item in result.prerequisites] == [
        ("legacy-token", ["ServiceMonitor/proxy"]),
        ("oauth", ["ServiceMonitor/proxy"]),
        ("scrape-token", ["PodMonitor/epp"]),
        ("scrape-user", ["ServiceMonitor/proxy"]),
    ]


def test_accelerators_count_every_pod_of_every_group():
    result = apply_policy(
        [deployment(), disaggregated_set()], PolicyContext(namespace="team-a", instance="r1")
    )
    # 3 pods x 2 GPUs, plus (2 + 3 groups) x 4 pods x 8 GPUs.
    assert result.accelerators == {"nvidia.com/gpu": 6 + 160}
    assert [item["template"] for item in result.workloads] == [
        "template",
        "roles[prefill].workerTemplate",
        "roles[decode].workerTemplate",
    ]


def test_labels_go_on_object_metadata_never_on_pod_templates():
    original = disaggregated_set()
    result = apply_policy([copy.deepcopy(original)], PolicyContext(namespace="team-a", instance="r1"))
    labelled = result.documents[0]
    assert labelled["metadata"]["labels"] == {MANAGED_BY_LABEL: "llm-d-lens", INSTANCE_LABEL: "r1"}
    templates = [template for _, template, _ in pod_templates(labelled)]
    assert templates == [template for _, template, _ in pod_templates(original)]


def test_duplicates_are_refused_and_the_apply_order_puts_workloads_last():
    service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "model"}, "spec": {}}
    account = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "model-sa"}}
    result = apply_policy(
        [deployment(), service, account, service], PolicyContext(namespace="team-a", instance="r1")
    )
    assert [doc["kind"] for doc in result.documents] == ["ServiceAccount", "Service", "Deployment"]
    assert codes(result) == ["duplicate-object"]
