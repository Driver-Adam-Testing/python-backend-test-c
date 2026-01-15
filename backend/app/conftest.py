from collections.abc import Generator
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine
from testcontainers.postgres import PostgresContainer

from app.api.auth import UserToken

# ============================================================================
# Integration Test Database Fixtures
# ============================================================================
# These fixtures use Testcontainers to provision isolated PostgreSQL containers
# They are ONLY used when tests are marked with @pytest.mark.integration
# ============================================================================


@pytest.fixture(scope="function")
def integration_db_engine() -> Generator[Engine, None, None]:
    """
    Create a PostgreSQL engine using Testcontainers.

    Benefits of Testcontainers:
    - Each test gets a fresh PostgreSQL container
    - Completely isolated - no schema conflicts
    - Automatically cleaned up after test
    - Production-like database (not mocks)

    Note: We create tables manually instead of using Alembic migrations because:
    - The Alembic migration history has multiple heads that need to be resolved
    - This is faster for tests (only creates needed tables)
    - TODO: Consider using Alembic once migration heads are merged
    """
    from database.models import (
        Action,
        GitProviderApp,
        GitProviderAppInstallation,
        Organization,
        OrgMembership,
        PrimaryAsset,
        PrimaryAssetRoleGrant,
        RoleActionAllowAsset,
        RoleActionAllowOrg,
        RoleActionAllowTeam,
        Team,
        TeamMembership,
        User,
        Version,
    )

    # Start PostgreSQL container
    postgres = PostgresContainer("postgres:16-alpine")
    postgres.start()

    # Get connection details from container
    db_url = postgres.get_connection_url().replace("psycopg2", "psycopg2")

    # Create engine
    engine = create_engine(db_url, echo=False)  # Set to True for SQL debugging

    # Create only the tables needed for integration tests
    # Note: Action must come before RoleActionAllow* tables due to FK constraints
    tables_to_create = [
        Organization.__table__,
        User.__table__,
        OrgMembership.__table__,
        Team.__table__,
        TeamMembership.__table__,
        GitProviderApp.__table__,
        GitProviderAppInstallation.__table__,
        PrimaryAsset.__table__,
        Version.__table__,
        PrimaryAssetRoleGrant.__table__,
        Action.__table__,
        RoleActionAllowAsset.__table__,
        RoleActionAllowOrg.__table__,
        RoleActionAllowTeam.__table__,
    ]
    SQLModel.metadata.create_all(engine, tables=tables_to_create)

    # Create test organizations
    with Session(engine) as session:
        test_org = Organization(
            id="test-org-id",
            name="Test Organization",
            display_name="Test Organization",
            org_metadata={},
        )

        # Also create org-1-id and org-2-id for organization isolation tests
        org1 = Organization(
            id="org-1-id",
            name="Organization 1",
            display_name="Organization 1",
            org_metadata={},
        )

        org2 = Organization(
            id="org-2-id",
            name="Organization 2",
            display_name="Organization 2",
            org_metadata={},
        )

        session.add(test_org)
        session.add(org1)
        session.add(org2)
        session.commit()

    yield engine

    # Cleanup: Stop and remove container
    engine.dispose()
    postgres.stop()


@pytest.fixture(scope="function")
def integration_db_session(
    integration_db_engine: Engine,
) -> Generator[Session, None, None]:
    """
    Create a database session for integration tests.

    Each test gets a fresh session with a clean database state.
    """
    with Session(integration_db_engine) as session:
        yield session
        # Rollback any uncommitted changes
        session.rollback()


# ============================================================================
# Unit Test Fixtures (Existing - Mocked)
# ============================================================================


@pytest.fixture(scope="function")
def current_user_with_org() -> UserToken:
    current_user = Mock(spec=UserToken)
    current_user.user_id = "test_user_id"
    current_user.organization_id = "test_org_id"
    current_user.organization_name = "test_org_name"
    current_user.is_service_account = False
    return current_user


@pytest.fixture(scope="function")
def current_user_with_other_org() -> UserToken:
    current_user = Mock(spec=UserToken)
    current_user.user_id = "other_test_user_id"
    current_user.organization_id = "other_test_org_id"
    current_user.organization_name = "other_test_org_name"
    current_user.is_service_account = False
    return current_user
