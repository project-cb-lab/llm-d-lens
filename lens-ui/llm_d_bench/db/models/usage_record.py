"""``usage_records`` table -- authoritative per-request usage ledger (counts only).

Backs ``llm_d_bench.model_service.contracts.UsageRecord``. This version stores
token counts and routing identity only; no pricing/cost columns (see design
section 1.3). Design reference: docs/design/model-service-v2-design.md section 4.5.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, UTCDateTime
from llm_d_bench.model_service.contracts import UsageRecord


class UsageRecordRow(Base, DeclarativeDtoMixin):
    __tablename__ = "usage_records"

    dto_type = UsageRecord
    column_map = {
        "id": "id",
        "request_id": "request_id",
        "user_id": "user_id",
        "token_id": "token_id",
        "group_id": "group_id",
        "group_name": "group_name",
        "cluster_id": "cluster_id",
        "execution_id": "execution_id",
        "model_ref": "model_ref",
        "provider": "provider",
        "input_tokens": "input_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "cache_write_tokens": "cache_write_tokens",
        "output_tokens": "output_tokens",
        "requests": "requests",
        "usage_source": "usage_source",
        "status": "status",
        "error_code": "error_code",
        "streaming": "streaming",
        "ttft_ms": "ttft_ms",
        "duration_ms": "duration_ms",
        "client_ip": "client_ip",
        "user_agent": "user_agent",
        "created_at": "created_at",
    }

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    token_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    group_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cluster_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    model_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="vllm")
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Requests this row accounts for. Per-request writers leave the default (1);
    #: the EPP metrics sync writes one aggregate row per (model, identity) and
    #: stores the request count here.
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    usage_source: Mapped[str] = mapped_column(String(16), nullable=False, default="engine")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    streaming: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        Index("uq_usage_records_request", "request_id", unique=True),
        Index("ix_usage_records_user_created", "user_id", "created_at"),
        Index("ix_usage_records_group_created", "group_id", "created_at"),
        Index("ix_usage_records_cluster_created", "cluster_id", "created_at"),
        Index("ix_usage_records_execution_created", "execution_id", "created_at"),
    )
