"""Durable cluster registry for uploaded kubeconfigs.

Backed by the ``clusters`` table (see
docs/design/sqlalchemy-data-access-layer-design.md section 5.4.1) via
``ClusterDao``, instead of one JSON file per cluster under
``<data_directory>/clusters``. The public functions below keep their exact
signatures/behavior from the file-based implementation so every other module
that imports them (``cluster.service``, ``cluster.router``,
``deploy.endpoint``, ``simulation.service``, ``utils.kubernetes``, ...) needs
no changes.

Kubeconfig text itself lives in the ``clusters.kubeconfig`` column, not as a
plain-text file; ``kubeconfig_path()`` materializes it to a small local cache
file on demand for callers (``run_kubectl``/``KUBECONFIG`` env var) that need
a filesystem path. ``ClusterDao`` is imported lazily (inside
``_dao()``) rather than at module level: it depends on
``llm_d_bench.db.models.cluster.ClusterRow``, which maps rows onto the
``Cluster`` dataclass defined below, and importing it eagerly here would
create an import cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from llm_d_bench.cluster.errors import ClusterOverviewError
from llm_d_bench.cluster.settings import cluster_settings

if TYPE_CHECKING:
    from llm_d_bench.db.dao.cluster import ClusterDao


@dataclass(frozen=True)
class Cluster:
    id: str
    name: str
    description: str
    created_at: str
    # Proxy settings for this cluster. ``mode="auto"`` means "let the system
    # figure it out" -- today (no auto-detection implemented yet) that falls
    # back to whatever proxy env vars the Prism backend process itself sees,
    # same as pre-wizard behavior; ``mode="custom"`` means the three values
    # below are used verbatim regardless of the backend process environment.
    # See docs/design/cluster-creation-wizard-design.md section 4.2.
    proxy_mode: str = "auto"
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    llm_d_ref: str | None = None
    llm_d_benchmark_ref: str | None = None
    # Filesystem path of the downloaded llm-d / llm-d-benchmark checkout
    # matching llm_d_ref/llm_d_benchmark_ref above, once the wizard's
    # Software Versions step has actually fetched it (see
    # llm_d_bench.cluster.repo_downloads). None until a download completes.
    llm_d_repo_path: str | None = None
    llm_d_benchmark_repo_path: str | None = None
    # Recorded by the wizard's mandatory HF_TOKEN secret step (see
    # to_dict/from_dict below and CreateClusterWizard.jsx) so other
    # components (Deploy/Evaluate) can default to reusing it -- see
    # llm_d_bench/deploy/service.py's ``_default_model_secret``.
    hf_token_secret_namespace: str | None = None
    hf_token_secret_name: str | None = None
    # llm-d Gateway Mode data plane pinned per cluster (wizard Software Versions
    # step): one shared Gateway installed at cluster creation, provider selectable
    # (istio/gke/agentgateway/envoy-ai-gateway). See
    # docs/design/model-service-llmd-routing-design.zh-CN.md section 5.
    gateway_provider: str | None = None
    gateway_namespace: str | None = None
    gateway_name: str | None = None
    #: Externally reachable Gateway URL clients use (the in-cluster Service
    #: address is not usable off-cluster).
    gateway_public_url: str | None = None
    gateway_port: int | None = None
    #: Host/IP the cluster can reach Lens' model-gateway ext_authz endpoint on
    #: (Lens adds its own serving port).
    gateway_authz_host: str | None = None
    #: Per-uid inotify instance limit Lens sets on every node (``fs.inotify
    #: .max_user_instances``). A busy node running many pods exhausts the kind
    #: default (128) and controllers then fail with "too many open files".
    inotify_max_user_instances: int = 8192
    router_version: str | None = None
    gie_version: str | None = None
    ipp_version: str | None = None
    # True while the cluster is still being assembled by the multi-step
    # creation wizard: it is hidden from list_clusters() by default (and
    # from every UI that renders the cluster list) until the wizard's final
    # "Finish" step flips this to False. This is what keeps the wizard from
    # appearing to have "saved" a cluster before the user actually finishes
    # it -- if the wizard is cancelled early, the caller deletes the draft
    # record instead of leaving a half-configured cluster visible.
    draft: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "createdAt": self.created_at,
            "proxy": {
                "mode": self.proxy_mode,
                "httpProxy": self.http_proxy,
                "httpsProxy": self.https_proxy,
                "noProxy": self.no_proxy,
            },
            "llmDRef": self.llm_d_ref,
            "llmDBenchmarkRef": self.llm_d_benchmark_ref,
            "llmDRepoPath": self.llm_d_repo_path,
            "llmDBenchmarkRepoPath": self.llm_d_benchmark_repo_path,
            "hfTokenSecret": (
                {"namespace": self.hf_token_secret_namespace, "name": self.hf_token_secret_name}
                if self.hf_token_secret_namespace and self.hf_token_secret_name
                else None
            ),
            "draft": self.draft,
            "gatewayProvider": self.gateway_provider,
            "gatewayNamespace": self.gateway_namespace,
            "gatewayName": self.gateway_name,
            "gatewayPublicUrl": self.gateway_public_url,
            "gatewayPort": self.gateway_port,
            "gatewayAuthzHost": self.gateway_authz_host,
            "inotifyMaxUserInstances": self.inotify_max_user_instances,
            "routerVersion": self.router_version,
            "gieVersion": self.gie_version,
            "ippVersion": self.ipp_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Cluster:
        proxy = data.get("proxy") if isinstance(data.get("proxy"), dict) else {}
        mode = str(proxy.get("mode") or "auto")
        if mode not in ("auto", "custom"):
            mode = "auto"
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            created_at=str(data.get("created_at") or ""),
            proxy_mode=mode,
            http_proxy=(proxy.get("httpProxy") or None),
            https_proxy=(proxy.get("httpsProxy") or None),
            no_proxy=(proxy.get("noProxy") or None),
            llm_d_ref=str(data.get("llmDRef")) if data.get("llmDRef") else None,
            llm_d_benchmark_ref=str(data.get("llmDBenchmarkRef")) if data.get("llmDBenchmarkRef") else None,
            llm_d_repo_path=str(data.get("llmDRepoPath")) if data.get("llmDRepoPath") else None,
            llm_d_benchmark_repo_path=(
                str(data.get("llmDBenchmarkRepoPath")) if data.get("llmDBenchmarkRepoPath") else None
            ),
            hf_token_secret_namespace=(
                str((data.get("hfTokenSecret") or {}).get("namespace"))
                if isinstance(data.get("hfTokenSecret"), dict) and (data.get("hfTokenSecret") or {}).get("namespace")
                else None
            ),
            hf_token_secret_name=(
                str((data.get("hfTokenSecret") or {}).get("name"))
                if isinstance(data.get("hfTokenSecret"), dict) and (data.get("hfTokenSecret") or {}).get("name")
                else None
            ),
            draft=bool(data.get("draft", False)),
            gateway_provider=str(data.get("gatewayProvider")) if data.get("gatewayProvider") else None,
            gateway_namespace=str(data.get("gatewayNamespace")) if data.get("gatewayNamespace") else None,
            gateway_name=str(data.get("gatewayName")) if data.get("gatewayName") else None,
            gateway_public_url=str(data.get("gatewayPublicUrl")) if data.get("gatewayPublicUrl") else None,
            gateway_port=int(data.get("gatewayPort")) if data.get("gatewayPort") else None,
            gateway_authz_host=str(data.get("gatewayAuthzHost")) if data.get("gatewayAuthzHost") else None,
            inotify_max_user_instances=(
                int(data.get("inotifyMaxUserInstances")) if data.get("inotifyMaxUserInstances") is not None else 8192
            ),
            router_version=str(data.get("routerVersion")) if data.get("routerVersion") else None,
            gie_version=str(data.get("gieVersion")) if data.get("gieVersion") else None,
            ipp_version=str(data.get("ippVersion")) if data.get("ippVersion") else None,
        )


def _dao() -> ClusterDao:
    # Lazily imported (see module docstring) to avoid an import cycle with
    # llm_d_bench.db.models.cluster. A fresh, stateless DAO per call,
    # always bound to whatever the current process-wide session factory is
    # (see llm_d_bench/db/engine.py); cheap, carries no state of its own.
    from llm_d_bench.db.dao.cluster import ClusterDao

    return ClusterDao()


def _kubeconfig_cache_directory() -> Path:
    directory = cluster_settings.data_directory / "kubeconfig_cache"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def kubeconfig_path(cluster_id: str) -> Path:
    """Materialize this cluster's kubeconfig (source of truth: the DB) to a local file.

    Callers that need a filesystem path (``KUBECONFIG`` env var for
    subprocess kubectl/helm invocations) get one here; the file is always
    rewritten from the current DB content, since the DB -- not this cache
    file -- is the source of truth.
    """
    path = _kubeconfig_cache_directory() / f"{cluster_id}.kubeconfig"
    text = _dao().read_kubeconfig(cluster_id)
    if text is None:
        path.unlink(missing_ok=True)
        return path
    path.write_text(text)
    os.chmod(path, 0o600)
    return path


def list_clusters(*, include_drafts: bool = False) -> list[Cluster]:
    return _dao().list(include_drafts=include_drafts)


def get_cluster(cluster_id: str) -> Cluster | None:
    return _dao().get(cluster_id)


def require_cluster(cluster_id: str) -> Cluster:
    cluster = get_cluster(cluster_id)
    if cluster is None:
        raise ClusterOverviewError("Unknown cluster", status_code=404, code="cluster_not_found")
    return cluster


def _name_taken(name: str, *, exclude_id: str | None = None) -> bool:
    """Whether another *non-draft* cluster already uses ``name``.

    Drafts are excluded: they're invisible scratch state for an in-progress
    wizard (see create_cluster's ``draft`` flag) and, unlike finalized
    clusters, aren't reliably cleaned up (e.g. the wizard tab is closed
    before the draft-delete request lands). Counting them here would let a
    stale/abandoned draft permanently squat a name and block real recreation
    with a spurious "already exists" error.
    """
    return _dao().name_taken(name, exclude_id=exclude_id)


def _require_unique_name(name: str, *, exclude_id: str | None = None) -> None:
    if _name_taken(name, exclude_id=exclude_id):
        raise ClusterOverviewError(
            f"A cluster named '{name.strip()}' already exists", status_code=409, code="cluster_name_conflict"
        )


def create_cluster(
    name: str,
    description: str,
    kubeconfig_text: str,
    *,
    proxy_mode: str = "auto",
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    llm_d_ref: str | None = None,
    llm_d_benchmark_ref: str | None = None,
    gateway_provider: str | None = None,
    gateway_namespace: str | None = None,
    gateway_name: str | None = None,
    gateway_public_url: str | None = None,
    gateway_port: int | None = None,
    gateway_authz_host: str | None = None,
    inotify_max_user_instances: int = 8192,
    router_version: str | None = None,
    gie_version: str | None = None,
    ipp_version: str | None = None,
    draft: bool = False,
) -> Cluster:
    _require_unique_name(name)
    return _dao().create(
        name,
        description,
        kubeconfig_text,
        proxy_mode=proxy_mode,
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        llm_d_ref=llm_d_ref,
        llm_d_benchmark_ref=llm_d_benchmark_ref,
        gateway_provider=gateway_provider,
        gateway_namespace=gateway_namespace,
        gateway_name=gateway_name,
        gateway_public_url=gateway_public_url,
        gateway_port=gateway_port,
        gateway_authz_host=gateway_authz_host,
        inotify_max_user_instances=inotify_max_user_instances,
        router_version=router_version,
        gie_version=gie_version,
        ipp_version=ipp_version,
        draft=draft,
    )


def update_cluster(cluster_id: str, **updates: Any) -> Cluster:
    """Patch a subset of a cluster's mutable settings (name/description/proxy/version
    refs) in place. Only keys present in ``updates`` are changed; anything else
    (created_at) is preserved from the existing record.
    """
    current = get_cluster(cluster_id)
    if current is None:
        raise ClusterOverviewError("Unknown cluster", status_code=404, code="cluster_not_found")
    if "name" in updates and updates["name"] is not None:
        _require_unique_name(updates["name"], exclude_id=cluster_id)
    # Drafts are excluded from the create-time name check, so two
    # concurrent wizards could both draft the same name; re-check here
    # when a draft finalizes (draft True -> False) so the second one to
    # finish still gets a clear conflict instead of a silent duplicate.
    elif current.draft and updates.get("draft") is False:
        _require_unique_name(current.name, exclude_id=cluster_id)
    # A ref update without an accompanying repo-path update means the ref
    # changed independently of a completed download (e.g. the wizard's
    # Step 3 PATCH firing before the download finishes) -- the previously
    # recorded path, if any, refers to the *old* ref and must not be kept.
    if "llm_d_ref" in updates and "llm_d_repo_path" not in updates and updates["llm_d_ref"] != current.llm_d_ref:
        updates = {**updates, "llm_d_repo_path": None}
    if (
        "llm_d_benchmark_ref" in updates
        and "llm_d_benchmark_repo_path" not in updates
        and updates["llm_d_benchmark_ref"] != current.llm_d_benchmark_ref
    ):
        updates = {**updates, "llm_d_benchmark_repo_path": None}
    updated = _dao().update(cluster_id, **updates)
    if updated is None:
        raise ClusterOverviewError("Unknown cluster", status_code=404, code="cluster_not_found")
    return updated


def read_kubeconfig(cluster_id: str) -> str | None:
    return _dao().read_kubeconfig(cluster_id)


def delete_cluster(cluster_id: str) -> bool:
    """Remove a cluster's DB row and any materialized kubeconfig cache file."""
    removed = _dao().delete(cluster_id)
    (_kubeconfig_cache_directory() / f"{cluster_id}.kubeconfig").unlink(missing_ok=True)
    return removed
