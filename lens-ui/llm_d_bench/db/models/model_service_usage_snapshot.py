"""``model_service_usage_snapshots`` table -- last EPP counters per cluster.

One row per cluster stores the cumulative llm-d EPP token counters observed at
the last scrape. The usage sync diffs successive snapshots to write deltas into
``usage_records`` (the EPP counters reset when the EPP restarts, which the diff
handles). Backs ``llm_d_bench.model_service.contracts.EppUsageSnapshot``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_bench.model_service.contracts import EppUsageSnapshot


class ModelServiceUsageSnapshotRow(Base, DeclarativeDtoMixin):
    __tablename__ = "model_service_usage_snapshots"

    dto_type = EppUsageSnapshot
    column_map = {
        "cluster_id": "cluster_id",
        "captured_at": "captured_at",
        "counters": "counters",
    }

    cluster_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    counters: Mapped[dict] = mapped_column(JSONVariant, nullable=False, default=dict)
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
