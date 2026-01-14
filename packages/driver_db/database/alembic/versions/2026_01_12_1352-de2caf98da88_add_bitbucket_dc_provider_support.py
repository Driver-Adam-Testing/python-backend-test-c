"""add_bitbucket_dc_provider_support

Revision ID: de2caf98da88
Revises: e2c37920d33c
Create Date: 2026-01-12 13:52:59.227781

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "de2caf98da88"
down_revision = "e2c37920d33c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE primaryassetprovider ADD VALUE 'BITBUCKET_DATA_CENTER'")


def downgrade() -> None:
    pass
