"""composite primary key for evaluate workflow cases

An embedded case ``id`` (e.g. ``guide-1-1``, ``baseline-1``) is only unique
within its workflow, but the table used it as the sole primary key, so a second
evaluation workflow with the same case shape failed on
``evaluate_workflow_cases_pkey``. The primary key becomes the composite
``(workflow_id, id)``, matching the actual uniqueness of the business id.

``batch_alter_table`` is used so the same migration applies on PostgreSQL
(direct DDL) and on the SQLite scratch database; SQLite cannot ALTER a primary
key, so it recreates the table there.

Revision ID: 54d6ef61bedf
Revises: b7e2a4c1d9f0
Create Date: 2026-09-19

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "54d6ef61bedf"
down_revision: str | Sequence[str] | None = "b7e2a4c1d9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "evaluate_workflow_cases"
_PK_NAME = "evaluate_workflow_cases_pkey"
_NEW_COLUMNS = ["workflow_id", "id"]
_OLD_COLUMNS = ["id"]


def _replace_primary_key(columns: list[str]) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite has no ALTER for primary keys; recreate the table.
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.drop_constraint(type_="primary")
            batch.create_primary_key(None, columns)
        return
    op.drop_constraint(_PK_NAME, _TABLE, type_="primary")
    op.create_primary_key(_PK_NAME, _TABLE, columns)


def upgrade() -> None:
    """Use the composite (workflow_id, id) primary key."""
    _replace_primary_key(_NEW_COLUMNS)


def downgrade() -> None:
    """Restore the single-column (id) primary key.

    Only valid when no two workflows share a case id (the schema this migration
    fixes exists to allow that, so a downgrade may need data cleanup first).
    """
    _replace_primary_key(_OLD_COLUMNS)
