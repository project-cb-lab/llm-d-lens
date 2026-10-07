"""Declarative base, DTO<->DAO mapping mixin, and portable column types.

See docs/design/sqlalchemy-data-access-layer-design.md section 6 for the
rationale: every DAO Row class declares a ``column_map`` (db column name ->
dot-path into its Pydantic/dataclass DTO) and gets ``from_dto``/``to_dto``/
``sync_from_dto`` for free, instead of hand-written field-by-field glue code
repeated across 15 tables.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar, Self

from sqlalchemy import ARRAY, JSON, DateTime, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

# Portable "JSON, but native JSONB on PostgreSQL" column type -- see design
# doc section 5.2 rule 3/6. On PostgreSQL this is a real JSONB column; on
# every other dialect (notably the SQLite backend used in tests) it degrades
# to a plain JSON column.
JSONVariant = JSON().with_variant(JSONB, "postgresql")

# Portable "native array on PostgreSQL, JSON elsewhere" column type -- see
# design doc section 5.2 rule 4. SQLite has no array type at all, so it
# degrades to JSON (a JSON list is a reasonable stand-in for tests).
ArrayVariant = ARRAY(Text).with_variant(JSON, "sqlite")


class UTCDateTime(TypeDecorator):
    """``DateTime(timezone=True)``, but round-trips aware ``datetime``s even on SQLite.

    PostgreSQL's ``timestamptz`` preserves timezone-awareness natively, but
    SQLite (the test-suite backend -- see design doc section 6.7.6) has no
    timezone-aware timestamp type and silently returns a naive ``datetime``
    on read, which breaks DTOs that require/round-trip an aware
    ``datetime`` (e.g. ``StorageVolume.created_at``). This type stores
    everything normalized to UTC and always re-attaches ``tzinfo=UTC`` on
    the way out, so every dialect behaves the same.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return value
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    """Shared declarative base for every Row class in llm_d_bench.db.models."""


def _dget(obj: Any, path: str) -> Any:
    """Read a dot-path off a DTO instance, short-circuiting on ``None``.

    Enum values are unwrapped to their plain ``.value`` so they persist as
    the same string/int the database CHECK constraints expect.
    """
    for part in path.split("."):
        if obj is None:
            return None
        obj = getattr(obj, part)
    return obj.value if isinstance(obj, Enum) else obj


def _dset(root: dict[str, Any], path: str, value: Any) -> None:
    """Write ``value`` into ``root`` at a dot-path, creating dicts along the way.

    This is the inverse of ``_dget``'s traversal, used by ``to_dto`` to
    reassemble the nested DTO shape from a flat set of mapped columns.
    """
    *parents, leaf = path.split(".")
    node = root
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value


class DeclarativeDtoMixin:
    """Gives a Row class bidirectional conversion to/from its DTO via ``column_map``.

    Subclasses declare:
      dto_type    -- the Pydantic BaseModel (or plain dataclass) this table
                      round-trips to/from.
      column_map  -- {db column name: dot-path into the DTO} covering every
                      business column except the primary key and any
                      "derived" columns resolved outside the DTO (see design
                      doc section 6.4), which are passed as ``extra_columns``
                      instead.
    """

    dto_type: ClassVar[type[Any]]
    column_map: ClassVar[dict[str, str]] = {}

    @classmethod
    def from_dto(cls, dto: Any, **extra_columns: Any) -> Self:
        """Build a new (not yet persisted) Row from a validated DTO instance."""
        values = {col: _dget(dto, path) for col, path in cls.column_map.items()}
        values.update(extra_columns)
        return cls(**values)

    def sync_from_dto(self, dto: Any, **extra_columns: Any) -> None:
        """In-place UPDATE: refresh every mapped column from a new DTO instance."""
        for col, path in self.column_map.items():
            setattr(self, col, _dget(dto, path))
        for col, value in extra_columns.items():
            setattr(self, col, value)

    def to_dto(self) -> Any:
        """Reassemble a DTO instance from this Row's mapped columns."""
        nested: dict[str, Any] = {}
        for col, path in self.column_map.items():
            _dset(nested, path, getattr(self, col))
        dto_type = self.dto_type
        model_validate = getattr(dto_type, "model_validate", None)
        if model_validate is not None:  # Pydantic BaseModel
            return model_validate(nested)
        return dto_type(**nested)  # plain dataclass (e.g. Cluster)
