"""An in-memory stand-in for ``kubectl`` that answers exactly the calls llmd makes.

Other tools (git, kustomize) run for real, so rendering is tested end to end.
"""

from __future__ import annotations

import copy
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
        return _ok(f'{resource} "{args[2]}" deleted\n' if removed else "")
