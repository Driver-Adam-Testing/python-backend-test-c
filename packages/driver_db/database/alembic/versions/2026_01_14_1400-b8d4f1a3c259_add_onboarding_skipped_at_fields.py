"""add onboarding checklist skipped_at fields

Revision ID: b8d4f1a3c259
Revises: a7c3e9f2b148
Create Date: 2026-01-14 14:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b8d4f1a3c259"
down_revision = "a7c3e9f2b148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "onboarding_checklist",
        sa.Column(
            "connect_codebase_skipped_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column(
            "generate_codebase_skipped_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column("setup_mcp_skipped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column(
            "enable_export_skipped_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column(
            "generate_autodoc_skipped_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column(
            "invite_teammate_skipped_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column(
            "configured_rbac_skipped_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column("teams_skipped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column(
            "scim_provisioning_skipped_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "onboarding_checklist",
        sa.Column("sso_sync_skipped_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("onboarding_checklist", "sso_sync_skipped_at")
    op.drop_column("onboarding_checklist", "scim_provisioning_skipped_at")
    op.drop_column("onboarding_checklist", "teams_skipped_at")
    op.drop_column("onboarding_checklist", "configured_rbac_skipped_at")
    op.drop_column("onboarding_checklist", "invite_teammate_skipped_at")
    op.drop_column("onboarding_checklist", "generate_autodoc_skipped_at")
    op.drop_column("onboarding_checklist", "enable_export_skipped_at")
    op.drop_column("onboarding_checklist", "setup_mcp_skipped_at")
    op.drop_column("onboarding_checklist", "generate_codebase_skipped_at")
    op.drop_column("onboarding_checklist", "connect_codebase_skipped_at")
