"""add llm-d gateway mode columns to clusters and model service tables

Revision ID: c1f7a2b9d4e6
Revises: fce4d4e8f943
Create Date: 2026-09-24 16:10:00.000000

Adds the cluster-scoped llm-d Gateway Mode data plane columns:
- clusters: pinned provider + Gateway coordinates and component versions.
- model_service_groups: cluster scope, engine served name, IPP base model.
- model_service_members: deployment-owned InferencePool + EPP references.

Design reference: docs/design/model-service-llmd-routing-design.zh-CN.md
sections 5/6. All new columns are nullable so existing rows stay valid.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f7a2b9d4e6"
down_revision: str | Sequence[str] | None = "fce4d4e8f943"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("clusters", sa.Column("gateway_provider", sa.String(length=32), nullable=True))
    op.add_column("clusters", sa.Column("gateway_namespace", sa.String(length=253), nullable=True))
    op.add_column("clusters", sa.Column("gateway_name", sa.String(length=253), nullable=True))
    op.add_column("clusters", sa.Column("router_version", sa.String(length=64), nullable=True))
    op.add_column("clusters", sa.Column("gie_version", sa.String(length=64), nullable=True))
    op.add_column("clusters", sa.Column("ipp_version", sa.String(length=64), nullable=True))

    op.add_column("model_service_groups", sa.Column("cluster_id", sa.String(length=32), nullable=True))
    op.add_column("model_service_groups", sa.Column("served_name", sa.String(length=200), nullable=True))
    op.add_column("model_service_groups", sa.Column("base_model", sa.String(length=200), nullable=True))
    op.create_index(op.f("ix_model_service_groups_cluster_id"), "model_service_groups", ["cluster_id"], unique=False)
    # Model names are now unique per cluster rather than globally.
    op.drop_index("uq_model_service_groups_name", table_name="model_service_groups")
    op.create_index(
        "uq_model_service_groups_cluster_name",
        "model_service_groups",
        ["cluster_id", "name"],
        unique=True,
    )
    op.create_index("ix_model_service_groups_name", "model_service_groups", ["name"], unique=False)

    op.add_column("model_service_members", sa.Column("pool_name", sa.String(length=253), nullable=True))
    op.add_column("model_service_members", sa.Column("epp_ref", sa.String(length=253), nullable=True))

    # Backfill legacy rows so pre-existing model services keep routing: the
    # InferencePool is the release name behind the deployment's ``<release>-epp``
    # EPP Service, and groups inherit their scope/engine fields.
    op.execute(
        "UPDATE model_service_members "
        "SET pool_name = CASE WHEN epp_ref LIKE '%-epp' "
        "THEN substr(epp_ref, 1, length(epp_ref) - 4) ELSE epp_ref END "
        "WHERE pool_name IS NULL AND epp_ref IS NOT NULL"
    )
    op.execute("UPDATE model_service_groups SET served_name = model_ref WHERE served_name IS NULL")
    op.execute("UPDATE model_service_groups SET base_model = model_ref WHERE base_model IS NULL")
    op.execute(
        "UPDATE model_service_groups SET cluster_id = ("
        "SELECT m.cluster_id FROM model_service_members m "
        "WHERE m.group_id = model_service_groups.id ORDER BY m.id LIMIT 1"
        ") WHERE cluster_id IS NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("model_service_members", "epp_ref")
    op.drop_column("model_service_members", "pool_name")

    op.drop_index("ix_model_service_groups_name", table_name="model_service_groups")
    op.drop_index("uq_model_service_groups_cluster_name", table_name="model_service_groups")
    op.create_index("uq_model_service_groups_name", "model_service_groups", ["name"], unique=True)
    op.drop_index(op.f("ix_model_service_groups_cluster_id"), table_name="model_service_groups")
    op.drop_column("model_service_groups", "base_model")
    op.drop_column("model_service_groups", "served_name")
    op.drop_column("model_service_groups", "cluster_id")

    op.drop_column("clusters", "ipp_version")
    op.drop_column("clusters", "gie_version")
    op.drop_column("clusters", "router_version")
    op.drop_column("clusters", "gateway_name")
    op.drop_column("clusters", "gateway_namespace")
    op.drop_column("clusters", "gateway_provider")
