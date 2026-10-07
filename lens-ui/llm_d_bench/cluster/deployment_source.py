"""Resolve only source checkouts registered by the clusters API."""

from pathlib import Path


def resolve_cluster_deployment_source(cluster) -> dict:
    primary = Path(cluster.llm_d_repo_path).expanduser().resolve() if cluster.llm_d_repo_path else None
    if primary and (primary / "guides").is_dir():
        return {
            "resolved_repository": str(primary),
            "ref": cluster.llm_d_ref,
            "resolved_from": "cluster-registry",
        }

    raise ValueError(
        f'Cluster "{cluster.name or cluster.id}" has no available downloaded llm-d source. '
        "Configure and download its Software Versions before deploying."
    )


def resolve_cluster_benchmark_source(cluster) -> dict:
    primary = (
        Path(cluster.llm_d_benchmark_repo_path).expanduser().resolve() if cluster.llm_d_benchmark_repo_path else None
    )
    if primary and primary.is_dir():
        return {
            "resolved_repository": str(primary),
            "ref": cluster.llm_d_benchmark_ref or "",
            "resolved_from": "cluster-registry",
        }
    raise ValueError(
        f'Cluster "{cluster.name or cluster.id}" has no available downloaded llm-d-benchmark source. '
        "Configure and download its Software Versions before benchmarking."
    )
