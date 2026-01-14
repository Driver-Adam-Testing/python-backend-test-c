"""Integration tests for orchestrator checkpoint/resume functionality.

TDD: These tests are written BEFORE implementation.

These tests verify that:
1. Orchestrator saves extraction checkpoints to S3 during long extractions
2. Orchestrator can resume from a checkpoint if extraction was interrupted
3. Checkpoint is deleted after successful completion

NOTE: These tests require the native Rust extension for checkpoint callbacks.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pygit2
import pytest
from analytics.checkpoint import ExtractionCheckpoint


def native_extension_available():
    try:
        from analytics_native import calculate_tree_sizes_incremental  # noqa: F401

        return True
    except ImportError:
        return False


# xfail all tests in this module if native extension not available
pytestmark = pytest.mark.xfail(
    not native_extension_available(),
    run=False,
    reason="Requires native extension for checkpoint callbacks",
)


class TestOrchestratorCheckpointIntegration:
    """Tests for checkpoint saving during extraction."""

    @pytest.fixture
    def mock_s3_client(self):
        """Mock S3 client for testing."""
        return MagicMock()

    @pytest.fixture
    def test_repo_with_history(self, temp_dir):
        """Create a test repo with commit history."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

        # Create 100 commits
        for i in range(100):
            (repo_path / f"file_{i % 10}.py").write_text(f"content_{i}\n" * (i % 3 + 1))
            index = repo.index
            index.add_all()
            index.write()
            tree = index.write_tree()

            sig = pygit2.Signature("Test", "test@test.com")
            parents = [repo.head.target] if not repo.head_is_unborn else []
            repo.create_commit("HEAD", sig, sig, f"Commit {i}", tree, parents)

        return repo_path, repo

    def test_extraction_saves_checkpoint_to_s3(
        self, test_repo_with_history, mock_s3_client
    ):
        """Extraction should save checkpoints to S3 at intervals."""
        repo_path, repo = test_repo_with_history

        checkpoint_uploads = []

        def capture_upload(*args: object, **kwargs: object) -> None:
            # Capture checkpoint uploads
            key = kwargs.get("Key", "")
            if isinstance(key, str) and "checkpoint.json" in key:
                body = kwargs.get("Body", b"")
                if isinstance(body, bytes):
                    checkpoint_uploads.append(json.loads(body.decode("utf-8")))

        mock_s3_client.put_object.side_effect = capture_upload

        with patch("analytics.checkpoint.boto3.client", return_value=mock_s3_client):
            with patch("boto3.client", return_value=mock_s3_client):
                from analytics.checkpoint import ExtractionCheckpoint, upload_checkpoint
                from analytics.pipeline.phases.extract import extract_commits

                # Manually test checkpoint callback
                callback_data = []

                def checkpoint_callback(data):
                    callback_data.append(data)
                    # Simulate what orchestrator would do: upload to S3
                    checkpoint = ExtractionCheckpoint(
                        codebase_id=data["codebase_id"],
                        started_at=datetime.now(UTC),
                        commits_total=data["commits_total"],
                        commits_processed=data["commits_processed"],
                        last_processed_index=data["last_processed_index"],
                        last_processed_sha=data["last_processed_sha"],
                        tree_size_cache=data["tree_size_cache"],
                    )
                    upload_checkpoint(checkpoint, "test-bucket", mock_s3_client)

                extract_commits(
                    repo=repo,
                    codebase_id="test-codebase",
                    checkpoint_callback=checkpoint_callback,
                    checkpoint_interval_secs=0.001,  # Very short for testing
                )

                # Should have received callback calls
                assert len(callback_data) > 0

                # Mock should have received checkpoint uploads
                assert mock_s3_client.put_object.call_count > 0


