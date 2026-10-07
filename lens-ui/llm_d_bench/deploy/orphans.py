"""Detect and clean deployment namespaces that Lens no longer tracks.

A deployment's namespace is the only handle Lens keeps on its cluster
resources. When the Lens record is removed without namespace cleanup (or the
database is rebuilt), the namespace and its InferencePool/EPP/model server are
left behind with nothing pointing at them. This module finds namespaces under
the configured deployment prefix that no execution references, and can delete a
caller-selected subset.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from llm_d_bench.deploy.run_store import JsonDeploymentRunStore
from llm_d_bench.utils.kubernetes import scoped_runner

DEFAULT_NAMESPACE_PREFIX = "llmd-"


class OrphanCleanupError(Exception):
    """Orphan namespace scan or cleanup failed."""


@dataclass(frozen=True)
class OrphanNamespace:
    name: str
    created_at: str | None = None
    phase: str | None = None

    def api_payload(self) -> dict:
        return {"name": self.name, "createdAt": self.created_at, "phase": self.phase}


def namespace_prefix() -> str:
    """The configured deployment namespace prefix (mirrors LLM_D_BENCH_NAMESPACE_PREFIX)."""
    prefix = (os.environ.get("LLM_D_BENCH_NAMESPACE_PREFIX") or DEFAULT_NAMESPACE_PREFIX).strip()
    return prefix or DEFAULT_NAMESPACE_PREFIX


def known_namespaces(store: JsonDeploymentRunStore | None = None) -> set[str]:
    """Namespaces currently referenced by a Lens deployment execution record."""
    store = store or JsonDeploymentRunStore()
    return {execution.namespace for execution in store.list_executions() if getattr(execution, "namespace", None)}


async def scan_orphan_namespaces(
    cluster_id: str, *, store: JsonDeploymentRunStore | None = None
) -> list[OrphanNamespace]:
    """Namespaces on the cluster under the deployment prefix with no Lens record."""
    prefix = namespace_prefix()
    known = known_namespaces(store)
    runner = scoped_runner(cluster_id)
    try:
        result = await runner.run(["kubectl", "get", "namespaces", "-o", "json"], timeout=30)
    except (FileNotFoundError, TimeoutError, OSError) as error:
        raise OrphanCleanupError(f"kubectl unavailable: {error}") from error
    if result.returncode != 0:
        raise OrphanCleanupError((result.stderr or result.stdout or "kubectl failed").strip())
    try:
        items = json.loads(result.stdout or "{}").get("items", [])
    except json.JSONDecodeError as error:
        raise OrphanCleanupError("invalid namespaces JSON") from error
    orphans: list[OrphanNamespace] = []
    for item in items:
        metadata = item.get("metadata") or {}
        name = metadata.get("name")
        if not name or name == prefix or not name.startswith(prefix) or name in known:
            continue
        orphans.append(
            OrphanNamespace(
                name=name,
                created_at=metadata.get("creationTimestamp"),
                phase=(item.get("status") or {}).get("phase"),
            )
        )
    return sorted(orphans, key=lambda orphan: orphan.created_at or "")


async def clean_orphan_namespaces(
    cluster_id: str, namespaces: list[str], *, store: JsonDeploymentRunStore | None = None
) -> tuple[list[str], list[dict]]:
    """Delete the named orphan namespaces, returning (cleaned, failures).

    Names outside the deployment prefix, or still referenced by a Lens record,
    are refused rather than deleted.
    """
    prefix = namespace_prefix()
    known = known_namespaces(store)
    runner = scoped_runner(cluster_id)
    cleaned: list[str] = []
    failed: list[dict] = []
    for name in dict.fromkeys(namespaces):
        if name == prefix or not name.startswith(prefix):
            failed.append({"name": name, "error": "namespace is outside the deployment prefix"})
            continue
        if name in known:
            failed.append({"name": name, "error": "namespace is still tracked by a Lens deployment"})
            continue
        try:
            result = await runner.run(["kubectl", "delete", "namespace", name, "--ignore-not-found=true"], timeout=120)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            failed.append({"name": name, "error": f"kubectl unavailable: {error}"})
            continue
        if result.returncode == 0:
            cleaned.append(name)
        else:
            failed.append({"name": name, "error": (result.stderr or result.stdout or "delete failed").strip()[:300]})
    return cleaned, failed
