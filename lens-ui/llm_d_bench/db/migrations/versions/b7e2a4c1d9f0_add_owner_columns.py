"""add owner columns to resource tables

Adds ``owner_user_id`` (and ``owner_group_id`` where team ownership applies)
to existing resource tables, per docs/design/auth-rbac-design.md section
4.2.12. Columns are nullable with ``ON DELETE SET NULL`` so existing rows are
unaffected (they simply have no owner and remain visible only via role scope).

``batch_alter_table`` is used so the same migration applies on PostgreSQL
(direct ALTER) and on the SQLite scratch database used to verify it.

Revision ID: b7e2a4c1d9f0
Revises: 9c1f0a7b4d22
Create Date: 2026-09-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2a4c1d9f0"
down_revision: str | Sequence[str] | None = "9c1f0a7b4d22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, columns) -- owner_user_id references users, owner_group_id references groups.
_OWNER_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("clusters", ("owner_user_id",)),
    ("deployment_batches", ("owner_user_id", "owner_group_id")),
    ("deployment_evidences", ("owner_user_id", "owner_group_id")),
    ("configuration_artifacts", ("owner_user_id", "owner_group_id")),
    ("evaluate_runs", ("owner_user_id", "owner_group_id")),
    ("evaluate_workflows", ("owner_user_id", "owner_group_id")),
    ("simulation_tasks", ("owner_user_id", "owner_group_id")),
    ("storage_volumes", ("owner_user_id",)),
    ("model_cache_entries", ("owner_user_id",)),
)

_REFERENCED_TABLE = {"owner_user_id": "users", "owner_group_id": "groups"}


def upgrade() -> None:
    """Add nullable owner columns, indexes and SET NULL foreign keys."""
    for table, columns in _OWNER_COLUMNS:
        with op.batch_alter_table(table) as batch:
            for column in columns:
                batch.add_column(sa.Column(column, sa.String(length=36), nullable=True))
                batch.create_index(f"ix_{table}_{column}", [column])
                batch.create_foreign_key(
                    f"fk_{table}_{column}",
                    _REFERENCED_TABLE[column],
                    [column],
                    ["id"],
                    ondelete="SET NULL",
                )


def downgrade() -> None:
    """Drop the owner constraints, indexes and columns in reverse order."""
    for table, columns in reversed(_OWNER_COLUMNS):
        with op.batch_alter_table(table) as batch:
            for column in reversed(columns):
                batch.drop_constraint(f"fk_{table}_{column}", type_="foreignkey")
                batch.drop_index(f"ix_{table}_{column}")
                batch.drop_column(column)
