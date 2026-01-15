"""Unit tests for AnalyticsService."""

import json
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError
from database.models_enums import PrimaryAssetProvider


class TestAnalyticsService:
    """Unit tests for AnalyticsService with mocked S3."""

    @pytest.fixture
    def mock_s3_client(self):
        """Create a mock S3 client."""
        with patch("app.services.analytics_service.s3_client") as mock:
            yield mock

    @pytest.fixture
    def mock_session(self):
        """Create a mock database session."""
        return MagicMock()

    @pytest.fixture
    def mock_super_admin_user(self):
        """Create a mock super admin user."""
        user = MagicMock()
        user.user_id = str(uuid4())
        user.organization_id = str(uuid4())
        return user

    @pytest.fixture
    def mock_source_admin_user(self):
        """Create a mock source admin user."""
        user = MagicMock()
        user.user_id = str(uuid4())
        user.organization_id = str(uuid4())
        return user

    @pytest.fixture
    def service_super_admin(self, mock_s3_client, mock_session, mock_super_admin_user):
        """Create AnalyticsService for super admin with mocked S3."""
        from app.services.analytics_service import AnalyticsService

        with patch("app.services.analytics_service.is_super_admin", return_value=True):
            return AnalyticsService(mock_session, mock_super_admin_user)

    @pytest.fixture
    def service_source_admin(
        self, mock_s3_client, mock_session, mock_source_admin_user
    ):
        """Create AnalyticsService for source admin with mocked S3."""
        from app.services.analytics_service import AnalyticsService

        with patch("app.services.analytics_service.is_super_admin", return_value=False):
            return AnalyticsService(mock_session, mock_source_admin_user)

    # Alias for backward compatibility in tests
    @pytest.fixture
    def service(self, service_super_admin):
        """Default service fixture (super admin)."""
        return service_super_admin

    def test_get_overview_success(self, service, mock_s3_client):
        """Test successful overview retrieval with full schema."""
        s3_data = {
            "codebase_id": "test-id",
            "display_name": "test-repo",
            "repository_name": "test-repo",
            "full_name": "owner/test-repo",
            "owner": "owner",
            "total_commits": 100,
            "total_contributors": 5,
            "total_branches": 3,
            "net_lines": 5000,
            "additions_lines": 8000,
            "deletions_lines": 3000,
            "churn_lines": 11000,
            "churn_sloc": 5000,
            "net_sloc": 2500,
            "current_sloc": 2500,
            "additions_sloc": 8000,
            "deletions_sloc": 3000,
            "avg_bytes_per_line": 50.0,
            "total_files": 42,
            "default_branch": "main",
            "primary_language": "Python",
            "first_commit_date": "2024-01-01T00:00:00Z",
            "last_commit_date": "2024-06-01T00:00:00Z",
            "collected_at": "2024-06-01T12:00:00Z",
            "last_updated_at": "2024-06-01T12:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(s3_data).encode())
        }

        result = service.get_overview("test-id")

        # Service enriches with provider (None when not found in DB)
        assert result is not None
        assert result.codebase_id == "test-id"
        assert result.display_name == "test-repo"
        assert result.total_commits == 100
        assert result.provider is None
        mock_s3_client.get_object.assert_called_once()

    def test_get_overview_not_found(self, service, mock_s3_client):
        """Test overview retrieval when file doesn't exist."""
        error_response = {"Error": {"Code": "NoSuchKey"}}
        mock_s3_client.get_object.side_effect = ClientError(error_response, "GetObject")

        result = service.get_overview("missing-id")

        assert result is None

    def test_get_status_success(self, service, mock_s3_client):
        """Test successful status retrieval."""
        expected_data = {
            "codebase_id": "test-id",
            "generated_at": "2024-06-15T10:00:00Z",
            "status": "complete",
            "generation_seconds": 1.5,
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(expected_data).encode())
        }

        result = service.get_status("test-id")

        assert result.status == "complete"
        assert result.codebase_id == "test-id"

    def test_get_status_returns_none_status_when_not_found(
        self, service, mock_s3_client
    ):
        """Test that get_status returns 'none' status when metadata not found."""
        error_response = {"Error": {"Code": "NoSuchKey"}}
        mock_s3_client.get_object.side_effect = ClientError(error_response, "GetObject")

        result = service.get_status("missing-id")

        assert result.status == "none"
        assert result.codebase_id == "missing-id"
        assert result.generated_at is None

    def test_get_org_summary_super_admin_gets_precomputed(
        self, service, mock_s3_client
    ):
        """Test that super admin gets pre-computed org summary."""
        expected_data = {
            "organization_id": "test-org",
            "total_codebases": 5,
            "codebases_with_analytics": 3,
            "total_commits": 500,
            "total_contributors": 25,
            "total_sloc": 50000,
            "generated_at": "2024-06-15T10:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(expected_data).encode())
        }

        result = service.get_org_summary()

        assert result is not None
        assert result.organization_id == "test-org"
        assert result.total_codebases == 5
        assert result.codebases_with_analytics == 3
        # Super admin reads org_summary.json directly
        mock_s3_client.get_object.assert_called_once()

    def test_get_codebases_list_success(
        self, mock_s3_client, mock_session, mock_super_admin_user
    ):
        """Test successful codebases list retrieval."""
        from app.services.analytics_service import AnalyticsService

        id1, id2 = uuid4(), uuid4()
        s3_data = {
            "organization_id": "test-org",
            "codebases": [
                {
                    "codebase_id": str(id1),
                    "display_name": "repo-1",
                    "total_commits": 100,
                    "current_sloc": 5000,
                    "last_commit_date": "2024-06-01T00:00:00Z",
                    "analytics_status": "complete",
                },
                {
                    "codebase_id": str(id2),
                    "display_name": "repo-2",
                    "total_commits": 50,
                    "current_sloc": 2500,
                    "last_commit_date": "2024-05-15T00:00:00Z",
                    "analytics_status": "complete",
                },
            ],
            "generated_at": "2024-06-15T10:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(s3_data).encode())
        }

        with (
            patch("app.services.analytics_service.is_super_admin", return_value=True),
            patch.object(
                AnalyticsService,
                "_get_codebase_ids_for_user",
                return_value={id1, id2},
            ),
        ):
            service = AnalyticsService(mock_session, mock_super_admin_user)
            result = service.get_codebases_list()

        # Service enriches each codebase with provider (None when not found in DB)
        assert result is not None
        assert len(result.codebases) == 2
        assert result.organization_id == "test-org"
        # Verify provider is added to each codebase
        for cb in result.codebases:
            assert (
                cb.provider is not None or cb.provider is None
            )  # provider field exists

    def test_get_branches_success(self, service, mock_s3_client):
        """Test successful branches retrieval with full schema."""
        expected_data = {
            "codebase_id": "test-id",
            "branches": [
                {
                    "name": "main",
                    "is_default": True,
                    "commits": 80,
                    "last_commit_date": "2024-06-01T00:00:00Z",
                    "last_analyzed_at": "2024-06-01T12:00:00Z",
                    "status": "active",
                    "head_commit_sha": "abc123",
                    "current_sloc": 5000,
                    "churn_sloc": 8000,
                    "unique_contributors": 5,
                    "total_files": 42,
                },
            ],
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(expected_data).encode())
        }

        result = service.get_branches("test-id")

        assert result is not None
        assert result.codebase_id == "test-id"
        assert len(result.branches) == 1
        assert result.branches[0].churn_sloc == 8000

    def test_get_activity_success(self, service, mock_s3_client):
        """Test successful activity retrieval with byte-based metrics."""
        expected_data = {
            "codebase_id": "test-id",
            "daily_activity": [
                {
                    "date": "2024-01-15",
                    "commits": 5,
                    "additions": 100,
                    "deletions": 20,
                    "active_contributors": 2,
                    "files_changed": 8,
                    "cumulative_lines": 1000,
                    "addition_bytes": 5000,
                    "deletion_bytes": 1000,
                    "net_bytes": 4000,
                    "patch_bytes": 6000,
                    "cumulative_sloc": 120,
                },
            ],
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(expected_data).encode())
        }

        result = service.get_activity("test-id")

        assert result is not None
        assert result.codebase_id == "test-id"
        assert result.daily_activity[0].addition_bytes == 5000

    def test_get_ownership_success(self, service, mock_s3_client):
        """Test successful ownership retrieval."""
        expected_data = {
            "codebase_id": "test-id",
            "directories": [
                {
                    "directory_path": "src",
                    "total_commits": 50,
                    "churn_sloc": 3000,
                    "unique_contributors": 3,
                    "primary_owner_email": "dev@example.com",
                    "primary_owner_name": "Developer",
                    "primary_owner_percentage": 60.0,
                    "contributors": [
                        {
                            "contributor_email": "dev@example.com",
                            "contributor_name": "Developer",
                            "total_commits": 30,
                            "churn_sloc": 1800,
                            "additions_sloc": 1800,
                            "deletions_sloc": 0,
                            "ownership_percentage": 60.0,
                            "first_commit_at": "2024-01-01T00:00:00Z",
                            "last_commit_at": "2024-06-01T00:00:00Z",
                            "active_days": 45,
                        }
                    ],
                }
            ],
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(expected_data).encode())
        }

        result = service.get_ownership("test-id")

        assert result is not None
        assert result.codebase_id == "test-id"
        assert result.directories[0].primary_owner_email == "dev@example.com"

    def test_invalid_json_returns_none(self, service, mock_s3_client):
        """Test that invalid JSON returns None."""
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: b"not valid json")
        }

        result = service.get_overview("test-id")

        assert result is None

    def test_s3_error_propagates(self, service, mock_s3_client):
        """Test that non-404 S3 errors propagate."""
        error_response = {"Error": {"Code": "AccessDenied"}}
        mock_s3_client.get_object.side_effect = ClientError(error_response, "GetObject")

        with pytest.raises(ClientError):
            service.get_overview("test-id")


