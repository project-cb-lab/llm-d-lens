"""``deployment_batches`` table -- see design doc section 5.4.6.

Backs ``llm_d_bench.deploy.contracts.DeploymentRun``. ``cases`` is excluded
from ``column_map`` because rule 5 splits it into the child
``deployment_jobs`` table; ``to_dto()`` reassembles the aggregate by reading
the ORM relationship in ordinal order.

Note on ``cluster_id``: the design doc specifies a real
``clusters.id`` foreign key with ``SET NULL``. It's intentionally left as a
plain indexed column here, matching the earlier deviations on
``StorageVolumeRow``/``ConfigurationArtifactRow``/``ModelCacheEntryRow``:
Deploy tests and current call sites treat cluster identity as an opaque
string carried in provenance (today usually ``cluster_server_id``), not as a
guaranteed persisted ``clusters`` row.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_bench.deploy.contracts import DeployableConfiguration, DeploymentRun

if TYPE_CHECKING:
    from llm_d_bench.db.models.deployment_job import DeploymentCaseRow


class DeploymentRunRow(Base, DeclarativeDtoMixin):
    __tablename__ = "deployment_batches"

    dto_type = DeploymentRun
    column_map = {
        "status": "status",
        "failure_policy": "failure_policy",
        "provenance": "provenance",
        "created_at": "created_at",
        "started_at": "started_at",
        "finished_at": "finished_at",
    }

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    source_configurations: Mapped[list[dict[str, Any]]] = mapped_column(JSONVariant, nullable=False)
    failure_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="continue")
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    cluster_id: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    jobs: Mapped[list[DeploymentCaseRow]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="(DeploymentCaseRow.ordinal, DeploymentCaseRow.attempt, DeploymentCaseRow.id)",
    )

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (Index("ix_deployment_batches_created_at", "created_at"),)

    @classmethod
    def from_dto(cls, dto: DeploymentRun, **extra_columns: object) -> DeploymentRunRow:
        row = super().from_dto(dto, **extra_columns)
        row.id = dto.id
        row.source_configurations = _configurations_to_json(dto.source_configurations)
        return row

    def sync_from_dto(self, dto: DeploymentRun, **extra_columns: object) -> None:
        super().sync_from_dto(dto, **extra_columns)
        self.source_configurations = _configurations_to_json(dto.source_configurations)

    def to_dto(self) -> DeploymentRun:
        payload: dict[str, object] = {path: getattr(self, col) for col, path in self.column_map.items()}
        payload["id"] = self.id
        payload["source_configurations"] = self.source_configurations
        payload["cases"] = [job.to_dto().model_dump(mode="json") for job in self.jobs]
        return DeploymentRun.model_validate(payload)


def _configurations_to_json(configurations: list[DeployableConfiguration]) -> list[dict[str, Any]]:
    return [configuration.model_dump(mode="json") for configuration in configurations]
