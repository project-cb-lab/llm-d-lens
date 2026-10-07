"""drop priority from model_service_members

Revision ID: d4e5f6a7b8c9
Revises: b2c3d4e5f6a7
Create Date: 2026-09-26 05:40:00.000000

A model service routes to one deployment-owned InferencePool, so a member's
``priority`` no longer selects the serving pool.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("model_service_members", "priority")


def downgrade() -> None:
    op.add_column(
        "model_service_members",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )
