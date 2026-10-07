"""``model_service_groups`` table -- the user-facing model name (routing group).

Backs ``llm_d_bench.model_service.contracts.ModelServiceGroup``.
Design reference: docs/design/model-service-v2-design.md section 4.2.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, UTCDateTime
from llm_d_bench.model_service.contracts import ModelServiceGroup


class ModelServiceGroupRow(Base, DeclarativeDtoMixin):
    __tablename__ = "model_service_groups"

    dto_type = ModelServiceGroup
    column_map = {
        "id": "id",
        "name": "name",
        "model_ref": "model_ref",
        "display_name": "display_name",
        "description": "description",
        "selection_policy": "selection_policy",
        "status": "status",
        "cluster_id": "cluster_id",
        "served_name": "served_name",
        "base_model": "base_model",
        "created_by_user_id": "created_by_user_id",
        "created_at": "created_at",
        "updated_at": "updated_at",
    }

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    selection_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="random")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # llm-d Gateway Mode (design section 5/6): cluster-scoped public model, engine
    # served name and IPP base-model mapping key.
    cluster_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    served_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    base_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        Index("uq_model_service_groups_cluster_name", "cluster_id", "name", unique=True),
        Index("ix_model_service_groups_name", "name"),
    )
