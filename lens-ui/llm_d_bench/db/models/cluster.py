"""``clusters`` table -- see design doc section 5.4.1.

Backs ``llm_d_bench.cluster.registry.Cluster``. Two columns intentionally
have no counterpart in that dataclass and are managed entirely by this
module instead of ``column_map``:

- ``kubeconfig`` -- the registry keeps kubeconfig text out of the API-facing
  DTO for auditability/least-exposure reasons (it's only ever returned via a
  dedicated, access-controlled path), so it is passed as an explicit
  ``extra_columns`` argument by ``ClusterDao`` rather than being a
  ``Cluster`` field.
- ``created_at``/``updated_at`` -- ``Cluster.created_at`` is an ISO-8601
  *string* (historical on-disk format), while the DB column is a real
  ``DateTime``; the conversion is done explicitly in ``to_dto``/the
  DAO rather than through the generic dot-path mapper.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.cluster.registry import Cluster
from llm_d_bench.db.base import Base, DeclarativeDtoMixin, UTCDateTime


class ClusterRow(Base, DeclarativeDtoMixin):
    __tablename__ = "clusters"

    dto_type = Cluster
    column_map = {
        "name": "name",
        "description": "description",
        "draft": "draft",
        "proxy_mode": "proxy_mode",
        "proxy_http_proxy": "http_proxy",
        "proxy_https_proxy": "https_proxy",
        "proxy_no_proxy": "no_proxy",
        "llm_d_ref": "llm_d_ref",
        "llm_d_benchmark_ref": "llm_d_benchmark_ref",
        "llm_d_repo_path": "llm_d_repo_path",
        "llm_d_benchmark_repo_path": "llm_d_benchmark_repo_path",
        "hf_token_secret_namespace": "hf_token_secret_namespace",
        "hf_token_secret_name": "hf_token_secret_name",
        "gateway_provider": "gateway_provider",
        "gateway_namespace": "gateway_namespace",
        "gateway_name": "gateway_name",
        "gateway_public_url": "gateway_public_url",
        "gateway_port": "gateway_port",
        "gateway_authz_host": "gateway_authz_host",
        "inotify_max_user_instances": "inotify_max_user_instances",
        "router_version": "router_version",
        "gie_version": "gie_version",
        "ipp_version": "ipp_version",
    }

    id: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Ownership metadata for resource-scope authorization (auth design
    # section 4.2.12). Set by the auth service on create; not part of the
    # API-facing Cluster DTO.
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kubeconfig: Mapped[str] = mapped_column(Text, nullable=False)
    draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proxy_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    proxy_http_proxy: Mapped[str | None] = mapped_column(Text, nullable=True)
    proxy_https_proxy: Mapped[str | None] = mapped_column(Text, nullable=True)
    proxy_no_proxy: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_d_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_d_benchmark_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_d_repo_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_d_benchmark_repo_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The wizard's HF_TOKEN secret step (see CreateClusterWizard.jsx) always
    # creates this secret up front and records its coordinates here so any
    # later Deploy/Evaluate flow can default to it (via copy_model_secret)
    # without asking the user to pick a secret again for every deployment.
    # The token itself is never stored outside the k8s Secret.
    hf_token_secret_namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    hf_token_secret_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
    # llm-d Gateway Mode data plane pinned per cluster (see registry.Cluster).
    gateway_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gateway_namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    gateway_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
    #: Externally reachable Gateway URL a client uses (e.g. https://gw.example.com/v1);
    #: the in-cluster Service address is not usable off-cluster.
    gateway_public_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Public NodePort the shared Gateway is exposed on (so clients reach it by
    #: node IP:port; DNS/domain binding is external).
    gateway_port: Mapped[int | None] = mapped_column(nullable=True)
    #: Host/IP the cluster can reach Lens' ext_authz endpoint on, for per-request
    #: identity injection. Auto-filled per cluster in the wizard/edit form; Lens
    #: adds its own live serving port.
    gateway_authz_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Per-uid inotify instance limit Lens applies to every node
    #: (``fs.inotify.max_user_instances``); kind's default of 128 is too small
    #: for a node running many pods.
    inotify_max_user_instances: Mapped[int] = mapped_column(nullable=False, default=8192, server_default="8192")
    router_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gie_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ipp_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        # Two *draft* clusters may share a name (see registry._name_taken's
        # docstring: drafts are excluded from the uniqueness check because
        # they're disposable scratch state for an in-progress wizard); only
        # non-draft rows must be unique. PostgreSQL and SQLite (3.8+) both
        # support partial unique indexes.
        Index(
            "uq_clusters_name_non_draft",
            "name",
            unique=True,
            postgresql_where=(draft.is_(False)),
            sqlite_where=(draft.is_(False)),
        ),
    )

    def to_dto(self) -> Cluster:
        values = {path: getattr(self, col) for col, path in self.column_map.items()}
        values["id"] = self.id
        values["created_at"] = self.created_at.isoformat()
        return Cluster(**values)
