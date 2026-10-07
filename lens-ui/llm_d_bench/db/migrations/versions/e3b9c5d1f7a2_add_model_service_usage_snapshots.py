"""create model_service_usage_snapshots

Revision ID: e3b9c5d1f7a2
Revises: d2a8b4c7e9f1
Create Date: 2026-09-25 21:10:00.000000

Stores the last cumulative llm-d EPP token counters per cluster, so the usage
sync can diff scrapes into ``usage_records`` deltas (llm-d Gateway Mode has no
per-request usage callback).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# JSON, but native JSONB on PostgreSQL -- mirrors llm_d_bench.db.base.JSONVariant.
JSONVariant = sa.JSON().with_variant(JSONB, "postgresql")

# revision identifiers, used by Alembic.
revision: str = "e3b9c5d1f7a2"
down_revision: str | Sequence[str] | None = "d2a8b4c7e9f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "model_service_usage_snapshots",
        sa.Column("cluster_id", sa.String(length=32), primary_key=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("counters", JSONVariant, nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("model_service_usage_snapshots")
