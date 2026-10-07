"""create model service gateway operations

Revision ID: fce4d4e8f943
Revises: 9248b444b5d7
Create Date: 2026-09-21 05:39:49.129510

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "fce4d4e8f943"
down_revision: str | Sequence[str] | None = "9248b444b5d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSON, but native JSONB on PostgreSQL -- mirrors llm_d_bench.db.base.JSONVariant.
JSONVariant = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "model_service_gateway_operations",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("cluster_id", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail_json", JSONVariant, nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_model_service_gateway_operations_cluster_id"),
        "model_service_gateway_operations",
        ["cluster_id"],
        unique=False,
    )
    op.create_index(
        "ix_model_service_gateway_ops_created", "model_service_gateway_operations", ["created_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_model_service_gateway_ops_created", table_name="model_service_gateway_operations")
    op.drop_index(op.f("ix_model_service_gateway_operations_cluster_id"), table_name="model_service_gateway_operations")
    op.drop_table("model_service_gateway_operations")
