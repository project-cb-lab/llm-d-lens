"""The kubectl/oc adapter. Every call names the one target namespace.

* API discovery uses ``get --raw /api/v1`` and ``/apis/<group>/<version>``, which every
  authenticated user may read; it gives each kind's scope and plural name.
* Permission checks submit SelfSubjectAccessReviews in one ``create -f -`` call.
* Objects llmd creates are applied with server-side apply under the ``llm-d-lens`` field
  manager. Objects of an imported stack are applied the way the team applies them
  (``kubectl apply``, client-side), so the team's own ``oc apply`` keeps working.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import yamlio
from .errors import CommandError, NotFoundError
from .model import FIELD_MANAGER, api_group
from .process import Result, Runner, subprocess_runner

IN_CLUSTER_NAMESPACE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


@dataclass(frozen=True)
class ApiResource:
    group: str
    version: str
    kind: str
    plural: str
    namespaced: bool

    @property
    def qualified(self) -> str:
        """The unambiguous kubectl resource name, e.g. ``deployments.apps``."""
        return f"{self.plural}.{self.group}" if self.group else self.plural


@dataclass(frozen=True)
class Target:
    """Which credentials to use. Empty fields mean kubectl's own defaults."""

    kubeconfig: str = ""
    context: str = ""
    cli: str = "kubectl"


def guess_plural(kind: str) -> str:
    """A kind's resource name when discovery is not available, by kubectl's own fallback rule."""
    singular = kind.lower()
    if singular.endswith("s"):
        return singular + "es"
    if singular.endswith("y"):
        return singular[:-1] + "ies"
    return singular + "s"


