"""add gateway_authz_host to clusters

Revision ID: a1f2b3c4d5e6
Revises: f4c0d6e2a8b3
Create Date: 2026-09-26 03:30:00.000000

Adds ``clusters.gateway_authz_host``: the host/IP a cluster's pods can reach the
Lens model-gateway ext_authz endpoint on, chosen per cluster in the create/edit
form. Used to render the Gateway's ext_authz identity injection.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f2b3c4d5e6"
down_revision: str | Sequence[str] | None = "f4c0d6e2a8b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("clusters", sa.Column("gateway_authz_host", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("clusters", "gateway_authz_host")
