"""``ai_providers`` table -- see design doc section 5.4.4.

Backs ``llm_d_bench.ai_providers.contracts.AIProvider``. Global configuration
(no ``cluster_id``); every field is a scalar. ``base_url`` is overridden
because it's a Pydantic ``HttpUrl`` on the DTO side, not a plain ``str`` --
it must be stringified before binding it to a ``String`` column, and the
DB's ``str`` is re-validated back into ``HttpUrl`` by ``AIProvider.model_validate``
on the way out. It (and ``id``) are deliberately excluded from ``column_map``
and handled explicitly in ``from_dto``/``sync_from_dto``/``to_dto`` instead.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.ai_providers.contracts import AIProvider
from llm_d_bench.db.base import Base, DeclarativeDtoMixin, UTCDateTime


class AIProviderRow(Base, DeclarativeDtoMixin):
    __tablename__ = "ai_providers"

    dto_type = AIProvider
    column_map = {
        "name": "name",
        "model": "model",
        "api_key": "api_key",
        "provider_type": "provider_type",
        "timeout_seconds": "timeout_seconds",
        # created_at/updated_at come straight from the DTO (default_factory
        # set at construction time, never mutated afterward except through
        # this repository's own save() -- see cluster.py's docstring for the
        # analogous case) rather than a server-generated timestamp.
        "created_at": "created_at",
        "updated_at": "updated_at",
    }

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    provider_type: Mapped[str] = mapped_column(String(16), nullable=False, default="openai")
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=30)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (Index("uq_ai_providers_name", "name", unique=True),)

    @classmethod
    def from_dto(cls, dto: AIProvider, **extra_columns: object) -> AIProviderRow:
        values = {col: getattr(dto, path) for col, path in cls.column_map.items()}
        values["id"] = dto.id
        values["base_url"] = str(dto.base_url)
        values.update(extra_columns)
        return cls(**values)

    def sync_from_dto(self, dto: AIProvider, **extra_columns: object) -> None:
        for col, path in self.column_map.items():
            setattr(self, col, getattr(dto, path))
        self.base_url = str(dto.base_url)
        for col, value in extra_columns.items():
            setattr(self, col, value)

    def to_dto(self) -> AIProvider:
        values: dict[str, object] = {path: getattr(self, col) for col, path in self.column_map.items()}
        values["id"] = self.id
        values["base_url"] = self.base_url
        return AIProvider.model_validate(values)
