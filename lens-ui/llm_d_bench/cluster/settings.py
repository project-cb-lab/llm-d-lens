"""Application settings for the Cluster session manager and overview service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from llm_d_bench.utils.paths import storage_path


@dataclass(frozen=True)
class ClusterSettings:
    session_directory: Path
    namespace_prefix: str
    data_directory: Path

    @property
    def clusters_directory(self) -> Path:
        return self.data_directory / "clusters"

    @classmethod
    def from_environment(cls) -> ClusterSettings:
        data_directory = storage_path("data", "credentials", "clusters")
        session_directory = data_directory / "cluster_sessions"
        namespace_prefix = os.environ.get("PRISM_CLUSTER_NAMESPACE_PREFIX", "llm-d-bench-")
        return cls(session_directory, namespace_prefix, data_directory)


cluster_settings = ClusterSettings.from_environment()