class TestOrchestratorCheckpointResume:
    """Tests for resuming from checkpoint."""

    @pytest.fixture
    def test_repo_50_commits(self, temp_dir):
        """Create a test repo with 50 commits."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

        commit_shas = []
        for i in range(50):
            (repo_path / f"file_{i % 5}.py").write_text(f"content_{i}\n")
            index = repo.index
            index.add_all()
            index.write()
            tree = index.write_tree()

            sig = pygit2.Signature("Test", "test@test.com")
            parents = [repo.head.target] if not repo.head_is_unborn else []
            oid = repo.create_commit("HEAD", sig, sig, f"Commit {i}", tree, parents)
            commit_shas.append(str(oid))

        return repo_path, repo, commit_shas

    def test_resume_from_checkpoint_produces_identical_results(
        self, test_repo_50_commits
    ):
        """Resume from checkpoint should produce identical results."""
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo, commit_shas = test_repo_50_commits

        # Step 1: Full extraction (baseline)
        full_result = extract_commits(
            repo=repo,
            codebase_id="test-codebase",
        )

        # Step 2: Capture midpoint checkpoint
        midpoint_checkpoint = None

        def capture_midpoint(data):
            nonlocal midpoint_checkpoint
            if data["commits_processed"] >= 25 and midpoint_checkpoint is None:
                midpoint_checkpoint = ExtractionCheckpoint(
                    codebase_id="test-codebase",
                    started_at=datetime.now(UTC),
                    commits_total=data["commits_total"],
                    commits_processed=data["commits_processed"],
                    last_processed_index=data["last_processed_index"],
                    last_processed_sha=data["last_processed_sha"],
                    tree_size_cache=data["tree_size_cache"],
                )

        extract_commits(
            repo=repo,
            codebase_id="test-codebase",
            checkpoint_callback=capture_midpoint,
            checkpoint_interval_secs=0.001,
        )

        assert midpoint_checkpoint is not None, "Should have captured checkpoint"

        # Step 3: Resume from checkpoint
        resumed_result = extract_commits(
            repo=repo,
            codebase_id="test-codebase",
            checkpoint=midpoint_checkpoint,
        )

        # Step 4: Verify identical results
        assert len(resumed_result.commits) == len(full_result.commits)

        # Compare tree_sloc values
        full_tree_sloc = {c["commit_sha"]: c["tree_sloc"] for c in full_result.commits}
        resumed_tree_sloc = {
            c["commit_sha"]: c["tree_sloc"] for c in resumed_result.commits
        }

        for sha, tree_sloc in full_tree_sloc.items():
            assert sha in resumed_tree_sloc, f"Missing commit {sha}"
            assert resumed_tree_sloc[sha] == tree_sloc, (
                f"tree_sloc mismatch for {sha}: "
                f"resumed={resumed_tree_sloc[sha]} vs full={tree_sloc}"
            )


class TestOrchestratorCheckpointCleanup:
    """Tests for checkpoint cleanup after completion."""

    def test_checkpoint_deleted_after_successful_extraction(self):
        """Checkpoint should be deleted after successful extraction."""
        from analytics.checkpoint import delete_checkpoint

        mock_s3 = MagicMock()

        with patch("analytics.checkpoint.boto3.client", return_value=mock_s3):
            delete_checkpoint("test-bucket", "test-codebase", mock_s3)

            mock_s3.delete_object.assert_called_once_with(
                Bucket="test-bucket",
                Key="analytics/test-codebase/checkpoint.json",
            )


class TestOrchestratorCheckpointValidation:
    """Tests for checkpoint validation in orchestrator."""

    @pytest.fixture
    def test_repo_with_history(self, temp_dir):
        """Create a test repo."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

        # Create commits
        for i in range(10):
            (repo_path / f"file_{i}.py").write_text(f"x = {i}\n")
            index = repo.index
            index.add_all()
            index.write()
            tree = index.write_tree()

            sig = pygit2.Signature("Test", "test@test.com")
            parents = [repo.head.target] if not repo.head_is_unborn else []
            repo.create_commit("HEAD", sig, sig, f"Commit {i}", tree, parents)

        return repo_path, repo

    def test_invalid_checkpoint_is_discarded(self, test_repo_with_history):
        """Checkpoint with invalid SHA should be discarded."""
        from analytics.checkpoint import validate_checkpoint

        repo_path, repo = test_repo_with_history

        # Create checkpoint with non-existent SHA (not all zeros - use a real-looking but fake SHA)
        invalid_checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            commits_total=100,
            commits_processed=50,
            last_processed_index=49,
            last_processed_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",  # Invalid - doesn't exist
            tree_size_cache={},
        )

        # Validation should fail
        assert validate_checkpoint(repo, invalid_checkpoint) is False

    def test_valid_checkpoint_passes_validation(self, test_repo_with_history):
        """Checkpoint with valid SHA should pass validation."""
        from analytics.checkpoint import validate_checkpoint

        repo_path, repo = test_repo_with_history

        # Get a valid commit SHA from the repo
        valid_sha = str(repo.head.target)

        valid_checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            commits_total=10,
            commits_processed=5,
            last_processed_index=4,
            last_processed_sha=valid_sha,
            tree_size_cache={valid_sha: (100, 10)},
        )

        # Validation should pass
        assert validate_checkpoint(repo, valid_checkpoint) is True
