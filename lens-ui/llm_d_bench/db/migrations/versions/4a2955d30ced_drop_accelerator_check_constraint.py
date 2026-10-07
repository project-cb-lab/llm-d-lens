"""drop accelerator check constraint

Revision ID: 4a2955d30ced
Revises: e5f6a7b8c9d0
Create Date: 2026-09-29 06:52:11.983587

Drops ``ck_monitoring_accelerator_operations_accelerator_intel_gpu``, which
pinned the column to ``'intel_gpu'`` and rejected every other hardware profile.
``AcceleratorType`` is intentionally open and validated against the registered
hardware profiles at the DTO boundary, so the database must not enumerate the
value (otherwise each new accelerator needs its own migration).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a2955d30ced"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_monitoring_accelerator_operations_accelerator_intel_gpu"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(_CONSTRAINT, "monitoring_accelerator_operations", type_="check")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_check_constraint(
        _CONSTRAINT,
        "monitoring_accelerator_operations",
        "accelerator = 'intel_gpu'",
    )
