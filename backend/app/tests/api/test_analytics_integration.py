"""Integration tests for analytics API endpoints.

Authorization follows the Sources tab pattern:
- Org-level: enforce_any_source_admin (Super Admin OR asset_admin on any codebase)
- Codebase-level: enforce_asset_action with asset.manage (asset_admin role)

NOTE: These tests are skipped because they require role->action seed data that is
normally populated via database migrations. The test fixture doesn't run migrations.
TODO: Either run migrations in test setup or add seed data fixture.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="Requires role->action seed data from migrations (RoleActionAllowAsset)"
)

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from database.models import OrgMembership
from database.models_enums import OrgRole, PrimaryAssetRole, PrincipalKind
from sqlmodel import Session, select

if TYPE_CHECKING:
    from app.auth.models import User

from app.schemas.analytics_schema import (
    ActivityEntry,
    ActivityResponse,
    AnalyticsOverview,
    AnalyticsStatus,
    BranchesResponse,
    BranchMetrics,
    CodebaseListItem,
    CodebasesListResponse,
    DirectoryOwnership,
    OrgAnalyticsSummary,
    OwnershipResponse,
)
from app.test_factories import (
    Auth0UserFactory,
    PrimaryAssetFactory,
    PrimaryAssetRoleGrantFactory,
)


def create_mock_user(organization_id: str, user_id: str = "test-user-id") -> User:
    """Create a mock User object for testing."""
    from app.auth.models import User

    return User(
        org_id=organization_id,
        org_name="Test Organization",
        sub=user_id,
        iss="https://test.auth0.com/",
        aud=["test-audience"],
        iat=1234567890,
        exp=9999999999,
        scope="",
        azp="",
        permissions=[],
        user_email="test@example.com",
        user_full_name="Test User",
    )


@pytest.mark.integration
class TestAnalyticsOrgLevelAuthorization:
    """Tests for organization-level endpoint authorization."""

    @pytest.fixture
    def mock_analytics_service(self):
        """Mock the AnalyticsService for integration tests."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_org_summary.return_value = OrgAnalyticsSummary(
                organization_id="test-org",
                total_codebases=1,
                codebases_with_analytics=1,
                generated_at=datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC),
            )
            mock_instance.get_codebases_list.return_value = CodebasesListResponse(
                organization_id="test-org",
                codebases=[],
                generated_at=datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC),
            )
            mock.return_value = mock_instance
            yield mock

    @pytest.mark.integration
    def test_super_admin_can_access_summary(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test that super admins can access org summary."""
        org_id = "test-org-id"

        # Create a super admin user
        super_admin = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )

        # Update org membership to make them super admin
        org_membership = integration_db_session.exec(
            select(OrgMembership).where(
                OrgMembership.user_id == super_admin.id,
                OrgMembership.org_id == org_id,
            )
        ).first()
        if org_membership:
            org_membership.role = OrgRole.org_super_admin
            integration_db_session.add(org_membership)
            integration_db_session.commit()

        # Create mock JWT user
        mock_user = create_mock_user(org_id, super_admin.id)

        from app.api.routes.v1.analytics import get_analytics_summary

        # Call the endpoint directly - no patches needed
        result = get_analytics_summary(session=integration_db_session, user=mock_user)

        # Super admin should be able to access
        assert result.total_codebases == 1

    @pytest.mark.integration
    def test_source_admin_can_access_summary(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test that source admins (asset_admin on any codebase) can access org summary."""
        org_id = "test-org-id"

        # Create user with org_member role
        source_admin = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )

        # Create a codebase and grant asset_admin role
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=source_admin.id,
            role=PrimaryAssetRole.asset_admin,
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, source_admin.id)

        from app.api.routes.v1.analytics import get_analytics_summary

        result = get_analytics_summary(session=integration_db_session, user=mock_user)

        # Source admin should be able to access
        assert result.total_codebases == 1

    @pytest.mark.integration
    def test_regular_member_cannot_access_summary(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test that org members without admin roles cannot access org summary."""
        from fastapi import HTTPException

        org_id = "test-org-id"

        # Create user with only org_member role (no asset_admin grants)
        regular_member = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )

        mock_user = create_mock_user(org_id, regular_member.id)

        from app.api.routes.v1.analytics import get_analytics_summary

        # Regular member should get 403
        with pytest.raises(HTTPException) as exc_info:
            get_analytics_summary(session=integration_db_session, user=mock_user)

        assert exc_info.value.status_code == 403


@pytest.mark.integration
class TestAnalyticsCodebaseLevelAuthorization:
    """Tests for codebase-level endpoint authorization."""

    @pytest.fixture
    def mock_analytics_service(self):
        """Mock the AnalyticsService for integration tests."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_overview.return_value = AnalyticsOverview(
                codebase_id="test-id",
                display_name="test-repo",
                repository_name="test-repo",
                full_name="owner/test-repo",
                owner="owner",
                total_commits=100,
                total_contributors=5,
                total_branches=3,
                net_lines=5000,
                additions_lines=8000,
                deletions_lines=3000,
                churn_lines=11000,
                churn_sloc=5000,
                net_sloc=2500,
                current_sloc=2500,
                additions_sloc=8000,
                deletions_sloc=3000,
                avg_bytes_per_line=50.0,
                total_files=42,
                default_branch="main",
                primary_language="Python",
                first_commit_date=datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
                last_commit_date=datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC),
                collected_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC),
                last_updated_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC),
            )
            mock.return_value = mock_instance
            yield mock

    @pytest.mark.integration
    def test_asset_admin_can_view_codebase_analytics(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test that asset_admin can view analytics for their codebase."""
        org_id = "test-org-id"

        # Create user
        asset_admin = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )

        # Create codebase with asset_admin grant
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=asset_admin.id,
            role=PrimaryAssetRole.asset_admin,
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, asset_admin.id)

        from app.api.routes.v1.analytics import get_codebase_overview

        result = get_codebase_overview(
            session=integration_db_session, user=mock_user, codebase_id=codebase.id
        )

        # Asset admin should be able to view analytics
        assert result.total_commits == 100

    @pytest.mark.integration
    def test_asset_member_cannot_view_codebase_analytics(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test that asset_member cannot view analytics (asset.manage required)."""
        from fastapi import HTTPException

        org_id = "test-org-id"

        # Create user
        asset_member = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )

        # Create codebase with asset_member grant (not asset_admin)
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=asset_member.id,
            role=PrimaryAssetRole.asset_member,  # Not admin!
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, asset_member.id)

        from app.api.routes.v1.analytics import get_codebase_overview

        # Asset member should get 403 (needs asset.manage permission)
        with pytest.raises(HTTPException) as exc_info:
            get_codebase_overview(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

        assert exc_info.value.status_code == 403

    @pytest.mark.integration
    def test_user_without_codebase_access_denied(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test that users without any role on codebase are denied."""
        from fastapi import HTTPException

        org_id = "test-org-id"

        # Create user with no grants
        user_without_access = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )

        # Create codebase (no grant for this user)
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )

        mock_user = create_mock_user(org_id, user_without_access.id)

        from app.api.routes.v1.analytics import get_codebase_overview

        # User without access should get 403
        with pytest.raises(HTTPException) as exc_info:
            get_codebase_overview(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

        assert exc_info.value.status_code == 403


@pytest.mark.integration
class TestAnalytics404Handling:
    """Tests for 404 handling when analytics data is not found."""

    @pytest.fixture
    def mock_analytics_service_not_found(self):
        """Mock the AnalyticsService to return None (not found)."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_overview.return_value = None
            mock_instance.get_branches.return_value = None
            mock_instance.get_activity.return_value = None
            mock_instance.get_ownership.return_value = None
            mock.return_value = mock_instance
            yield mock

    @pytest.mark.integration
    def test_codebase_overview_returns_404_when_not_found(
        self,
        integration_db_session: Session,
        mock_analytics_service_not_found,
    ):
        """Test that codebase overview returns 404 when analytics not found."""
        from fastapi import HTTPException

        org_id = "test-org-id"

        # Create super admin (can access any codebase)
        super_admin = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )
        org_membership = integration_db_session.exec(
            select(OrgMembership).where(
                OrgMembership.user_id == super_admin.id,
                OrgMembership.org_id == org_id,
            )
        ).first()
        if org_membership:
            org_membership.role = OrgRole.org_super_admin
            integration_db_session.add(org_membership)
            integration_db_session.commit()

        # Create codebase
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )

        mock_user = create_mock_user(org_id, super_admin.id)

        from app.api.routes.v1.analytics import get_codebase_overview

        # Should return 404 when analytics not found
        with pytest.raises(HTTPException) as exc_info:
            get_codebase_overview(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

        assert exc_info.value.status_code == 404
        assert "Analytics not found" in exc_info.value.detail


@pytest.mark.integration
class TestAnalyticsBranchesAuthorization:
    """Tests for /codebases/{id}/analytics/branches authorization."""

    @pytest.fixture
    def mock_analytics_service(self):
        """Mock the AnalyticsService for integration tests."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_branches.return_value = BranchesResponse(
                codebase_id="test-id",
                branches=[
                    BranchMetrics(
                        name="main",
                        is_default=True,
                        commits=80,
                        last_commit_date=datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC),
                        last_analyzed_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC),
                        status="active",
                    )
                ],
            )
            mock.return_value = mock_instance
            yield mock

    @pytest.mark.integration
    def test_asset_admin_can_view_branches(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test asset_admin can access branches endpoint."""
        org_id = "test-org-id"

        asset_admin = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=asset_admin.id,
            role=PrimaryAssetRole.asset_admin,
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, asset_admin.id)

        from app.api.routes.v1.analytics import get_codebase_branches

        result = get_codebase_branches(
            session=integration_db_session, user=mock_user, codebase_id=codebase.id
        )

        assert len(result.branches) == 1
        assert result.branches[0].name == "main"

    @pytest.mark.integration
    def test_asset_member_cannot_view_branches(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test asset_member gets 403 on branches endpoint."""
        from fastapi import HTTPException

        org_id = "test-org-id"

        asset_member = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=asset_member.id,
            role=PrimaryAssetRole.asset_member,
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, asset_member.id)

        from app.api.routes.v1.analytics import get_codebase_branches

        with pytest.raises(HTTPException) as exc_info:
            get_codebase_branches(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

        assert exc_info.value.status_code == 403


@pytest.mark.integration
class TestAnalyticsActivityAuthorization:
    """Tests for /codebases/{id}/analytics/activity authorization."""

    @pytest.fixture
    def mock_analytics_service(self):
        """Mock the AnalyticsService for integration tests."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_activity.return_value = ActivityResponse(
                codebase_id="test-id",
                daily_activity=[
                    ActivityEntry(
                        date="2024-01-15",
                        commits=5,
                        additions=100,
                        deletions=20,
                    )
                ],
            )
            mock.return_value = mock_instance
            yield mock

    @pytest.mark.integration
    def test_asset_admin_can_view_activity(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test asset_admin can access activity endpoint."""
        org_id = "test-org-id"

        asset_admin = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=asset_admin.id,
            role=PrimaryAssetRole.asset_admin,
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, asset_admin.id)

        from app.api.routes.v1.analytics import get_codebase_activity

        result = get_codebase_activity(
            session=integration_db_session, user=mock_user, codebase_id=codebase.id
        )

        assert len(result.daily_activity) == 1

    @pytest.mark.integration
    def test_asset_member_cannot_view_activity(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test asset_member gets 403 on activity endpoint."""
        from fastapi import HTTPException

        org_id = "test-org-id"

        asset_member = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=asset_member.id,
            role=PrimaryAssetRole.asset_member,
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, asset_member.id)

        from app.api.routes.v1.analytics import get_codebase_activity

        with pytest.raises(HTTPException) as exc_info:
            get_codebase_activity(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

        assert exc_info.value.status_code == 403


@pytest.mark.integration
class TestAnalyticsOwnershipAuthorization:
    """Tests for /codebases/{id}/analytics/ownership authorization."""

    @pytest.fixture
    def mock_analytics_service(self):
        """Mock the AnalyticsService for integration tests."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_ownership.return_value = OwnershipResponse(
                codebase_id="test-id",
                directories=[
                    DirectoryOwnership(
                        directory_path="src",
                        total_commits=50,
                        churn_sloc=3000,
                        unique_contributors=3,
                        primary_owner_email="dev@example.com",
                        primary_owner_name="Developer",
                        primary_owner_percentage=60.0,
                        contributors=[],
                    )
                ],
            )
            mock.return_value = mock_instance
            yield mock

    @pytest.mark.integration
    def test_asset_admin_can_view_ownership(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test asset_admin can access ownership endpoint."""
        org_id = "test-org-id"

        asset_admin = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=asset_admin.id,
            role=PrimaryAssetRole.asset_admin,
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, asset_admin.id)

        from app.api.routes.v1.analytics import get_codebase_ownership

        result = get_codebase_ownership(
            session=integration_db_session, user=mock_user, codebase_id=codebase.id
        )

        assert len(result.directories) == 1
        assert result.directories[0].directory_path == "src"

    @pytest.mark.integration
    def test_asset_member_cannot_view_ownership(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test asset_member gets 403 on ownership endpoint."""
        from fastapi import HTTPException

        org_id = "test-org-id"

        asset_member = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=asset_member.id,
            role=PrimaryAssetRole.asset_member,
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, asset_member.id)

        from app.api.routes.v1.analytics import get_codebase_ownership

        with pytest.raises(HTTPException) as exc_info:
            get_codebase_ownership(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

        assert exc_info.value.status_code == 403


@pytest.mark.integration
class TestAnalyticsStatusAuthorization:
    """Tests for /codebases/{id}/analytics/status authorization."""

    @pytest.fixture
    def mock_analytics_service(self):
        """Mock the AnalyticsService for integration tests."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_status.return_value = AnalyticsStatus(
                codebase_id="test-id",
                generated_at=datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC),
                status="complete",
                generation_seconds=1.5,
            )
            mock.return_value = mock_instance
            yield mock

    @pytest.mark.integration
    def test_asset_admin_can_view_status(
        self,
        integration_db_session: Session,
        mock_analytics_service,
    ):
        """Test asset_admin can access status endpoint."""
        org_id = "test-org-id"

        asset_admin = Auth0UserFactory.create(
            integration_db_session, organization_id=org_id
        )
        codebase = PrimaryAssetFactory.create(
            integration_db_session, organization_id=org_id
        )
        PrimaryAssetRoleGrantFactory.create(
            integration_db_session,
            primary_asset_id=codebase.id,
            principal_kind=PrincipalKind.user,
            user_id=asset_admin.id,
            role=PrimaryAssetRole.asset_admin,
            organization_id=org_id,
        )

        mock_user = create_mock_user(org_id, asset_admin.id)

        from app.api.routes.v1.analytics import get_analytics_status

        result = get_analytics_status(
            session=integration_db_session, user=mock_user, codebase_id=codebase.id
        )

        assert result.status == "complete"

    @pytest.mark.integration
    def test_status_returns_none_when_no_analytics(
        self,
        integration_db_session: Session,
    ):
        """Test status endpoint returns 'none' status when no analytics exist."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_status.return_value = AnalyticsStatus(
                codebase_id="test-id",
                status="none",
                generated_at=None,
                generation_seconds=None,
            )
            mock.return_value = mock_instance

            org_id = "test-org-id"

            asset_admin = Auth0UserFactory.create(
                integration_db_session, organization_id=org_id
            )
            codebase = PrimaryAssetFactory.create(
                integration_db_session, organization_id=org_id
            )
            PrimaryAssetRoleGrantFactory.create(
                integration_db_session,
                primary_asset_id=codebase.id,
                principal_kind=PrincipalKind.user,
                user_id=asset_admin.id,
                role=PrimaryAssetRole.asset_admin,
                organization_id=org_id,
            )

            mock_user = create_mock_user(org_id, asset_admin.id)

            from app.api.routes.v1.analytics import get_analytics_status

            result = get_analytics_status(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

            # Should return 'none' status, not 404
            assert result.status == "none"
            assert result.generated_at is None


@pytest.mark.integration
class TestResponseSchemaValidation:
    """Tests that verify response schemas match Pydantic models."""

    @pytest.mark.integration
    def test_org_summary_response_matches_schema(
        self,
        integration_db_session: Session,
    ):
        """Verify OrgAnalyticsSummary response matches schema."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_org_summary.return_value = OrgAnalyticsSummary(
                organization_id="test-org",
                total_codebases=5,
                codebases_with_analytics=3,
                generated_at=datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC),
            )
            mock.return_value = mock_instance

            org_id = "test-org-id"
            super_admin = Auth0UserFactory.create(
                integration_db_session, organization_id=org_id
            )
            org_membership = integration_db_session.exec(
                select(OrgMembership).where(
                    OrgMembership.user_id == super_admin.id,
                    OrgMembership.org_id == org_id,
                )
            ).first()
            if org_membership:
                org_membership.role = OrgRole.org_super_admin
                integration_db_session.add(org_membership)
                integration_db_session.commit()

            mock_user = create_mock_user(org_id, super_admin.id)

            from app.api.routes.v1.analytics import get_analytics_summary

            result = get_analytics_summary(
                session=integration_db_session, user=mock_user
            )

            # Verify result is the correct Pydantic model
            assert isinstance(result, OrgAnalyticsSummary)
            assert result.organization_id == "test-org"
            assert result.total_codebases == 5

    @pytest.mark.integration
    def test_codebases_list_response_matches_schema(
        self,
        integration_db_session: Session,
    ):
        """Verify CodebasesListResponse response matches schema."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_codebases_list.return_value = CodebasesListResponse(
                organization_id="test-org",
                codebases=[
                    CodebaseListItem(
                        codebase_id="id-1",
                        display_name="repo-1",
                        total_commits=100,
                        current_sloc=5000,
                        last_commit_date=datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC),
                        analytics_status="complete",
                    )
                ],
                generated_at=datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC),
            )
            mock.return_value = mock_instance

            org_id = "test-org-id"
            super_admin = Auth0UserFactory.create(
                integration_db_session, organization_id=org_id
            )
            org_membership = integration_db_session.exec(
                select(OrgMembership).where(
                    OrgMembership.user_id == super_admin.id,
                    OrgMembership.org_id == org_id,
                )
            ).first()
            if org_membership:
                org_membership.role = OrgRole.org_super_admin
                integration_db_session.add(org_membership)
                integration_db_session.commit()

            mock_user = create_mock_user(org_id, super_admin.id)

            from app.api.routes.v1.analytics import get_codebases_list

            result = get_codebases_list(session=integration_db_session, user=mock_user)

            # Verify result is the correct Pydantic model
            assert isinstance(result, CodebasesListResponse)
            assert result.organization_id == "test-org"
            assert len(result.codebases) == 1

    @pytest.mark.integration
    def test_overview_response_has_all_required_fields(
        self,
        integration_db_session: Session,
    ):
        """Verify AnalyticsOverview response has all required fields."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_overview.return_value = AnalyticsOverview(
                codebase_id="test-id",
                display_name="test-repo",
                repository_name="test-repo",
                full_name="owner/test-repo",
                owner="owner",
                total_commits=100,
                total_contributors=5,
                total_branches=3,
                current_sloc=2500,
                avg_bytes_per_line=50.0,
                total_files=42,
                default_branch="main",
                primary_language="Python",
                first_commit_date=datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
                last_commit_date=datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC),
                collected_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC),
                last_updated_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC),
            )
            mock.return_value = mock_instance

            org_id = "test-org-id"
            asset_admin = Auth0UserFactory.create(
                integration_db_session, organization_id=org_id
            )
            codebase = PrimaryAssetFactory.create(
                integration_db_session, organization_id=org_id
            )
            PrimaryAssetRoleGrantFactory.create(
                integration_db_session,
                primary_asset_id=codebase.id,
                principal_kind=PrincipalKind.user,
                user_id=asset_admin.id,
                role=PrimaryAssetRole.asset_admin,
                organization_id=org_id,
            )

            mock_user = create_mock_user(org_id, asset_admin.id)

            from app.api.routes.v1.analytics import get_codebase_overview

            result = get_codebase_overview(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

            # Verify result is the correct Pydantic model
            assert isinstance(result, AnalyticsOverview)
            assert result.codebase_id == "test-id"
            assert result.total_commits == 100

    @pytest.mark.integration
    def test_branches_response_matches_schema(
        self,
        integration_db_session: Session,
    ):
        """Verify BranchesResponse response has all required fields."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_branches.return_value = BranchesResponse(
                codebase_id="test-id",
                branches=[
                    BranchMetrics(
                        name="main",
                        is_default=True,
                        commits=80,
                        last_commit_date=datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC),
                        last_analyzed_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC),
                        status="active",
                    )
                ],
            )
            mock.return_value = mock_instance

            org_id = "test-org-id"
            asset_admin = Auth0UserFactory.create(
                integration_db_session, organization_id=org_id
            )
            codebase = PrimaryAssetFactory.create(
                integration_db_session, organization_id=org_id
            )
            PrimaryAssetRoleGrantFactory.create(
                integration_db_session,
                primary_asset_id=codebase.id,
                principal_kind=PrincipalKind.user,
                user_id=asset_admin.id,
                role=PrimaryAssetRole.asset_admin,
                organization_id=org_id,
            )

            mock_user = create_mock_user(org_id, asset_admin.id)

            from app.api.routes.v1.analytics import get_codebase_branches

            result = get_codebase_branches(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

            # Verify result is the correct Pydantic model
            assert isinstance(result, BranchesResponse)
            assert len(result.branches) == 1
            assert result.branches[0].name == "main"

    @pytest.mark.integration
    def test_activity_response_matches_schema(
        self,
        integration_db_session: Session,
    ):
        """Verify ActivityResponse response has all required fields."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_activity.return_value = ActivityResponse(
                codebase_id="test-id",
                daily_activity=[
                    ActivityEntry(
                        date="2024-01-15",
                        commits=5,
                        additions=100,
                        deletions=20,
                    )
                ],
            )
            mock.return_value = mock_instance

            org_id = "test-org-id"
            asset_admin = Auth0UserFactory.create(
                integration_db_session, organization_id=org_id
            )
            codebase = PrimaryAssetFactory.create(
                integration_db_session, organization_id=org_id
            )
            PrimaryAssetRoleGrantFactory.create(
                integration_db_session,
                primary_asset_id=codebase.id,
                principal_kind=PrincipalKind.user,
                user_id=asset_admin.id,
                role=PrimaryAssetRole.asset_admin,
                organization_id=org_id,
            )

            mock_user = create_mock_user(org_id, asset_admin.id)

            from app.api.routes.v1.analytics import get_codebase_activity

            result = get_codebase_activity(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

            # Verify result is the correct Pydantic model
            assert isinstance(result, ActivityResponse)
            assert len(result.daily_activity) == 1

    @pytest.mark.integration
    def test_ownership_response_matches_schema(
        self,
        integration_db_session: Session,
    ):
        """Verify OwnershipResponse response has all required fields."""
        with patch("app.api.routes.v1.analytics.AnalyticsService") as mock:
            mock_instance = MagicMock()
            mock_instance.get_ownership.return_value = OwnershipResponse(
                codebase_id="test-id",
                directories=[
                    DirectoryOwnership(
                        directory_path="src",
                        total_commits=50,
                        churn_sloc=3000,
                        unique_contributors=3,
                        primary_owner_email="dev@example.com",
                        primary_owner_name="Developer",
                        primary_owner_percentage=60.0,
                        contributors=[],
                    )
                ],
            )
            mock.return_value = mock_instance

            org_id = "test-org-id"
            asset_admin = Auth0UserFactory.create(
                integration_db_session, organization_id=org_id
            )
            codebase = PrimaryAssetFactory.create(
                integration_db_session, organization_id=org_id
            )
            PrimaryAssetRoleGrantFactory.create(
                integration_db_session,
                primary_asset_id=codebase.id,
                principal_kind=PrincipalKind.user,
                user_id=asset_admin.id,
                role=PrimaryAssetRole.asset_admin,
                organization_id=org_id,
            )

            mock_user = create_mock_user(org_id, asset_admin.id)

            from app.api.routes.v1.analytics import get_codebase_ownership

            result = get_codebase_ownership(
                session=integration_db_session,
                user=mock_user,
                codebase_id=codebase.id,
            )

            # Verify result is the correct Pydantic model
            assert isinstance(result, OwnershipResponse)
            assert len(result.directories) == 1
            assert result.directories[0].directory_path == "src"
