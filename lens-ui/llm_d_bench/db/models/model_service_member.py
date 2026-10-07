"""``model_service_members`` table -- a published deployment backing a group.

Backs ``llm_d_bench.model_service.contracts.ModelServiceMember``.
Design reference: docs/design/model-service-v2-design.md section 4.3.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_bench.model_service.contracts import ModelServiceMember


class ModelServiceMemberRow(Base, DeclarativeDtoMixin):
    __tablename__ = "model_service_members"

    dto_type = ModelServiceMember
    column_map = {
        "id": "id",
        "group_id": "group_id",
        "execution_id": "execution_id",
        "cluster_id": "cluster_id",
        "target_namespace": "target_namespace",
        "target_service": "target_service",
        "target_port": "target_port",
        "endpoint_kind": "endpoint_kind",
        "pool_name": "pool_name",
        "epp_ref": "epp_ref",
        "status": "status",
        "health_json": "health_json",
        "last_health_at": "last_health_at",
        "owner_user_id": "owner_user_id",
        "owner_group_id": "owner_group_id",
        "published_by_user_id": "published_by_user_id",
        "created_at": "created_at",
        "updated_at": "updated_at",
    }

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("model_service_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cluster_id: Mapped[str] = mapped_column(String(32), nullable=False)
    target_namespace: Mapped[str] = mapped_column(String(253), nullable=False)
    target_service: Mapped[str] = mapped_column(String(253), nullable=False)
    target_port: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="vllm")
    # llm-d Gateway Mode: the deployment-owned InferencePool + EPP this member maps to.
    pool_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
    epp_ref: Mapped[str | None] = mapped_column(String(253), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    health_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_group_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    published_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        Index("uq_model_service_members_group_execution", "group_id", "execution_id", unique=True),
        Index("ix_model_service_members_group_status", "group_id", "status"),
        Index("ix_model_service_members_cluster", "cluster_id"),
    )
