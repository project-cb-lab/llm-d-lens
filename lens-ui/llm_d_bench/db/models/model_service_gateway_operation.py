"""``model_service_gateway_operations`` table -- gateway ops audit/status.

Backs ``llm_d_bench.model_service.gateway_contracts.GatewayOperation``.
Design reference: docs/design/model-service-gateway-deployment.md sections 4-6.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_bench.model_service.gateway_contracts import GatewayOperation


class ModelServiceGatewayOperationRow(Base, DeclarativeDtoMixin):
    __tablename__ = "model_service_gateway_operations"

    dto_type = GatewayOperation
    column_map = {
        "id": "id",
        "kind": "kind",
        "status": "status",
        "cluster_id": "cluster_id",
        "message": "message",
        "detail_json": "detail_json",
        "created_by_user_id": "created_by_user_id",
        "created_at": "created_at",
        "finished_at": "finished_at",
    }

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    cluster_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (Index("ix_model_service_gateway_ops_created", "created_at"),)