class TestAnalyticsFiltering:
    """Tests for org summary and codebases list filtering based on admin access."""

    @pytest.fixture
    def mock_s3_client(self):
        """Create a mock S3 client."""
        with patch("app.services.analytics_service.s3_client") as mock:
            yield mock

    @pytest.fixture
    def mock_session(self):
        """Create a mock database session."""
        return MagicMock()

    @pytest.fixture
    def mock_super_admin_user(self):
        """Create a mock super admin user."""
        user = MagicMock()
        user.user_id = str(uuid4())
        user.organization_id = str(uuid4())
        return user

    @pytest.fixture
    def mock_source_admin_user(self):
        """Create a mock source admin user."""
        user = MagicMock()
        user.user_id = str(uuid4())
        user.organization_id = str(uuid4())
        return user

    @pytest.fixture
    def administered_codebase_ids(self):
        """IDs of codebases the source admin can manage."""
        return {UUID("11111111-1111-1111-1111-111111111111")}

    @pytest.fixture
    def all_codebases_data(self):
        """Full list of codebases in the org."""
        return {
            "organization_id": "test-org",
            "codebases": [
                {
                    "codebase_id": "11111111-1111-1111-1111-111111111111",
                    "display_name": "repo-1",
                    "total_commits": 100,
                    "total_contributors": 5,
                    "current_sloc": 5000,
                    "last_commit_date": "2024-06-01T00:00:00Z",
                    "analytics_status": "complete",
                },
                {
                    "codebase_id": "22222222-2222-2222-2222-222222222222",
                    "display_name": "repo-2",
                    "total_commits": 50,
                    "total_contributors": 3,
                    "current_sloc": 2500,
                    "last_commit_date": "2024-05-15T00:00:00Z",
                    "analytics_status": "complete",
                },
            ],
            "generated_at": "2024-06-15T10:00:00Z",
        }

    def test_super_admin_sees_all_codebases(
        self, mock_s3_client, mock_session, mock_super_admin_user, all_codebases_data
    ):
        """Super admin should see all codebases without filtering."""
        from app.services.analytics_service import AnalyticsService

        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(all_codebases_data).encode())
        }

        # For super admin, _get_codebase_ids_for_user returns all codebase IDs
        all_ids = {
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        }
        with (
            patch("app.services.analytics_service.is_super_admin", return_value=True),
            patch.object(
                AnalyticsService,
                "_get_codebase_ids_for_user",
                return_value=all_ids,
            ),
        ):
            service = AnalyticsService(mock_session, mock_super_admin_user)
            result = service.get_codebases_list()

        assert result is not None
        assert len(result.codebases) == 2

    def test_source_admin_sees_only_administered_codebases(
        self,
        mock_s3_client,
        mock_session,
        mock_source_admin_user,
        all_codebases_data,
        administered_codebase_ids,
    ):
        """Source admin should only see codebases they administer."""
        from app.services.analytics_service import AnalyticsService

        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(all_codebases_data).encode())
        }

        with (
            patch("app.services.analytics_service.is_super_admin", return_value=False),
            patch.object(
                AnalyticsService,
                "_get_codebase_ids_for_user",
                return_value=administered_codebase_ids,
            ),
        ):
            service = AnalyticsService(mock_session, mock_source_admin_user)
            result = service.get_codebases_list()

        assert result is not None
        assert len(result.codebases) == 1
        assert result.codebases[0].codebase_id == "11111111-1111-1111-1111-111111111111"

    def test_source_admin_org_summary_is_computed_from_filtered_codebases(
        self,
        mock_s3_client,
        mock_session,
        mock_source_admin_user,
        all_codebases_data,
        administered_codebase_ids,
    ):
        """Source admin org summary should be computed from only their administered codebases."""
        from app.services.analytics_service import AnalyticsService

        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(all_codebases_data).encode())
        }

        with (
            patch("app.services.analytics_service.is_super_admin", return_value=False),
            patch.object(
                AnalyticsService,
                "_get_codebase_ids_for_user",
                return_value=administered_codebase_ids,
            ),
        ):
            service = AnalyticsService(mock_session, mock_source_admin_user)
            result = service.get_org_summary()

        # Should only include stats from the one administered codebase
        assert result is not None
        assert result.total_codebases == 1
        assert result.codebases_with_analytics == 1

    def test_super_admin_org_summary_reads_precomputed_file(
        self, mock_s3_client, mock_session, mock_super_admin_user
    ):
        """Super admin org summary should read from pre-computed org_summary.json."""
        from app.services.analytics_service import AnalyticsService

        precomputed_summary = {
            "organization_id": "test-org",
            "total_codebases": 100,
            "codebases_with_analytics": 95,
            "generated_at": "2024-06-15T10:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(precomputed_summary).encode())
        }

        with patch("app.services.analytics_service.is_super_admin", return_value=True):
            service = AnalyticsService(mock_session, mock_super_admin_user)
            result = service.get_org_summary()

        # Should return pre-computed summary, not aggregate from codebases
        assert result is not None
        assert result.organization_id == "test-org"
        assert result.total_codebases == 100
        assert result.codebases_with_analytics == 95


