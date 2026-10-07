"""Resolve monitoring recipes only from the cluster's registered llm-d checkout."""

from __future__ import annotations

from pathlib import Path

_INSTALLER_RELATIVE = "guides/recipes/observability/install-prometheus-grafana.sh"


def _repo_root_from_cluster(cluster_id: str | None) -> Path | None:
    """The cluster's own pinned llm-d checkout, if it has downloaded one.

    Imported lazily to avoid a circular import: ``llm_d_bench.cluster``'s
    package __init__ pulls in ``cluster/service.py``, which imports from
    ``llm_d_bench.monitoring.cluster_stack`` (this module's parent package)
    at module load time -- importing ``llm_d_bench.cluster.registry`` at the
    top of this file would re-enter ``llm_d_bench.cluster``'s __init__ while
    it's still mid-import.
    """
    if not cluster_id:
        return None
    from llm_d_bench.cluster.registry import get_cluster

    cluster = get_cluster(cluster_id)
    if cluster is None or not cluster.llm_d_repo_path:
        return None
    path = Path(cluster.llm_d_repo_path).expanduser().resolve()
    return path if _is_repo(path) else None


def _is_repo(path: Path) -> bool:
    return (path / _INSTALLER_RELATIVE).is_file()


async def repo_root(cluster_id: str | None = None) -> Path | None:
    """Return the cluster checkout, or None until Software Versions is downloaded."""
    return _repo_root_from_cluster(cluster_id)
