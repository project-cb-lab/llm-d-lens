"""add requests count to usage_records

Revision ID: f4c0d6e2a8b3
Revises: e3b9c5d1f7a2
Create Date: 2026-09-25 21:20:00.000000

The llm-d EPP metrics sync writes one aggregate row per (model, identity) pair,
so the ledger stores how many requests that row accounts for. Per-request
writers keep the default of 1.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4c0d6e2a8b3"
down_revision: str | Sequence[str] | None = "e3b9c5d1f7a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "usage_records",
        sa.Column("requests", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("usage_records", "requests")