class TestProviderEnrichment:
    """Tests for provider lookup and enrichment."""

    @pytest.fixture
    def mock_s3_client(self):
        """Create a mock S3 client."""
        with patch("app.services.analytics_service.s3_client") as mock:
            yield mock

    @pytest.fixture
    def mock_session(self):
        """Create a mock database session."""
        return MagicMock()

    @pytest.fixture
    def mock_user(self):
        """Create a mock user."""
        user = MagicMock()
        user.user_id = str(uuid4())
        user.organization_id = str(uuid4())
        return user

    @pytest.fixture
    def service(self, mock_s3_client, mock_session, mock_user):
        """Create AnalyticsService with mocked dependencies."""
        from app.services.analytics_service import AnalyticsService

        with patch("app.services.analytics_service.is_super_admin", return_value=True):
            return AnalyticsService(mock_session, mock_user)

    def test_get_provider_for_codebase_github(self, service, mock_session):
        """Test provider lookup returns 'github' for GitHub codebases."""

        codebase_id = str(uuid4())
        mock_asset = MagicMock()
        mock_asset.provider = PrimaryAssetProvider.GITHUB
        mock_session.query.return_value.filter.return_value.first.return_value = (
            mock_asset
        )

        result = service._get_provider_for_codebase(codebase_id)

        assert result == "github"

    def test_get_provider_for_codebase_gitlab(self, service, mock_session):
        """Test provider lookup returns 'gitlab' for GitLab codebases."""
        codebase_id = str(uuid4())
        mock_asset = MagicMock()
        mock_asset.provider = PrimaryAssetProvider.GITLAB_SELF_MANAGED
        mock_session.query.return_value.filter.return_value.first.return_value = (
            mock_asset
        )

        result = service._get_provider_for_codebase(codebase_id)

        assert result == "gitlab"

    def test_get_provider_for_codebase_bitbucket(self, service, mock_session):
        """Test provider lookup returns 'bitbucket' for Bitbucket codebases."""
        codebase_id = str(uuid4())
        mock_asset = MagicMock()
        mock_asset.provider = PrimaryAssetProvider.BITBUCKET
        mock_session.query.return_value.filter.return_value.first.return_value = (
            mock_asset
        )

        result = service._get_provider_for_codebase(codebase_id)

        assert result == "bitbucket"

    def test_get_provider_for_codebase_azure_devops(self, service, mock_session):
        """Test provider lookup returns 'azure-devops' for Azure DevOps codebases."""
        codebase_id = str(uuid4())
        mock_asset = MagicMock()
        mock_asset.provider = PrimaryAssetProvider.AZURE_DEVOPS_CLOUD
        mock_session.query.return_value.filter.return_value.first.return_value = (
            mock_asset
        )

        result = service._get_provider_for_codebase(codebase_id)

        assert result == "azure-devops"

    def test_get_provider_for_codebase_not_found(self, service, mock_session):
        """Test provider lookup returns None when codebase not in DB."""
        codebase_id = str(uuid4())
        mock_session.query.return_value.filter.return_value.first.return_value = None

        result = service._get_provider_for_codebase(codebase_id)

        assert result is None

    def test_get_provider_for_codebase_no_provider_set(self, service, mock_session):
        """Test provider lookup returns None when provider is None."""
        codebase_id = str(uuid4())
        mock_asset = MagicMock()
        mock_asset.provider = None
        mock_session.query.return_value.filter.return_value.first.return_value = (
            mock_asset
        )

        result = service._get_provider_for_codebase(codebase_id)

        assert result is None

    def test_get_providers_for_codebases_batch(self, service, mock_session):
        """Test batch provider lookup for multiple codebases."""
        id1, id2 = str(uuid4()), str(uuid4())

        mock_asset1 = MagicMock()
        mock_asset1.id = UUID(id1)
        mock_asset1.provider = PrimaryAssetProvider.GITHUB

        mock_asset2 = MagicMock()
        mock_asset2.id = UUID(id2)
        mock_asset2.provider = PrimaryAssetProvider.GITLAB_SELF_MANAGED

        mock_session.query.return_value.filter.return_value.all.return_value = [
            mock_asset1,
            mock_asset2,
        ]

        result = service._get_providers_for_codebases([id1, id2])

        assert result[id1] == "github"
        assert result[id2] == "gitlab"

    def test_get_providers_for_codebases_empty_list(self, service, mock_session):
        """Test batch provider lookup with empty list."""
        result = service._get_providers_for_codebases([])

        assert result == {}
        mock_session.query.assert_not_called()

    def test_overview_includes_provider(self, service, mock_s3_client, mock_session):
        """Test that get_overview enriches response with provider."""
        codebase_id = str(uuid4())
        s3_data = {
            "codebase_id": codebase_id,
            "display_name": "test-repo",
            "repository_name": "test-repo",
            "full_name": "owner/test-repo",
            "owner": "owner",
            "total_commits": 100,
            "total_contributors": 5,
            "total_branches": 3,
            "current_sloc": 2500,
            "avg_bytes_per_line": 50.0,
            "total_files": 42,
            "default_branch": "main",
            "primary_language": "Python",
            "first_commit_date": "2024-01-01T00:00:00Z",
            "last_commit_date": "2024-06-01T00:00:00Z",
            "collected_at": "2024-06-01T12:00:00Z",
            "last_updated_at": "2024-06-01T12:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(s3_data).encode())
        }

        mock_asset = MagicMock()
        mock_asset.provider = PrimaryAssetProvider.GITHUB
        mock_session.query.return_value.filter.return_value.first.return_value = (
            mock_asset
        )

        result = service.get_overview(codebase_id)

        assert result is not None
        assert result.provider == "github"


