"""add inotify_max_user_instances to clusters

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-26 18:40:00.000000

Adds ``clusters.inotify_max_user_instances``: the per-uid inotify instance limit
Lens applies to every node (``fs.inotify.max_user_instances``). Defaults to 8192
because kind's 128 is exhausted by a node running many pods.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "clusters",
        sa.Column(
            "inotify_max_user_instances",
            sa.Integer(),
            nullable=False,
            server_default="8192",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("clusters", "inotify_max_user_instances")
