"""Integration tests for incremental pipeline.

Tests the full pipeline flow with incremental updates.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

# Add src to path for imports
_src_path = Path(__file__).parent.parent.parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))

# Mock Hatchet before any imports
sys.modules["hatchet_client"] = MagicMock()

import pygit2


def create_test_repo_with_commits(repo_path: Path, num_commits: int) -> list[str]:
    """Create a test repo with specified number of commits.

    Returns list of commit SHAs in order (oldest first).
    """
    repo_path.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(repo_path))

    config = repo.config
    config["user.name"] = "Test User"
    config["user.email"] = "test@example.com"

    commit_shas = []
    for i in range(num_commits):
        file_path = repo_path / "file.txt"
        file_path.write_text(f"Content version {i}\n" + "x" * (i * 100))

        repo.index.add("file.txt")
        repo.index.write()

        tree = repo.index.write_tree()
        sig = pygit2.Signature("Test User", "test@example.com")
        parents = [repo.head.target] if not repo.head_is_unborn else []

        commit_oid = repo.create_commit(
            "HEAD", sig, sig, f"Commit {i + 1}", tree, parents
        )
        commit_shas.append(str(commit_oid))

    return commit_shas


class TestIncrementalPipelineIntegration:
    """Integration tests for incremental pipeline flow."""

    def test_full_pipeline_creates_checkpoint(self, tmp_path):
        """Full pipeline run creates checkpoint fields in metadata.json."""
        from analytics.pipeline.orchestrator import (
            AnalyticsPipeline,
            PipelineConfig,
            PipelineInput,
        )

        # Create test repo
        repo_path = tmp_path / "repo"
        create_test_repo_with_commits(repo_path, 5)  # SHAs not needed for this test

        # Configure pipeline
        config = PipelineConfig(
            work_dir=tmp_path / "work",
            cleanup_on_complete=False,
        )
        pipeline = AnalyticsPipeline(config)

        # Mock S3 operations - simulate no checkpoint exists
        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.put_object.return_value = {}
            mock_s3.get_object.side_effect = ClientError(
                {"Error": {"Code": "NoSuchKey"}}, "GetObject"
            )

            result = pipeline.run(
                PipelineInput(
                    codebase_id="test-codebase-id",
                    organization_id="test-org-id",
                    clone_url=str(repo_path),  # Use local path
                    repo_owner="test",
                    repo_name="repo",
                    incremental=False,
                )
            )

        assert result.success
        assert result.total_commits == 5

        # Check metadata.json has checkpoint fields
        json_dir = tmp_path / "work" / "test-codebase-id" / "json"
        metadata_path = json_dir / "metadata.json"

        assert metadata_path.exists()
        with open(metadata_path) as f:
            metadata = json.load(f)

        assert metadata["pipeline_version"] == "2.0"
        assert metadata["total_commits_processed"] == 5
        assert metadata["last_processed_commit_sha"] is not None
        # The checkpoint SHA should be one of the commits in the repo
        assert len(metadata["last_processed_commit_sha"]) == 40  # Valid SHA format

    def test_incremental_only_processes_new_commits(self, tmp_path):
        """Incremental mode only extracts commits since checkpoint."""
        from analytics.pipeline.orchestrator import (
            AnalyticsPipeline,
            PipelineConfig,
            PipelineInput,
        )

        # Create test repo with 3 initial commits
        repo_path = tmp_path / "repo"
        commit_shas = create_test_repo_with_commits(repo_path, 3)

        # Configure pipeline
        config = PipelineConfig(
            work_dir=tmp_path / "work",
            cleanup_on_complete=False,
        )
        pipeline = AnalyticsPipeline(config)

        # Run full pipeline first - simulate no checkpoint exists
        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.put_object.return_value = {}
            mock_s3.get_object.side_effect = ClientError(
                {"Error": {"Code": "NoSuchKey"}}, "GetObject"
            )

            result1 = pipeline.run(
                PipelineInput(
                    codebase_id="test-codebase-id",
                    organization_id="test-org-id",
                    clone_url=str(repo_path),
                    repo_owner="test",
                    repo_name="repo",
                    incremental=False,
                )
            )

        assert result1.success
        assert result1.total_commits == 3

        # Now add 2 more commits
        repo = pygit2.Repository(str(repo_path))
        for i in range(2):
            file_path = repo_path / "file.txt"
            file_path.write_text(f"New content {i}\n")
            repo.index.add("file.txt")
            repo.index.write()
            tree = repo.index.write_tree()
            sig = pygit2.Signature("Test User", "test@example.com")
            commit_oid = repo.create_commit(
                "HEAD", sig, sig, f"New commit {i + 1}", tree, [repo.head.target]
            )
            commit_shas.append(str(commit_oid))

        # Create a new pipeline for incremental run
        pipeline2 = AnalyticsPipeline(config)

        # Run incremental - mock metadata.json from previous run
        # The orchestrator reads from metadata.json (not checkpoint.json) for incremental mode
        metadata_json = {
            "pipeline_version": "2.0",
            "last_processed_commit_sha": commit_shas[2],  # 3rd commit (index 2)
            "total_commits_processed": 3,
        }

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.put_object.return_value = {}
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=lambda: json.dumps(metadata_json).encode())
            }

            result2 = pipeline2.run(
                PipelineInput(
                    codebase_id="test-codebase-id",
                    organization_id="test-org-id",
                    clone_url=str(repo_path),
                    repo_owner="test",
                    repo_name="repo",
                    incremental=True,
                )
            )

        assert result2.success
        # Should only extract 2 new commits
        assert result2.total_commits == 2

    def test_incremental_with_no_new_commits(self, tmp_path):
        """Incremental mode processes zero commits when up to date."""
        from analytics.pipeline.orchestrator import (
            AnalyticsPipeline,
            PipelineConfig,
            PipelineInput,
        )

        # Create test repo
        repo_path = tmp_path / "repo"
        commit_shas = create_test_repo_with_commits(repo_path, 5)

        config = PipelineConfig(
            work_dir=tmp_path / "work",
            cleanup_on_complete=False,
        )
        pipeline = AnalyticsPipeline(config)

        # Mock checkpoint pointing to latest commit
        checkpoint_data = {
            "last_processed_commit_sha": commit_shas[-1],  # Latest commit
            "total_commits_processed": 5,
        }

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.put_object.return_value = {}
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=lambda: json.dumps(checkpoint_data).encode())
            }

            result = pipeline.run(
                PipelineInput(
                    codebase_id="test-codebase-id",
                    organization_id="test-org-id",
                    clone_url=str(repo_path),
                    repo_owner="test",
                    repo_name="repo",
                    incremental=True,
                )
            )

        assert result.success
        # Should extract zero new commits
        assert result.total_commits == 0

    def test_incremental_falls_back_to_full_on_invalid_checkpoint(self, tmp_path):
        """Incremental mode falls back to full extraction if checkpoint SHA not found."""
        from analytics.pipeline.orchestrator import (
            AnalyticsPipeline,
            PipelineConfig,
            PipelineInput,
        )

        # Create test repo
        repo_path = tmp_path / "repo"
        create_test_repo_with_commits(repo_path, 5)  # SHAs not needed for this test

        config = PipelineConfig(
            work_dir=tmp_path / "work",
            cleanup_on_complete=False,
        )
        pipeline = AnalyticsPipeline(config)

        # Mock checkpoint with invalid SHA
        checkpoint_data = {
            "last_processed_commit_sha": "0" * 40,  # Invalid SHA
            "total_commits_processed": 100,
        }

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.put_object.return_value = {}
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=lambda: json.dumps(checkpoint_data).encode())
            }

            result = pipeline.run(
                PipelineInput(
                    codebase_id="test-codebase-id",
                    organization_id="test-org-id",
                    clone_url=str(repo_path),
                    repo_owner="test",
                    repo_name="repo",
                    incremental=True,
                )
            )

        assert result.success
        # Should fall back to full extraction (5 commits)
        assert result.total_commits == 5


class TestExtractFilteringIntegration:
    """Integration tests for extract phase filtering."""

    def test_extract_since_sha_filters_correctly(self, tmp_path):
        """Extract phase correctly filters commits based on since_sha."""
        from analytics.pipeline.phases.extract import extract_commits

        # Create repo with 10 commits
        repo_path = tmp_path / "repo"
        commit_shas = create_test_repo_with_commits(repo_path, 10)

        repo = pygit2.Repository(str(repo_path))

        # Extract only commits since the 5th commit (index 4)
        result = extract_commits(
            repo=repo,
            codebase_id="test-codebase-id",
            since_sha=commit_shas[4],
        )

        assert result.success
        # Should have 5 new commits (indices 5-9)
        assert result.total_commits == 5

        # Verify the commit SHAs are correct
        extracted_shas = {c["commit_sha"] for c in result.commits}
        expected_shas = set(commit_shas[5:])
        assert extracted_shas == expected_shas
