"""create model service tables

Revision ID: 9248b444b5d7
Revises: 54d6ef61bedf
Create Date: 2026-09-21 05:10:07.739393

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "9248b444b5d7"
down_revision: str | Sequence[str] | None = "54d6ef61bedf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSON, but native JSONB on PostgreSQL -- mirrors llm_d_bench.db.base.JSONVariant.
JSONVariant = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "model_access_tokens",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_hint", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_access_tokens_user", "model_access_tokens", ["user_id", "status"], unique=False)
    op.create_index(op.f("ix_model_access_tokens_user_id"), "model_access_tokens", ["user_id"], unique=False)
    op.create_index("uq_model_access_tokens_hash", "model_access_tokens", ["token_hash"], unique=True)
    op.create_table(
        "model_service_groups",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("model_ref", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("selection_policy", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_model_service_groups_created_by_user_id"),
        "model_service_groups",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index("uq_model_service_groups_name", "model_service_groups", ["name"], unique=True)
    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("token_id", sa.String(length=48), nullable=True),
        sa.Column("group_id", sa.String(length=48), nullable=True),
        sa.Column("group_name", sa.String(length=100), nullable=True),
        sa.Column("cluster_id", sa.String(length=32), nullable=True),
        sa.Column("execution_id", sa.String(length=64), nullable=True),
        sa.Column("model_ref", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("streaming", sa.Boolean(), nullable=False),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_records_cluster_created", "usage_records", ["cluster_id", "created_at"], unique=False)
    op.create_index(op.f("ix_usage_records_cluster_id"), "usage_records", ["cluster_id"], unique=False)
    op.create_index("ix_usage_records_execution_created", "usage_records", ["execution_id", "created_at"], unique=False)
    op.create_index(op.f("ix_usage_records_execution_id"), "usage_records", ["execution_id"], unique=False)
    op.create_index("ix_usage_records_group_created", "usage_records", ["group_id", "created_at"], unique=False)
    op.create_index(op.f("ix_usage_records_group_id"), "usage_records", ["group_id"], unique=False)
    op.create_index(op.f("ix_usage_records_token_id"), "usage_records", ["token_id"], unique=False)
    op.create_index("ix_usage_records_user_created", "usage_records", ["user_id", "created_at"], unique=False)
    op.create_index(op.f("ix_usage_records_user_id"), "usage_records", ["user_id"], unique=False)
    op.create_index("uq_usage_records_request", "usage_records", ["request_id"], unique=True)
    op.create_table(
        "model_service_members",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("group_id", sa.String(length=48), nullable=False),
        sa.Column("execution_id", sa.String(length=64), nullable=False),
        sa.Column("cluster_id", sa.String(length=32), nullable=False),
        sa.Column("target_namespace", sa.String(length=253), nullable=False),
        sa.Column("target_service", sa.String(length=253), nullable=False),
        sa.Column("target_port", sa.Integer(), nullable=False),
        sa.Column("endpoint_kind", sa.String(length=32), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("health_json", JSONVariant, nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        sa.Column("owner_group_id", sa.String(length=48), nullable=True),
        sa.Column("published_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["model_service_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_service_members_cluster", "model_service_members", ["cluster_id"], unique=False)
    op.create_index(op.f("ix_model_service_members_group_id"), "model_service_members", ["group_id"], unique=False)
    op.create_index(
        "ix_model_service_members_group_status", "model_service_members", ["group_id", "status"], unique=False
    )
    op.create_index(
        op.f("ix_model_service_members_owner_user_id"), "model_service_members", ["owner_user_id"], unique=False
    )
    op.create_index(
        "uq_model_service_members_group_execution",
        "model_service_members",
        ["group_id", "execution_id"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_model_service_members_group_execution", table_name="model_service_members")
    op.drop_index(op.f("ix_model_service_members_owner_user_id"), table_name="model_service_members")
    op.drop_index("ix_model_service_members_group_status", table_name="model_service_members")
    op.drop_index(op.f("ix_model_service_members_group_id"), table_name="model_service_members")
    op.drop_index("ix_model_service_members_cluster", table_name="model_service_members")
    op.drop_table("model_service_members")
    op.drop_index("uq_usage_records_request", table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_user_id"), table_name="usage_records")
    op.drop_index("ix_usage_records_user_created", table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_token_id"), table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_group_id"), table_name="usage_records")
    op.drop_index("ix_usage_records_group_created", table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_execution_id"), table_name="usage_records")
    op.drop_index("ix_usage_records_execution_created", table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_cluster_id"), table_name="usage_records")
    op.drop_index("ix_usage_records_cluster_created", table_name="usage_records")
    op.drop_table("usage_records")
    op.drop_index("uq_model_service_groups_name", table_name="model_service_groups")
    op.drop_index(op.f("ix_model_service_groups_created_by_user_id"), table_name="model_service_groups")
    op.drop_table("model_service_groups")
    op.drop_index("uq_model_access_tokens_hash", table_name="model_access_tokens")
    op.drop_index(op.f("ix_model_access_tokens_user_id"), table_name="model_access_tokens")
    op.drop_index("ix_model_access_tokens_user", table_name="model_access_tokens")
    op.drop_table("model_access_tokens")
