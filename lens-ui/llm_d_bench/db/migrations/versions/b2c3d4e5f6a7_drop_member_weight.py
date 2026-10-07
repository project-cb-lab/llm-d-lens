"""drop weight from model_service_members

Revision ID: b2c3d4e5f6a7
Revises: a1f2b3c4d5e6
Create Date: 2026-09-26 04:10:00.000000

llm-d Gateway Mode renders one InferencePool per HTTPRoute (no weighted
``backendRefs``), so a member's ``weight`` no longer affects routing. Provider
precedence is expressed with ``priority``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1f2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("model_service_members", "weight")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "model_service_members",
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
    )
