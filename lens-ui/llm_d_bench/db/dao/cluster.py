"""DAO backing ``llm_d_bench.cluster.registry`` -- see design doc section 5.4.1."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from llm_d_bench.cluster.registry import Cluster
from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.cluster import ClusterRow


class ClusterDaoError(Exception):
    """Raised for cluster-repository failures that aren't already a domain error."""


class ClusterDao(BaseDao):
    def create(
        self,
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
        with self._transaction() as session:
            cluster_id = self._new_cluster_id(session)
            created_at = datetime.now(UTC)
            row = ClusterRow(
                id=cluster_id,
                name=name,
                description=description,
                kubeconfig=kubeconfig_text,
                draft=draft,
                proxy_mode=proxy_mode if proxy_mode in ("auto", "custom") else "auto",
                proxy_http_proxy=http_proxy or None,
                proxy_https_proxy=https_proxy or None,
                proxy_no_proxy=no_proxy or None,
                llm_d_ref=llm_d_ref or None,
                llm_d_benchmark_ref=llm_d_benchmark_ref or None,
                gateway_provider=gateway_provider or None,
                gateway_namespace=gateway_namespace or None,
                gateway_name=gateway_name or None,
                gateway_public_url=gateway_public_url or None,
                gateway_port=gateway_port or None,
                gateway_authz_host=gateway_authz_host or None,
                inotify_max_user_instances=inotify_max_user_instances,
                router_version=router_version or None,
                gie_version=gie_version or None,
                ipp_version=ipp_version or None,
                created_at=created_at,
            )
            session.add(row)
            session.flush()
            return row.to_dto()

    def _new_cluster_id(self, session) -> str:  # noqa: ANN001
        while True:
            candidate = uuid4().hex[:8]
            if session.get(ClusterRow, candidate) is None:
                return candidate

    def list(self, *, include_drafts: bool = False) -> list[Cluster]:
        with self._read_only() as session:
            stmt = select(ClusterRow).order_by(ClusterRow.created_at, ClusterRow.id)
            if not include_drafts:
                stmt = stmt.where(ClusterRow.draft.is_(False))
            return [row.to_dto() for row in session.scalars(stmt)]

    def get(self, cluster_id: str) -> Cluster | None:
        with self._read_only() as session:
            row = session.get(ClusterRow, cluster_id)
            return row.to_dto() if row else None

    def get_row(self, cluster_id: str) -> ClusterRow | None:
        """Escape hatch for callers (e.g. update_cluster) that need to see ``kubeconfig``."""
        with self._read_only() as session:
            return session.get(ClusterRow, cluster_id)

    def read_kubeconfig(self, cluster_id: str) -> str | None:
        with self._read_only() as session:
            row = session.get(ClusterRow, cluster_id)
            return row.kubeconfig if row else None

    def update(self, cluster_id: str, **updates: object) -> Cluster | None:
        """Patch a subset of a cluster's mutable columns; unset keys are left untouched."""
        with self._transaction() as session:
            row = session.get(ClusterRow, cluster_id)
            if row is None:
                return None
            for column in (
                "name",
                "description",
                "proxy_mode",
                "llm_d_ref",
                "llm_d_benchmark_ref",
                "llm_d_repo_path",
                "llm_d_benchmark_repo_path",
                "hf_token_secret_namespace",
                "hf_token_secret_name",
                "gateway_provider",
                "gateway_namespace",
                "gateway_name",
                "gateway_public_url",
                "gateway_port",
                "gateway_authz_host",
                "router_version",
                "gie_version",
                "ipp_version",
                "draft",
            ):
                if column in updates:
                    setattr(row, column, updates[column])
            # Public kwarg names (http_proxy/...) differ from column names
            # (proxy_http_proxy/...); map them explicitly rather than adding
            # them to the loop above.
            for public_name, column_name in (
                ("http_proxy", "proxy_http_proxy"),
                ("https_proxy", "proxy_https_proxy"),
                ("no_proxy", "proxy_no_proxy"),
            ):
                if public_name in updates:
                    setattr(row, column_name, updates[public_name])
            if "kubeconfig" in updates:
                row.kubeconfig = updates["kubeconfig"]
            return row.to_dto()

    def delete(self, cluster_id: str) -> bool:
        with self._transaction() as session:
            row = session.get(ClusterRow, cluster_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def name_taken(self, name: str, *, exclude_id: str | None = None) -> bool:
        with self._read_only() as session:
            stmt = select(ClusterRow.id).where(
                ClusterRow.draft.is_(False), func.lower(ClusterRow.name) == name.strip().lower()
            )
            if exclude_id is not None:
                stmt = stmt.where(ClusterRow.id != exclude_id)
            return session.execute(stmt).first() is not None