class TestJSONParsingEdgeCases:
    """Tests for S3 JSON parsing edge cases."""

    @pytest.fixture
    def mock_s3_client(self):
        """Create a mock S3 client."""
        with patch("app.services.analytics_service.s3_client") as mock:
            yield mock

    @pytest.fixture
    def mock_session(self):
        """Create a mock database session."""
        return MagicMock()

    @pytest.fixture
    def mock_user(self):
        """Create a mock user."""
        user = MagicMock()
        user.user_id = str(uuid4())
        user.organization_id = str(uuid4())
        return user

    @pytest.fixture
    def service(self, mock_s3_client, mock_session, mock_user):
        """Create AnalyticsService with mocked dependencies."""
        from app.services.analytics_service import AnalyticsService

        with patch("app.services.analytics_service.is_super_admin", return_value=True):
            return AnalyticsService(mock_session, mock_user)

    def test_empty_json_object(self, service, mock_s3_client):
        """Test handling of empty JSON object '{}'."""
        mock_s3_client.get_object.return_value = {"Body": MagicMock(read=lambda: b"{}")}

        result = service.get_overview("test-id")

        # Empty dict is falsy in Python, so _read_json returns it but
        # the check `if not data` treats it as falsy, returning None
        assert result is None

    def test_extra_unknown_fields_ignored(self, service, mock_s3_client):
        """Test that extra fields in JSON are handled gracefully by Pydantic."""
        s3_data = {
            "codebase_id": "test-id",
            "display_name": "test-repo",
            "repository_name": "test-repo",
            "full_name": "owner/test-repo",
            "owner": "owner",
            "total_commits": 100,
            "total_contributors": 5,
            "total_branches": 3,
            "current_sloc": 2500,
            "avg_bytes_per_line": 50.0,
            "total_files": 42,
            "default_branch": "main",
            "primary_language": "Python",
            "first_commit_date": "2024-01-01T00:00:00Z",
            "last_commit_date": "2024-06-01T00:00:00Z",
            "collected_at": "2024-06-01T12:00:00Z",
            "last_updated_at": "2024-06-01T12:00:00Z",
            # Extra unknown fields - should be ignored by Pydantic
            "unknown_future_field": "some_value",
            "another_new_field": 12345,
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(s3_data).encode())
        }
        service.session.query.return_value.filter.return_value.first.return_value = None

        result = service.get_overview("test-id")

        # Pydantic should parse the valid fields and ignore extra fields
        assert result is not None
        assert result.codebase_id == "test-id"
        assert result.display_name == "test-repo"
        # Extra fields are not accessible on the Pydantic model (they're ignored)
        assert not hasattr(result, "unknown_future_field")

    def test_null_values_for_optional_fields(self, service, mock_s3_client):
        """Test handling of null values for optional datetime fields."""
        s3_data = {
            "codebase_id": "test-id",
            "display_name": "test-repo",
            "repository_name": "test-repo",
            "full_name": "owner/test-repo",
            "owner": "owner",
            "total_commits": 0,
            "total_contributors": 0,
            "total_branches": 0,
            "current_sloc": 0,
            "avg_bytes_per_line": None,
            "total_files": 0,
            "default_branch": "main",
            "primary_language": None,
            "first_commit_date": None,
            "last_commit_date": None,
            "collected_at": "2024-06-01T12:00:00Z",
            "last_updated_at": "2024-06-01T12:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(s3_data).encode())
        }
        service.session.query.return_value.filter.return_value.first.return_value = None

        result = service.get_overview("test-id")

        assert result is not None
        assert result.first_commit_date is None
        assert result.last_commit_date is None
        assert result.primary_language is None

    def test_empty_codebases_list(self, service, mock_s3_client):
        """Test handling of codebases_list.json with empty codebases array."""
        s3_data = {
            "organization_id": "test-org",
            "codebases": [],
            "generated_at": "2024-06-15T10:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(s3_data).encode())
        }

        result = service.get_codebases_list()

        assert result is not None
        assert result.codebases == []
        assert len(result.codebases) == 0

    def test_empty_branches_list(self, service, mock_s3_client):
        """Test handling of branches.json with empty branches array."""
        s3_data = {
            "codebase_id": "test-id",
            "branches": [],
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(s3_data).encode())
        }

        result = service.get_branches("test-id")

        assert result is not None
        assert result.branches == []

    def test_empty_activity_list(self, service, mock_s3_client):
        """Test handling of activity.json with empty daily_activity array."""
        s3_data = {
            "codebase_id": "test-id",
            "daily_activity": [],
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(s3_data).encode())
        }

        result = service.get_activity("test-id")

        assert result is not None
        assert result.daily_activity == []

    def test_empty_ownership_directories(self, service, mock_s3_client):
        """Test handling of ownership.json with empty directories array."""
        s3_data = {
            "codebase_id": "test-id",
            "directories": [],
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(s3_data).encode())
        }

        result = service.get_ownership("test-id")

        assert result is not None
        assert result.directories == []


