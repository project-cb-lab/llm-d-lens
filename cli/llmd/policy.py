"""Turn rendered objects into what may be applied to one namespace, and say what is unsafe.

This is where the guarantees for a team without cluster-admin rights live:

* every applied object is namespaced and lands in the one target namespace; references to the
  namespace the stack was written for (Service DNS names, ``--namespace`` flags, RoleBinding
  subjects) are rewritten;
* cluster-scoped objects and RoleBindings to OpenShift SCCs are *administrator parts*: never
  applied, reported so an administrator can apply them;
* privileged containers, host namespaces and hostPath volumes block the plan unless the user
  acknowledges them (``--allow-privileged``): admission acts on Pods, which a server-side dry
  run of a Deployment does not create, so they are found here;
* objects the stack uses but does not ship (Secrets, claims, ConfigMaps, ServiceAccounts,
  network attachments, and the Secrets a monitor scrapes with) are prerequisites the namespace
  must already hold.

Pod templates are never changed: a label there would start a new DisaggregatedSet revision and
roll every group. Objects llmd creates carry its labels in their own metadata only.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .model import INSTANCE_LABEL, MANAGED_BY_LABEL, MANAGED_BY_VALUE, Finding, api_group, object_ref

POD_TEMPLATE_KEYS = ("template", "workerTemplate", "leaderTemplate")
MULTUS_ANNOTATION = "k8s.v1.cni.cncf.io/networks"
SCC_CLUSTER_ROLE_PREFIX = "system:openshift:scc:"
_SAFE_CAPABILITIES = {"NET_BIND_SERVICE"}
ACKNOWLEDGE = "Allowed only where an administrator granted it; acknowledge with --allow-privileged."

#: Used when API discovery is not available (offline plans); a cluster plan asks the API server.
CLUSTER_SCOPED = {
    ("", "Namespace"),
    ("", "Node"),
    ("", "PersistentVolume"),
    ("rbac.authorization.k8s.io", "ClusterRole"),
    ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
    ("apiextensions.k8s.io", "CustomResourceDefinition"),
    ("apiregistration.k8s.io", "APIService"),
    ("storage.k8s.io", "StorageClass"),
    ("storage.k8s.io", "CSIDriver"),
    ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicy"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicyBinding"),
    ("scheduling.k8s.io", "PriorityClass"),
    ("node.k8s.io", "RuntimeClass"),
    ("networking.k8s.io", "IngressClass"),
    ("gateway.networking.k8s.io", "GatewayClass"),
    ("security.openshift.io", "SecurityContextConstraints"),
    ("config.openshift.io", "*"),
    ("operator.openshift.io", "*"),
}
NAMESPACED_BUILTINS = {
    ("", kind)
    for kind in "ConfigMap Secret Service ServiceAccount PersistentVolumeClaim Pod Endpoints".split()
} | {
    ("apps", "Deployment"),
    ("apps", "StatefulSet"),
    ("apps", "DaemonSet"),
    ("batch", "Job"),
    ("batch", "CronJob"),
    ("rbac.authorization.k8s.io", "Role"),
    ("rbac.authorization.k8s.io", "RoleBinding"),
    ("networking.k8s.io", "NetworkPolicy"),
    ("networking.k8s.io", "Ingress"),
    ("policy", "PodDisruptionBudget"),
    ("autoscaling", "HorizontalPodAutoscaler"),
}
#: Applied first, in this order; workloads go last so what they use already exists.
APPLY_ORDER = [
    "ServiceAccount",
    "Secret",
    "ConfigMap",
    "PersistentVolumeClaim",
    "Role",
    "RoleBinding",
    "NetworkPolicy",
    "Service",
    "PodDisruptionBudget",
]
WORKLOAD_KINDS = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "Pod",
    "LeaderWorkerSet",
    "DisaggregatedSet",
}

#: Prometheus Operator monitors: they select pods in the namespaces their ``namespaceSelector`` names.
MONITOR_KINDS = {"PodMonitor", "ServiceMonitor"}
#: Where a monitor's endpoint names a Secret it scrapes with (a key of a Secret in its namespace),
#: as paths below each endpoint (monitoring.coreos.com/v1).
MONITOR_SECRETS = [
    ("authorization", "credentials"),
    ("basicAuth", "username"),
    ("basicAuth", "password"),
    ("bearerTokenSecret",),
    ("oauth2", "clientId", "secret"),
    ("oauth2", "clientSecret"),
    *(
        (*prefix, *path)
        for prefix in ((), ("oauth2",))
        for path in (
            ("tlsConfig", "ca", "secret"),
            ("tlsConfig", "cert", "secret"),
            ("tlsConfig", "keySecret"),
        )
    ),
]

#: (apiVersion, kind) -> True when namespaced, False when cluster-scoped, None when unknown.
ScopeResolver = Callable[[str, str], bool | None]


def fallback_scope(api_version: str, kind: str) -> bool | None:
    group = api_group(api_version)
    if (group, kind) in CLUSTER_SCOPED or (group, "*") in CLUSTER_SCOPED:
        return False
    if (group, kind) in NAMESPACED_BUILTINS:
        return True
    return None


@dataclass
class PolicyContext:
    namespace: str
    instance: str
    allow_privileged: bool = False
    #: Leave the administrator parts out instead of blocking on them (an administrator applied them).
    skip_admin_parts: bool = False
    #: Label objects as created by llmd. Off for an import: imported objects are never changed.
    label: bool = True
    scope: ScopeResolver = fallback_scope


@dataclass
class PolicyResult:
    documents: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    prerequisites: list[dict[str, Any]] = field(default_factory=list)
    workloads: list[dict[str, Any]] = field(default_factory=list)
    #: {"document", "reason"}: only an administrator may apply these. Never applied by llmd.
    admin_parts: list[dict[str, Any]] = field(default_factory=list)
    #: The namespaces the stack was written for, when it is deployed elsewhere.
    source_namespaces: list[str] = field(default_factory=list)
    accelerators: dict[str, int] = field(default_factory=dict)


# -- pod templates -----------------------------------------------------------------------------------
def _is_pod_template(value: Any) -> bool:
    return isinstance(value, dict) and isinstance((value.get("spec") or {}).get("containers"), list)


def pod_templates(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any], int | None]]:
    """(location, pod template, pods made from it) for every pod template in an object."""
    kind = document.get("kind")
    spec = document.get("spec") or {}
    if kind == "Pod":
        yield "pod", document, 1
    elif kind in ("Deployment", "StatefulSet", "ReplicaSet") and _is_pod_template(spec.get("template")):
        yield "template", spec["template"], int(spec.get("replicas", 1) or 0)
    elif kind == "Job" and _is_pod_template(spec.get("template")):
        yield "template", spec["template"], int(spec.get("parallelism", 1) or 1)
    elif kind == "LeaderWorkerSet":
        yield from _lws_templates("", spec, int(spec.get("replicas", 1) or 0))
    elif kind == "DisaggregatedSet":
        for index, role in enumerate(spec.get("roles") or []):
            role_spec = role.get("spec") or {}
            name = role.get("name") or str(index)
            yield from _lws_templates(f"roles[{name}].", role_spec, int(role_spec.get("replicas", 1) or 0))
    else:
        yield from _generic_templates("", spec)


def _lws_templates(prefix: str, spec: dict[str, Any], groups: int):
    template = spec.get("leaderWorkerTemplate") or {}
    size = int(template.get("size", 1) or 1)
    if _is_pod_template(template.get("leaderTemplate")):
        yield f"{prefix}leaderTemplate", template["leaderTemplate"], groups
        if _is_pod_template(template.get("workerTemplate")):
            yield f"{prefix}workerTemplate", template["workerTemplate"], groups * max(size - 1, 0)
    elif _is_pod_template(template.get("workerTemplate")):
        yield f"{prefix}workerTemplate", template["workerTemplate"], groups * size


def _generic_templates(prefix: str, value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in POD_TEMPLATE_KEYS and _is_pod_template(item):
                yield f"{prefix}{key}", item, None
            else:
                yield from _generic_templates(f"{prefix}{key}.", item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _generic_templates(f"{prefix}{index}.", item)


def containers(template: dict[str, Any]) -> list[dict[str, Any]]:
    spec = template.get("spec") or {}
    return [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]


def template_accelerators(template: dict[str, Any]) -> dict[str, int]:
    """Extended resources (``nvidia.com/gpu`` and the like) one pod asks for: the largest request or
    limit any container names."""
    found: dict[str, int] = {}
    for container in containers(template):
        resources = container.get("resources") or {}
        for scope in ("limits", "requests"):
            for resource, value in (resources.get(scope) or {}).items():
                if "/" in resource and not resource.startswith("hugepages"):
                    try:
                        found[resource] = max(found.get(resource, 0), int(str(value)))
                    except ValueError:
                        continue
    return found


# -- policy ------------------------------------------------------------------------------------------
def apply_policy(rendered: list[dict[str, Any]], context: PolicyContext) -> PolicyResult:
    result = PolicyResult()
    findings = result.findings
    kept: list[dict[str, Any]] = []
    #: The namespace each kept object was written for ("" when it named none), in ``kept`` order.
    origins: list[str] = []
    source_namespaces: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    for original in rendered:
        document = copy.deepcopy(original)
        metadata = document.get("metadata")
        if not document.get("apiVersion") or not document.get("kind") or not isinstance(metadata, dict):
            findings.append(
                Finding("error", "invalid-object", "an object has no apiVersion, kind or metadata")
            )
            continue
        ref = object_ref(document)
        if not metadata.get("name"):
            findings.append(
                Finding("error", "unnamed-object", f"{ref} has only generateName, so it cannot be tracked")
            )
            continue
        group, kind = api_group(document["apiVersion"]), document["kind"]
        identity = (group, kind, metadata["name"])
        if identity in seen:
            findings.append(Finding("error", "duplicate-object", f"{ref} is defined twice", object=ref))
            continue
        seen.add(identity)
        if context.scope(document["apiVersion"], kind) is False:
            if kind == "Namespace":
                findings.append(
                    Finding(
                        "info",
                        "namespace-dropped",
                        f"{ref} left out: the stack goes into {context.namespace}",
                    )
                )
            elif context.skip_admin_parts:
                findings.append(
                    Finding("warning", "admin-part-skipped", f"{ref} left out: an administrator applies it")
                )
            else:
                result.admin_parts.append({"document": document, "reason": "cluster-scoped"})
                findings.append(
                    Finding(
                        "error",
                        "cluster-scoped",
                        f"{ref} is cluster-scoped; only an administrator may apply it",
                        object=ref,
                        hint="Have an administrator apply it, then pass --skip-admin-parts.",
                    )
                )
            continue
        written_for = metadata.get("namespace")
        if written_for and written_for != context.namespace:
            source_namespaces.add(written_for)
        metadata["namespace"] = context.namespace
        kept.append(document)
        origins.append(written_for or "")

    rewrites = 0
    for old in sorted(source_namespaces):
        for index, document in enumerate(kept):
            kept[index], count = _rewrite_namespace_references(document, old, context.namespace)
            rewrites += count
    for document, written_for in zip(kept, origins, strict=True):
        moved = written_for and written_for != context.namespace
        if moved and document["kind"] == "Route" and document.get("spec", {}).get("host"):
            findings.extend(_drop_route_host(document, context.namespace))
    # A stack whose objects name several namespaces puts some of them where their readers look
    # (a dashboard in the Grafana instance's namespace, say); in the target namespace those
    # readers may not find them, so each such object is named. The stack's own namespace is the
    # one most of its objects name; moving those is the namespace move itself.
    named = [written_for for written_for in origins if written_for]
    home = max(sorted(set(named)), key=named.count) if named else ""
    if len(set(named)) > 1:
        for document, written_for in zip(kept, origins, strict=True):
            if written_for and written_for != home and written_for != context.namespace:
                ref = object_ref(document)
                findings.append(
                    Finding(
                        "warning",
                        "moved-from-other-namespace",
                        f"{ref} was written for namespace {written_for}, unlike the rest of the stack; "
                        f"it is applied in {context.namespace} instead, where what reads it in "
                        f"{written_for} may not find it",
                        object=ref,
                    )
                )
    if source_namespaces:
        result.source_namespaces = sorted(source_namespaces)
        findings.append(
            Finding(
                "info",
                "namespace-rewritten",
                f"objects moved from {', '.join(result.source_namespaces)} to {context.namespace}; "
                f"{rewrites} in-stack reference(s) rewritten",
            )
        )

    documents = []
    for document in kept:
        # --skip-admin-parts wins over --allow-privileged: an administrator applied the binding, and
        # the privileged pods it admits still need the acknowledgement.
        if _is_scc_binding(document) and context.skip_admin_parts:
            findings.append(
                Finding(
                    "warning",
                    "admin-part-skipped",
                    f"{object_ref(document)} left out: an administrator applies it",
                )
            )
            continue
        if _is_scc_binding(document) and not context.allow_privileged:
            result.admin_parts.append({"document": document, "reason": "scc-binding"})
            findings.append(_scc_finding(document))
            continue
        if context.label:
            labels = document["metadata"].setdefault("labels", {}) or {}
            document["metadata"]["labels"] = labels
            labels[MANAGED_BY_LABEL] = MANAGED_BY_VALUE
            labels[INSTANCE_LABEL] = context.instance
        documents.append(document)
    _analyze(documents, context, result)
    order = {kind: position for position, kind in enumerate(APPLY_ORDER)}
    result.documents = sorted(
        documents,
        key=lambda doc: order.get(doc["kind"], len(order) + (1 if doc["kind"] in WORKLOAD_KINDS else 0)),
    )
    return result


def _is_scc_binding(document: dict[str, Any]) -> bool:
    role_ref = document.get("roleRef") or {}
    return (
        document.get("kind") == "RoleBinding"
        and role_ref.get("kind") == "ClusterRole"
        and str(role_ref.get("name", "")).startswith(SCC_CLUSTER_ROLE_PREFIX)
    )


def _scc_finding(document: dict[str, Any]) -> Finding:
    ref = object_ref(document)
    scc = str((document.get("roleRef") or {}).get("name", "")).rsplit(":", 1)[-1]
    return Finding(
        "error",
        "scc-binding",
        f"{ref} grants the {scc} SCC, which needs permission to use that SCC",
        object=ref,
        hint="Have an administrator apply it, then pass --skip-admin-parts; if you hold that permission, "
        "pass --allow-privileged.",
    )


def _drop_route_host(document: dict[str, Any], namespace: str) -> list[Finding]:
    """A Route moved to another namespace loses its fixed host: the host belongs to the original
    deployment, and OpenShift assigns ``<name>-<namespace>.<apps domain>`` instead."""
    host = document["spec"].pop("host")
    ref = object_ref(document)
    return [
        Finding(
            "info",
            "route-host-dropped",
            f"{ref}: host {host} belongs to the namespace the stack was written for; OpenShift assigns "
            f"one in {namespace}",
            object=ref,
        )
    ]


def _walk_strings(value: Any, transform: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_walk_strings(item, transform) for item in value]
    if isinstance(value, dict):
        return {key: _walk_strings(item, transform) for key, item in value.items()}
    return value


def _rewrite_namespace_references(document: dict[str, Any], old: str, new: str) -> tuple[dict[str, Any], int]:
    """Rewrite in-stack references to ``old``: Service DNS names, ``namespace:``/``--namespace``
    settings, ``-n`` arguments, NAMESPACE variables, RoleBinding subjects, and the namespaces a
    monitor selects. Metadata is never rewritten, and plain words elsewhere are left alone."""
    count = 0
    service = re.compile(rf"(?<=\.){re.escape(old)}(?=\.svc\b)")
    setting = re.compile(rf"((?:[Nn]amespace)[\"']?\s*[:=]\s*[\"']?){re.escape(old)}\b")

    def transform(value: str) -> str:
        nonlocal count
        updated, hits = service.subn(new, value)
        updated, more = setting.subn(rf"\g<1>{new}", updated)
        count += hits + more
        return updated

    body = _walk_strings({key: value for key, value in document.items() if key != "metadata"}, transform)
    document = {
        "apiVersion": body.pop("apiVersion"),
        "kind": body.pop("kind"),
        "metadata": document["metadata"],
        **body,
    }
    for _, template, _ in pod_templates(document):
        for container in containers(template):
            for key in ("args", "command"):
                tokens = container.get(key)
                for position, token in enumerate(tokens if isinstance(tokens, list) else []):
                    previous = tokens[position - 1] if position else ""
                    if (
                        token == old
                        and isinstance(previous, str)
                        and (previous == "-n" or previous.endswith("namespace"))
                    ):
                        tokens[position] = new
                        count += 1
            for env in container.get("env") or []:
                if "NAMESPACE" in str(env.get("name", "")).upper() and env.get("value") == old:
                    env["value"] = new
                    count += 1
    if document["kind"] == "RoleBinding":
        for subject in document.get("subjects") or []:
            if subject.get("namespace") == old:
                subject["namespace"] = new
                count += 1
    if document["kind"] in MONITOR_KINDS:
        names = ((document.get("spec") or {}).get("namespaceSelector") or {}).get("matchNames")
        for position, name in enumerate(names if isinstance(names, list) else []):
            if name == old:
                names[position] = new
                count += 1
    return document, count


def _network_names(value: Any) -> list[str]:
    text = str(value).strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            return []
        return [
            (f"{item['namespace']}/" if item.get("namespace") else "") + str(item["name"])
            for item in items
            if isinstance(item, dict) and item.get("name")
        ]
    return [item.split("@", 1)[0].strip() for item in text.split(",") if item.strip()]


def monitor_secrets(document: dict[str, Any]) -> list[str]:
    """The Secrets a PodMonitor or ServiceMonitor scrapes with; an optional one is left out."""
    spec = document.get("spec") or {}
    endpoints = spec.get("podMetricsEndpoints") or spec.get("endpoints") or []
    names = []
    for endpoint in endpoints if isinstance(endpoints, list) else []:
        for path in MONITOR_SECRETS:
            value: Any = endpoint
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if isinstance(value, dict) and value.get("name") and not value.get("optional"):
                names.append(str(value["name"]))
    return names


def _analyze(documents: list[dict[str, Any]], context: PolicyContext, result: PolicyResult) -> None:
    """Safety findings, prerequisites, and the workload summary."""
    findings = result.findings
    severity = "warning" if context.allow_privileged else "error"
    # Once acknowledged, the findings stay as warnings, without the hint that asks for the flag.
    acknowledge = "" if context.allow_privileged else ACKNOWLEDGE
    provided = {(document["kind"], document["metadata"]["name"]) for document in documents}
    required: dict[tuple[str, str], set[str]] = {}

    def need(kind: str, name: str, user: str) -> None:
        if name and (kind, name) not in provided:
            required.setdefault((kind, name), set()).add(user)

    for document in documents:
        ref = object_ref(document)
        if document["kind"] == "ServiceAccount":
            for pull in document.get("imagePullSecrets") or []:
                need("Secret", pull.get("name", ""), ref)
        if document["kind"] == "RoleBinding":
            role_ref = document.get("roleRef") or {}
            if role_ref.get("kind") == "Role":
                need("Role", role_ref.get("name", ""), ref)
            if _is_scc_binding(document):
                findings.append(
                    Finding(severity, "scc-binding", f"{ref} grants an SCC (acknowledged)", object=ref)
                )
            for subject in document.get("subjects") or []:
                if subject.get("namespace") and subject["namespace"] != context.namespace:
                    findings.append(
                        Finding(
                            "warning",
                            "foreign-subject",
                            f"{ref} grants access to {subject.get('kind')} "
                            f"{subject['namespace']}/{subject.get('name')}",
                            object=ref,
                        )
                    )
        if document["kind"] in MONITOR_KINDS:
            for name in monitor_secrets(document):
                need("Secret", name, ref)
        volume_name = (document.get("spec") or {}).get("volumeName")
        if document["kind"] == "PersistentVolumeClaim" and volume_name:
            # PersistentVolumes are cluster-scoped: llmd neither creates nor reads them.
            findings.append(
                Finding(
                    "warning",
                    "claim-prebound",
                    f"{ref} is bound in advance to PersistentVolume {volume_name}, which only an "
                    "administrator creates; until it exists the claim stays Pending and the pods that "
                    "use it are not scheduled",
                    object=ref,
                    hint="Have an administrator apply the stack's PersistentVolume first. Teardown keeps "
                    "the claim unless --delete-volumes; deleting it leaves that PersistentVolume Released.",
                )
            )
        for location, template, pods in pod_templates(document):
            _analyze_template(document, location, template, pods, severity, acknowledge, need, result)
    for (kind, name), users in sorted(required.items()):
        if (kind, name) != ("ServiceAccount", "default"):
            result.prerequisites.append({"kind": kind, "name": name, "usedBy": sorted(users)})


def _analyze_template(
    document, location, template, pods, severity, acknowledge, need, result: PolicyResult
) -> None:
    findings = result.findings
    ref = object_ref(document)
    where = ref if location in ("template", "pod") else f"{ref} {location}"
    spec = template.get("spec") or {}
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(key):
            findings.append(
                Finding(severity, "host-namespace", f"{where} sets {key}", object=ref, hint=acknowledge)
            )
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    for network in _network_names(annotations.get(MULTUS_ANNOTATION) or ""):
        if "/" not in network:
            need("NetworkAttachmentDefinition", network, ref)
    need("ServiceAccount", spec.get("serviceAccountName") or "", ref)
    for pull in spec.get("imagePullSecrets") or []:
        need("Secret", pull.get("name", ""), ref)
    for volume in spec.get("volumes") or []:
        if "hostPath" in volume:
            path = (volume["hostPath"] or {}).get("path", "")
            findings.append(
                Finding(
                    severity,
                    "host-path",
                    f"{where} mounts hostPath {path} as volume {volume.get('name')}",
                    object=ref,
                    hint=acknowledge,
                )
            )
        elif "persistentVolumeClaim" in volume:
            need("PersistentVolumeClaim", (volume["persistentVolumeClaim"] or {}).get("claimName", ""), ref)
        elif "secret" in volume and not (volume["secret"] or {}).get("optional"):
            need("Secret", (volume["secret"] or {}).get("secretName", ""), ref)
        elif "configMap" in volume and not (volume["configMap"] or {}).get("optional"):
            need("ConfigMap", (volume["configMap"] or {}).get("name", ""), ref)
    images = []
    for container in containers(template):
        images.append(container.get("image"))
        security = container.get("securityContext") or {}
        name = container.get("name")
        if security.get("privileged"):
            findings.append(
                Finding(
                    severity,
                    "privileged",
                    f"{where} container {name} is privileged",
                    object=ref,
                    hint=acknowledge,
                )
            )
        added = set((security.get("capabilities") or {}).get("add") or []) - _SAFE_CAPABILITIES
        if added:
            findings.append(
                Finding(
                    "warning",
                    "capabilities",
                    f"{where} container {name} adds {', '.join(sorted(added))}",
                    object=ref,
                )
            )
        for env in container.get("env") or []:
            source = env.get("valueFrom") or {}
            if "secretKeyRef" in source and not source["secretKeyRef"].get("optional"):
                need("Secret", source["secretKeyRef"].get("name", ""), ref)
            if "configMapKeyRef" in source and not source["configMapKeyRef"].get("optional"):
                need("ConfigMap", source["configMapKeyRef"].get("name", ""), ref)
        for env_from in container.get("envFrom") or []:
            if "secretRef" in env_from and not env_from["secretRef"].get("optional"):
                need("Secret", env_from["secretRef"].get("name", ""), ref)
            if "configMapRef" in env_from and not env_from["configMapRef"].get("optional"):
                need("ConfigMap", env_from["configMapRef"].get("name", ""), ref)
    accelerators = template_accelerators(template)
    for resource, amount in accelerators.items():
        result.accelerators[resource] = result.accelerators.get(resource, 0) + amount * (
            1 if pods is None else pods
        )
    result.workloads.append(
        {"object": ref, "template": location, "pods": pods, "accelerators": accelerators, "images": images}
    )
