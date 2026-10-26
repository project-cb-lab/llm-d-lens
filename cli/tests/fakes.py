"""An in-memory stand-in for ``kubectl`` that answers exactly the calls llmd makes.

Other tools (git, kustomize) run for real, so rendering is tested end to end.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid

import yaml

from llmd.process import Result, subprocess_runner

CORE = {
    "ConfigMap": ("configmaps", True),
    "Secret": ("secrets", True),
    "Service": ("services", True),
    "ServiceAccount": ("serviceaccounts", True),
    "PersistentVolumeClaim": ("persistentvolumeclaims", True),
    "PersistentVolume": ("persistentvolumes", False),
    "Pod": ("pods", True),
    "Namespace": ("namespaces", False),
}
DEFAULT_SERVED = {
    "v1": CORE,
    "apps/v1": {"Deployment": ("deployments", True)},
    "batch/v1": {"Job": ("jobs", True)},
    "rbac.authorization.k8s.io/v1": {
        "Role": ("roles", True),
        "RoleBinding": ("rolebindings", True),
        "ClusterRole": ("clusterroles", False),
    },
    "scheduling.k8s.io/v1": {"PriorityClass": ("priorityclasses", False)},
    "leaderworkerset.x-k8s.io/v1": {"LeaderWorkerSet": ("leaderworkersets", True)},
    "disaggregatedset.x-k8s.io/v1": {"DisaggregatedSet": ("disaggregatedsets", True)},
    "k8s.cni.cncf.io/v1": {"NetworkAttachmentDefinition": ("network-attachment-definitions", True)},
}


def _fail(message: str) -> Result:
    return Result(("kubectl",), 1, "", message)


def _ok(stdout: str = "") -> Result:
    return Result(("kubectl",), 0, stdout, "")


def _merge(base, patch):
    """Merge ``patch`` into ``base`` like server-side apply does for plain fields (maps merge,
    lists and scalars are replaced)."""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        merged[key] = _merge(merged.get(key), value) if isinstance(value, dict) else copy.deepcopy(value)
    return merged


class FakeCluster:
    def __init__(
        self, *, namespaces=("team-a",), user="alice", server="https://api.fake.example.invalid:6443"
    ):
        self.namespaces = set(namespaces)
        self.server = server
        self.user = user
        self.served = copy.deepcopy(DEFAULT_SERVED)
        #: {(verb, "plural.group" or "plural")} the user may not do.
        self.denied: set[tuple[str, str]] = set()
        self.objects: dict[tuple[str, str, str], dict] = {}
        self.calls: list[list[str]] = []
        self._version = 0
        #: Act as the workload controllers: after each change, give every Deployment and
        #: DisaggregatedSet exactly its pods, Ready, labelled the way the controllers label them.
        self.controllers = True
        #: (old LeaderWorkerSet, new replicas) -> allowed? Stands in for an admission policy that
        #: holds the controller's scale-down until a group is drained.
        self.refuse_scale_down = None

    # -- helpers for tests ---------------------------------------------------------------------------
    def serve(self, api_version: str, kind: str, plural: str, namespaced: bool = True) -> None:
        self.served.setdefault(api_version, {})[kind] = (plural, namespaced)

    def resource_of(self, api_version: str, kind: str) -> str | None:
        plural, _ = self.served.get(api_version, {}).get(kind, (None, None))
        if plural is None:
            return None
        group = api_version.split("/", 1)[0] if "/" in api_version else ""
        return f"{plural}.{group}" if group else plural

    def put(self, namespace: str, document: dict) -> dict:
        """Create or replace an object as another tool would (kubectl apply, Helm)."""
        document = copy.deepcopy(document)
        resource = self.resource_of(document["apiVersion"], document["kind"])
        metadata = document.setdefault("metadata", {})
        if not self.served[document["apiVersion"]][document["kind"]][1]:
            namespace = ""
            metadata.pop("namespace", None)
        else:
            metadata["namespace"] = namespace
        metadata.setdefault("uid", uuid.uuid4().hex)
        self._bump(document)
        self.objects[(namespace, resource, metadata["name"])] = document
        return document

    def get(self, namespace: str, resource: str, name: str) -> dict | None:
        return self.objects.get((namespace, self._canonical(resource), name))

    def names(self, namespace: str, resource: str) -> list[str]:
        canonical = self._canonical(resource)
        return sorted(name for ns, kind, name in self.objects if ns == namespace and kind == canonical)

    def settle(self) -> None:
        """Make the pods of every Deployment match its spec, and bring every DisaggregatedSet that is
        not rolling to its replicas (a roll moves only through :meth:`roll_step`)."""
        for (namespace, resource, name), document in list(self.objects.items()):
            spec = document.get("spec") or {}
            if resource == "deployments.apps":
                labels = dict(((spec.get("template") or {}).get("metadata") or {}).get("labels") or {})
                replicas = int(spec.get("replicas", 1) if spec.get("replicas") is not None else 1)
                self._pods(namespace, f"{name}-", replicas, labels)
            elif resource.startswith("disaggregatedsets."):
                self._reconcile_set(namespace, document)

    # -- the DisaggregatedSet controller (lws) -------------------------------------------------------
    @staticmethod
    def revision(document: dict) -> str:
        """lws hashes each role's name and leaderWorkerTemplate into the revision."""
        roles = [
            (role["name"], (role.get("spec") or {}).get("leaderWorkerTemplate"))
            for role in document["spec"]["roles"]
        ]
        return hashlib.sha256(json.dumps(roles, sort_keys=True).encode()).hexdigest()[:8]

    def _sets(self, namespace: str, name: str) -> list[dict]:
        found = [
            item
            for (ns, resource, _), item in self.objects.items()
            if ns == namespace
            and resource.startswith("leaderworkersets.")
            and item["metadata"]["labels"].get("disaggregatedset.x-k8s.io/name") == name
        ]
        return sorted(found, key=lambda item: item["metadata"]["creationTimestamp"])

    def _set(self, namespace: str, name: str, role: dict, revision: str, replicas: int) -> dict:
        lws_name = f"{name}-{revision}-{role['name']}"
        key = (namespace, "leaderworkersets.leaderworkerset.x-k8s.io", lws_name)
        current = self.objects.get(key)
        if current is None:
            self._version += 1
            current = {
                "apiVersion": "leaderworkerset.x-k8s.io/v1",
                "kind": "LeaderWorkerSet",
                "metadata": {
                    "name": lws_name,
                    "namespace": namespace,
                    "uid": uuid.uuid4().hex,
                    "creationTimestamp": f"2026-01-01T00:00:{self._version:05d}Z",
                    "labels": {
                        "disaggregatedset.x-k8s.io/name": name,
                        "disaggregatedset.x-k8s.io/role": role["name"],
                        "disaggregatedset.x-k8s.io/revision": revision,
                    },
                },
                "spec": {
                    "replicas": 0,
                    "leaderWorkerTemplate": copy.deepcopy(role["spec"]["leaderWorkerTemplate"]),
                },
            }
            self.objects[key] = current
        current["spec"]["replicas"] = replicas
        self._lws_pods(namespace, current, name, role)
        return current

    def _lws_pods(self, namespace: str, lws: dict, name: str, role: dict) -> None:
        template = lws["spec"]["leaderWorkerTemplate"]
        size = int(template.get("size") or 1)
        labels = dict(((template.get("workerTemplate") or {}).get("metadata") or {}).get("labels") or {})
        revision = lws["metadata"]["labels"]["disaggregatedset.x-k8s.io/revision"]
        labels.update(
            {
                "disaggregatedset.x-k8s.io/name": name,
                "disaggregatedset.x-k8s.io/role": role["name"],
                "disaggregatedset.x-k8s.io/revision": revision,
                "leaderworkerset.sigs.k8s.io/name": lws["metadata"]["name"],
            }
        )
        self._pods(namespace, f"{lws['metadata']['name']}-", lws["spec"]["replicas"] * size, labels, size)

    def _reconcile_set(self, namespace: str, document: dict) -> None:
        name, target = document["metadata"]["name"], self.revision(document)
        sets = self._sets(namespace, name)
        if any(item["metadata"]["labels"]["disaggregatedset.x-k8s.io/revision"] != target for item in sets):
            return  # a roll is in progress; roll_step moves it
        for role in document["spec"]["roles"]:
            self._set(namespace, name, role, target, int(role["spec"].get("replicas") or 0))

    def roll_step(self, namespace: str, name: str) -> bool:
        """One step of lws v0.9's in-place roll (maxSurge 0), per role: raise the new revision's
        LeaderWorkerSet by up to maxUnavailable groups, then lower the old one by as many. The new
        groups stay Pending (no free nodes) until the old ones are gone. A lowering an admission
        policy refuses (``self.refuse_scale_down``) holds that role. Returns whether any old group
        was removed; once every old set is at 0, the old sets are deleted."""
        document = self.objects[(namespace, "disaggregatedsets.disaggregatedset.x-k8s.io", name)]
        target = self.revision(document)
        moved = False
        for role in document["spec"]["roles"]:
            spec = role["spec"]
            replicas = int(spec.get("replicas") or 0)
            config = ((spec.get("rolloutStrategy") or {}).get("rollingUpdateConfiguration")) or {}
            unavailable = int(config.get("maxUnavailable", 1))
            olds = [
                item
                for item in self._sets(namespace, name)
                if item["metadata"]["labels"]["disaggregatedset.x-k8s.io/role"] == role["name"]
                and item["metadata"]["labels"]["disaggregatedset.x-k8s.io/revision"] != target
                and item["spec"]["replicas"] > 0
            ]
            remaining = sum(item["spec"]["replicas"] for item in olds)
            raised = min(replicas, replicas - remaining + unavailable) if olds else replicas
            new = self._set(
                namespace,
                name,
                role,
                target,
                max(raised, self._replicas(namespace, name, role["name"], target)),
            )
            if not olds:
                continue
            old = olds[-1]
            lowered = max(old["spec"]["replicas"] - unavailable, 0)
            if self.refuse_scale_down and not self.refuse_scale_down(old, lowered):
                self._pending(namespace, new, replicas - remaining)
                continue
            self._lower(namespace, old, lowered, name, role)
            moved = True
        if all(
            item["spec"]["replicas"] == 0
            for item in self._sets(namespace, name)
            if item["metadata"]["labels"]["disaggregatedset.x-k8s.io/revision"] != target
        ):
            for item in self._sets(namespace, name):
                if item["metadata"]["labels"]["disaggregatedset.x-k8s.io/revision"] != target:
                    del self.objects[
                        (namespace, "leaderworkersets.leaderworkerset.x-k8s.io", item["metadata"]["name"])
                    ]
        return moved

    def _pending(self, namespace: str, lws: dict, schedulable: int) -> None:
        """The new set's groups from ``schedulable`` on have no nodes: their pods are Pending."""
        for key, pod in self.objects.items():
            if key[0] != namespace or key[1] != "pods":
                continue
            pod_labels = pod["metadata"]["labels"]
            if pod_labels.get("leaderworkerset.sigs.k8s.io/name") != lws["metadata"]["name"]:
                continue
            if int(pod_labels["leaderworkerset.sigs.k8s.io/group-index"]) >= schedulable:
                pod["status"] = {
                    "phase": "Pending",
                    "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}],
                }

    def _lower(self, namespace: str, lws: dict, replicas: int, name: str, role: dict) -> None:
        lws["spec"]["replicas"] = replicas
        self._lws_pods(
            namespace,
            lws,
            name,
            {"name": role["name"], "spec": {"leaderWorkerTemplate": lws["spec"]["leaderWorkerTemplate"]}},
        )
        for key, pod in self.objects.items():  # the freed nodes take the new groups
            if key[0] == namespace and key[1] == "pods" and pod["status"].get("phase") == "Pending":
                pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}

    def _replicas(self, namespace: str, name: str, role: str, revision: str) -> int:
        item = self.objects.get(
            (namespace, "leaderworkersets.leaderworkerset.x-k8s.io", f"{name}-{revision}-{role}")
        )
        return item["spec"]["replicas"] if item else 0

    def _orphans(self, namespace: str, removed: dict) -> None:
        """Pods of a deleted workload go with it (garbage collection)."""
        name = removed["metadata"]["name"]
        for key in [key for key in self.objects if key[0] == namespace and key[1] == "pods"]:
            if key[2].startswith(f"{name}-"):
                del self.objects[key]

    def _pods(self, namespace: str, prefix: str, want: int, labels: dict, size: int = 1) -> None:
        have = sorted(
            key
            for key in self.objects
            if key[0] == namespace and key[1] == "pods" and key[2].startswith(prefix)
        )
        for key in have[want:]:
            del self.objects[key]
        for index in range(len(have), want):
            pod_labels = {**labels, "leaderworkerset.sigs.k8s.io/group-index": str(index // size)}
            pod = {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": f"{prefix}{index}", "labels": pod_labels},
                "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
            }
            self.put(namespace, pod)

    def _bump(self, document: dict) -> None:
        self._version += 1
        document["metadata"]["resourceVersion"] = str(self._version)

    def _canonical(self, resource: str) -> str:
        for api_version, kinds in self.served.items():
            group = api_version.split("/", 1)[0] if "/" in api_version else ""
            for plural, _ in kinds.values():
                if resource in (plural, f"{plural}.{group}" if group else plural):
                    return f"{plural}.{group}" if group else plural
        return resource

    def _namespaced(self, resource: str) -> bool:
        for api_version, kinds in self.served.items():
            for kind, (_, namespaced) in kinds.items():
                if self.resource_of(api_version, kind) == resource:
                    return namespaced
        return True

    # -- the runner ----------------------------------------------------------------------------------
    def __call__(self, argv, *, input=None, timeout=300, cwd=None, env=None) -> Result:
        argv = [str(item) for item in argv]
        if argv[0] not in ("kubectl", "oc"):
            return subprocess_runner(argv, input=input, timeout=timeout, cwd=cwd, env=env)
        args, tokens = [], iter(argv[1:])
        for token in tokens:
            if token in ("--kubeconfig", "--context"):
                next(tokens, None)
            else:
                args.append(token)
        self.calls.append(args)
        return self._dispatch(args, input or "")

    def _option(self, args: list[str], *names: str) -> str:
        for position, token in enumerate(args):
            for name in names:
                if token == name and position + 1 < len(args):
                    return args[position + 1]
                if token.startswith(name + "="):
                    return token.split("=", 1)[1]
        return ""

    def _dispatch(self, args: list[str], stdin: str) -> Result:
        if args[:2] == ["config", "current-context"]:
            return _ok("fake-context\n")
        if args[:2] == ["config", "view"]:
            return _ok(self.server if any("cluster.server" in arg for arg in args) else "team-a")
        if args[:2] == ["auth", "whoami"]:
            return _ok(json.dumps({"status": {"userInfo": {"username": self.user}}}))
        if args[:2] == ["get", "--raw"]:
            return self._discovery(args[2])
        if args[:2] == ["get", "namespace"]:
            name = args[2]
            return (
                _ok(f"namespace/{name}")
                if name in self.namespaces
                else _fail(f'(NotFound) "{name}" not found')
            )
        handler = {
            "create": self._reviews,
            "apply": self._apply,
            "get": self._get,
            "delete": self._delete,
        }.get(args[0])
        return handler(args, stdin) if handler else _fail(f"fake kubectl does not support {args}")

    def _discovery(self, path: str) -> Result:
        api_version = path.removeprefix("/apis/").removeprefix("/api/")
        if api_version not in self.served:
            return _fail("Error from server (NotFound): the server could not find the requested resource")
        resources = [
            {"name": plural, "kind": kind, "namespaced": namespaced}
            for kind, (plural, namespaced) in self.served[api_version].items()
        ]
        return _ok(
            json.dumps({"kind": "APIResourceList", "groupVersion": api_version, "resources": resources})
        )

    def _reviews(self, args: list[str], stdin: str) -> Result:
        items = []
        for review in json.loads(stdin).get("items", []):
            attributes = review["spec"]["resourceAttributes"]
            resource = attributes["resource"] + (f".{attributes['group']}" if attributes["group"] else "")
            items.append({**review, "status": {"allowed": (attributes["verb"], resource) not in self.denied}})
        return _ok(json.dumps({"kind": "List", "items": items}))

    def _apply(self, args: list[str], stdin: str) -> Result:
        namespace = self._option(args, "-n")
        dry_run = any(arg.startswith("--dry-run") for arg in args)
        server_side = "--server-side" in args
        out = []
        for document in yaml.safe_load_all(stdin):
            if not document:
                continue
            resource = self.resource_of(document["apiVersion"], document["kind"])
            if resource is None:
                return _fail(f'error: resource mapping not found for kind "{document["kind"]}"')
            if ("create", resource) in self.denied or ("patch", resource) in self.denied:
                return _fail(f'Error from server (Forbidden): {resource} is forbidden: User "{self.user}"')
            key = (namespace if self._namespaced(resource) else "", resource, document["metadata"]["name"])
            current = self.objects.get(key)
            merged = _merge(current or {}, document)
            merged["metadata"]["namespace"] = key[0] or None
            merged["metadata"]["uid"] = (current or {}).get("metadata", {}).get("uid") or uuid.uuid4().hex
            if not server_side:
                annotations = merged["metadata"].setdefault("annotations", {})
                annotations["kubectl.kubernetes.io/last-applied-configuration"] = json.dumps(document)
            if current and "status" in current:
                merged["status"] = current["status"]
            if not dry_run:
                self._bump(merged)
                self.objects[key] = merged
            out.append(merged)
        if self.controllers and not dry_run:
            self.settle()
        return _ok("\n".join(json.dumps(item) for item in out))

    def _selected(self, document: dict, selector: str) -> bool:
        labels = (document.get("metadata") or {}).get("labels") or {}
        for term in filter(None, selector.split(",")):
            key, _, value = term.partition("=")
            if labels.get(key) != value:
                return False
        return True

    def _get(self, args: list[str], stdin: str) -> Result:
        resource = self._canonical(args[1])
        namespace = self._option(args, "-n") if self._namespaced(resource) else ""
        selector = self._option(args, "-l")
        names = [
            arg for arg in args[2:] if not arg.startswith("-") and arg not in (namespace, selector, "json")
        ]
        if ("get", resource) in self.denied or ("list", resource) in self.denied:
            return _fail(f'Error from server (Forbidden): {resource} is forbidden: User "{self.user}"')
        items = [
            copy.deepcopy(document)
            for (ns, kind, name), document in sorted(self.objects.items())
            if ns == namespace
            and kind == resource
            and (not names or name in names)
            and (not selector or self._selected(document, selector))
        ]
        return _ok(json.dumps({"kind": "List", "items": items}))

    def _delete(self, args: list[str], stdin: str) -> Result:
        resource = self._canonical(args[1])
        namespace = self._option(args, "-n") if self._namespaced(resource) else ""
        if ("delete", resource) in self.denied:
            return _fail(f'Error from server (Forbidden): {resource} is forbidden: User "{self.user}"')
        removed = self.objects.pop((namespace, resource, args[2]), None)
        if self.controllers and removed:
            self._orphans(namespace, removed)
        return _ok(f'{resource} "{args[2]}" deleted\n' if removed else "")