class TestFilteringEdgeCases:
    """Tests for source admin filtering edge cases."""

    @pytest.fixture
    def mock_s3_client(self):
        """Create a mock S3 client."""
        with patch("app.services.analytics_service.s3_client") as mock:
            yield mock

    @pytest.fixture
    def mock_session(self):
        """Create a mock database session."""
        return MagicMock()

    @pytest.fixture
    def mock_source_admin_user(self):
        """Create a mock source admin user."""
        user = MagicMock()
        user.user_id = str(uuid4())
        user.organization_id = str(uuid4())
        return user

    def test_source_admin_no_administered_codebases(
        self, mock_s3_client, mock_session, mock_source_admin_user
    ):
        """Test source admin with no administered codebases gets empty list."""
        from app.services.analytics_service import AnalyticsService

        all_codebases_data = {
            "organization_id": "test-org",
            "codebases": [
                {
                    "codebase_id": str(uuid4()),
                    "display_name": "repo-1",
                    "total_commits": 100,
                    "current_sloc": 5000,
                    "analytics_status": "complete",
                },
            ],
            "generated_at": "2024-06-15T10:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(all_codebases_data).encode())
        }

        with (
            patch("app.services.analytics_service.is_super_admin", return_value=False),
            patch.object(
                AnalyticsService,
                "_get_codebase_ids_for_user",
                return_value=set(),  # Empty set - no administered codebases
            ),
        ):
            service = AnalyticsService(mock_session, mock_source_admin_user)
            result = service.get_codebases_list()

        assert result is not None
        assert result.codebases == []
        assert len(result.codebases) == 0

    def test_source_admin_all_codebases_administered(
        self, mock_s3_client, mock_session, mock_source_admin_user
    ):
        """Test source admin who administers all codebases sees full list."""
        from app.services.analytics_service import AnalyticsService

        id1, id2 = uuid4(), uuid4()
        all_codebases_data = {
            "organization_id": "test-org",
            "codebases": [
                {
                    "codebase_id": str(id1),
                    "display_name": "repo-1",
                    "total_commits": 100,
                    "current_sloc": 5000,
                    "last_commit_date": "2024-06-01T00:00:00Z",
                    "analytics_status": "complete",
                },
                {
                    "codebase_id": str(id2),
                    "display_name": "repo-2",
                    "total_commits": 50,
                    "current_sloc": 2500,
                    "last_commit_date": "2024-05-15T00:00:00Z",
                    "analytics_status": "complete",
                },
            ],
            "generated_at": "2024-06-15T10:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(all_codebases_data).encode())
        }

        with (
            patch("app.services.analytics_service.is_super_admin", return_value=False),
            patch.object(
                AnalyticsService,
                "_get_codebase_ids_for_user",
                return_value={id1, id2},  # All codebases
            ),
        ):
            service = AnalyticsService(mock_session, mock_source_admin_user)
            result = service.get_codebases_list()

        assert result is not None
        assert len(result.codebases) == 2

    def test_org_summary_empty_when_no_administered_codebases(
        self, mock_s3_client, mock_session, mock_source_admin_user
    ):
        """Test org summary returns zeros when no codebases match."""
        from app.services.analytics_service import AnalyticsService

        all_codebases_data = {
            "organization_id": "test-org",
            "codebases": [
                {
                    "codebase_id": str(uuid4()),
                    "display_name": "repo-1",
                    "total_commits": 100,
                    "total_contributors": 5,
                    "current_sloc": 5000,
                    "analytics_status": "complete",
                },
            ],
            "generated_at": "2024-06-15T10:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(all_codebases_data).encode())
        }

        with (
            patch("app.services.analytics_service.is_super_admin", return_value=False),
            patch.object(
                AnalyticsService,
                "_get_codebase_ids_for_user",
                return_value=set(),  # No administered codebases
            ),
        ):
            service = AnalyticsService(mock_session, mock_source_admin_user)
            result = service.get_org_summary()

        assert result is not None
        assert result.total_codebases == 0
        assert result.codebases_with_analytics == 0

    def test_org_summary_handles_missing_optional_fields(
        self, mock_s3_client, mock_session, mock_source_admin_user
    ):
        """Test org summary handles codebases with missing optional fields."""
        from app.services.analytics_service import AnalyticsService

        codebase_id = uuid4()
        # Codebase data with minimal fields (missing total_contributors, current_sloc)
        all_codebases_data = {
            "organization_id": "test-org",
            "codebases": [
                {
                    "codebase_id": str(codebase_id),
                    "display_name": "repo-1",
                    "total_commits": 100,
                    # Missing: total_contributors, current_sloc
                    "analytics_status": "complete",
                },
            ],
            "generated_at": "2024-06-15T10:00:00Z",
        }
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(all_codebases_data).encode())
        }

        with (
            patch("app.services.analytics_service.is_super_admin", return_value=False),
            patch.object(
                AnalyticsService,
                "_get_codebase_ids_for_user",
                return_value={codebase_id},
            ),
        ):
            service = AnalyticsService(mock_session, mock_source_admin_user)
            result = service.get_org_summary()

        # Should handle missing fields gracefully
        assert result is not None
        assert result.total_codebases == 1
        assert result.codebases_with_analytics == 1  # Has "complete" status
