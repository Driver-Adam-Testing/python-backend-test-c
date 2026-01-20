"""add new onboarding checklist fields

Revision ID: a7c3e9f2b148
Revises: de2caf98da88
Create Date: 2026-01-14 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a7c3e9f2b148"
down_revision = "de2caf98da88"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "onboarding_checklist",
        sa.Column(
            "configured_rbac_completed_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column("teams_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column(
            "scim_provisioning_completed_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column("sso_sync_completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("onboarding_checklist", "sso_sync_completed_at")
    op.drop_column("onboarding_checklist", "scim_provisioning_completed_at")
    op.drop_column("onboarding_checklist", "teams_completed_at")
    op.drop_column("onboarding_checklist", "configured_rbac_completed_at")