class Kube:
    def __init__(self, target: Target | None = None, runner: Runner = subprocess_runner) -> None:
        self.target = target or Target()
        self._runner = runner
        self._discovery: dict[str, dict[str, ApiResource] | None] = {}

    # -- plumbing ------------------------------------------------------------------------------------
    def base(self) -> list[str]:
        argv = [self.target.cli]
        if self.target.kubeconfig:
            argv += ["--kubeconfig", self.target.kubeconfig]
        if self.target.context:
            argv += ["--context", self.target.context]
        return argv

    def run(self, *args: str, input: str | None = None, timeout: float = 120) -> Result:
        return self._runner([*self.base(), *args], input=input, timeout=timeout)

    # -- identity ------------------------------------------------------------------------------------
    def current_context(self) -> str:
        if self.target.context:
            return self.target.context
        result = self.run("config", "current-context", timeout=30)
        return result.stdout.strip() if result.ok else ""

    def server(self) -> str:
        """The API server URL of the selected context (read from the kubeconfig, no cluster call)."""
        if not self.target.kubeconfig and not self.target.context and _in_cluster():
            host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
            host = f"[{host}]" if ":" in host else host
            return f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}" if host else ""
        result = self.run(
            "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}", timeout=30
        )
        return result.stdout.strip() if result.ok else ""

    def default_namespace(self) -> str:
        if not self.target.kubeconfig and not self.target.context and _in_cluster():
            return IN_CLUSTER_NAMESPACE.read_text(encoding="utf-8").strip()
        result = self.run("config", "view", "--minify", "-o", "jsonpath={..namespace}", timeout=30)
        return result.stdout.strip() if result.ok else ""

    def whoami(self) -> str:
        result = self.run("auth", "whoami", "-o", "json", timeout=30)
        try:
            return str(json.loads(result.stdout)["status"]["userInfo"]["username"]) if result.ok else ""
        except (ValueError, KeyError, TypeError):
            return ""

    # -- discovery -----------------------------------------------------------------------------------
    def resources(self, api_version: str) -> dict[str, ApiResource] | None:
        """Kind -> resource for one group/version, or None when the cluster does not serve it."""
        if api_version in self._discovery:
            return self._discovery[api_version]
        path = f"/apis/{api_version}" if "/" in api_version else f"/api/{api_version}"
        result = self.run("get", "--raw", path, timeout=60)
        found: dict[str, ApiResource] | None = None
        if result.ok:
            group, version = api_group(api_version), api_version.split("/", 1)[-1]
            found = {}
            for item in json.loads(result.stdout or "{}").get("resources") or []:
                if "/" not in item.get("name", ""):  # leave out subresources such as deployments/scale
                    found[item["kind"]] = ApiResource(
                        group, version, item["kind"], item["name"], bool(item.get("namespaced"))
                    )
        elif "NotFound" not in result.message and "could not find" not in result.message:
            raise CommandError(f"API discovery for {api_version} failed: {result.message}")
        self._discovery[api_version] = found
        return found

    def resource(self, api_version: str, kind: str) -> ApiResource | None:
        return (self.resources(api_version) or {}).get(kind)

    def resource_name(self, document: dict[str, Any]) -> str:
        """The kubectl resource name of an object's kind (discovery first, kubectl's rule otherwise)."""
        found = self.resource(document["apiVersion"], document["kind"])
        if found:
            return found.qualified
        group = api_group(document["apiVersion"])
        plural = guess_plural(document["kind"])
        return f"{plural}.{group}" if group else plural

    def scope(self, api_version: str, kind: str) -> bool | None:
        """True when namespaced, False when cluster-scoped, None when the cluster does not serve it."""
        found = self.resource(api_version, kind)
        return None if found is None else found.namespaced

    # -- reads ---------------------------------------------------------------------------------------
    def access_reviews(self, checks: list[tuple[str, str, str, str]]) -> list[bool | None]:
        """[(verb, group, plural, namespace)] -> allowed? (None when a review was not answered)."""
        if not checks:
            return []
        items = [
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": {
                    "resourceAttributes": {"verb": verb, "group": group, "resource": plural, "namespace": ns}
                },
            }
            for verb, group, plural, ns in checks
        ]
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": items})
        result = self.run("create", "-o", "json", "-f", "-", input=body, timeout=60)
        if not result.ok:
            return [None] * len(checks)
        try:
            reviews = yamlio.kubectl_objects(result.stdout)
        except ValueError:
            return [None] * len(checks)
        allowed = [
            bool((review.get("status") or {}).get("allowed"))
            if "allowed" in (review.get("status") or {})
            else None
            for review in reviews
        ]
        return allowed + [None] * (len(checks) - len(allowed))

    def namespace_state(self, namespace: str) -> str:
        """exists | missing | unknown (a team member may not be allowed to read Namespace objects)."""
        result = self.run("get", "namespace", namespace, "-o", "name", timeout=30)
        if result.ok:
            return "exists"
        return "missing" if "NotFound" in result.message else "unknown"

    def get_json(
        self, resource: str, namespace: str, *, names: list[str] | None = None, selector: str = ""
    ) -> list[dict[str, Any]]:
        args = ["get", resource, *(names or []), "-n", namespace, "-o", "json", "--ignore-not-found"]
        if selector:
            args += ["-l", selector]
        result = self.run(*args, timeout=60)
        if not result.ok:
            if "NotFound" in result.message or "doesn't have a resource type" in result.message:
                return []
            raise CommandError(f"reading {resource} in {namespace} failed: {result.message}")
        return yamlio.kubectl_objects(result.stdout)

    def get(self, resource: str, name: str, namespace: str) -> dict[str, Any] | None:
        found = self.get_json(resource, namespace, names=[name])
        return found[0] if found else None

    # -- writes --------------------------------------------------------------------------------------
    def apply(
        self,
        documents: list[dict[str, Any]],
        namespace: str,
        *,
        server_side: bool = True,
        dry_run: bool = False,
        force_conflicts: bool = False,
        timeout: float = 300,
    ) -> list[dict[str, Any]]:
        """Apply objects. ``server_side`` uses server-side apply as llmd's field manager; without it,
        ``kubectl apply`` as a team applies its YAML (the last-applied annotation is kept)."""
        args = ["apply", "-n", namespace, "-o", "json", "-f", "-"]
        if server_side:
            args[1:1] = ["--server-side", f"--field-manager={FIELD_MANAGER}"]
            if force_conflicts:
                args.insert(1, "--force-conflicts")
        if dry_run:
            args.insert(1, "--dry-run=server")
        result = self.run(*args, input=yamlio.dump_documents(documents), timeout=timeout)
        if not result.ok:
            raise CommandError(
                f"{'server dry run' if dry_run else 'apply'} failed: {result.message}",
                hint="Missing permissions, a quota, or an admission policy can cause this.",
            )
        return yamlio.kubectl_objects(result.stdout)

    def delete(self, resource: str, name: str, namespace: str, *, wait: bool = False) -> bool:
        result = self.run(
            "delete",
            resource,
            name,
            "-n",
            namespace,
            "--ignore-not-found",
            f"--wait={'true' if wait else 'false'}",
            timeout=600 if wait else 120,
        )
        if not result.ok:
            if "NotFound" in result.message:
                return False
            raise CommandError(f"deleting {resource}/{name} failed: {result.message}")
        return bool(result.stdout.strip())

    def require(self, resource: str, name: str, namespace: str) -> dict[str, Any]:
        found = self.get(resource, name, namespace)
        if found is None:
            raise NotFoundError(f"{resource}/{name} not found in {namespace}")
        return found


def _in_cluster() -> bool:
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and IN_CLUSTER_NAMESPACE.is_file()
