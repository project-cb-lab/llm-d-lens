"""add gateway_public_url to clusters

Revision ID: d2a8b4c7e9f1
Revises: c1f7a2b9d4e6
Create Date: 2026-09-25 18:30:00.000000

Adds ``clusters.gateway_public_url``: the externally reachable URL clients use
for a cluster's shared Gateway. The Gateway's published address is often an
in-cluster Service DNS (e.g. Istio), which is not usable off-cluster.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2a8b4c7e9f1"
down_revision: str | Sequence[str] | None = "c1f7a2b9d4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("clusters", sa.Column("gateway_public_url", sa.String(length=512), nullable=True))
    op.add_column("clusters", sa.Column("gateway_port", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("clusters", "gateway_port")
    op.drop_column("clusters", "gateway_public_url")
