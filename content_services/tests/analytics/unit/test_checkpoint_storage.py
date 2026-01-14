"""Unit tests for checkpoint data class and storage.

TDD: These tests are written BEFORE implementation.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch


class TestExtractionCheckpoint:
    """Tests for the ExtractionCheckpoint Pydantic model."""

    def test_checkpoint_serialization_roundtrip(self):
        """Checkpoint should serialize to JSON and deserialize identically."""
        from analytics.checkpoint import ExtractionCheckpoint

        checkpoint = ExtractionCheckpoint(
            codebase_id="test-uuid-123",
            started_at=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
            commits_total=1000,
            commits_processed=500,
            last_processed_index=499,
            last_processed_sha="abc123def456",
            tree_size_cache={
                "abc123": (1000, 50),
                "def456": (2000, 100),
            },
        )

        # Serialize
        json_str = checkpoint.model_dump_json()

        # Deserialize
        restored = ExtractionCheckpoint.model_validate_json(json_str)

        # Verify identical
        assert restored.codebase_id == checkpoint.codebase_id
        assert restored.started_at == checkpoint.started_at
        assert restored.commits_total == checkpoint.commits_total
        assert restored.commits_processed == checkpoint.commits_processed
        assert restored.last_processed_index == checkpoint.last_processed_index
        assert restored.last_processed_sha == checkpoint.last_processed_sha
        assert restored.tree_size_cache == checkpoint.tree_size_cache

    def test_checkpoint_version_included(self):
        """Checkpoint should include version for compatibility checks."""
        from analytics.checkpoint import CHECKPOINT_VERSION, ExtractionCheckpoint

        checkpoint = ExtractionCheckpoint(
            codebase_id="test",
            started_at=datetime.now(UTC),
            commits_total=100,
            commits_processed=50,
            last_processed_index=49,
            last_processed_sha="abc123",
            tree_size_cache={},
        )

        data = json.loads(checkpoint.model_dump_json())
        assert data["version"] == CHECKPOINT_VERSION

    def test_checkpoint_default_timestamps(self):
        """last_updated_at should default to now."""
        from analytics.checkpoint import ExtractionCheckpoint

        before = datetime.now(UTC)
        checkpoint = ExtractionCheckpoint(
            codebase_id="test",
            started_at=datetime.now(UTC),
            commits_total=100,
            commits_processed=50,
            last_processed_index=49,
            last_processed_sha="abc123",
            tree_size_cache={},
        )
        after = datetime.now(UTC)

        assert before <= checkpoint.last_updated_at <= after

    def test_checkpoint_large_cache(self):
        """Should handle large tree_size_cache (100K+ entries)."""
        from analytics.checkpoint import ExtractionCheckpoint

        # Simulate 100K commits
        large_cache = {f"sha_{i}": (i * 100, i * 10) for i in range(100_000)}

        checkpoint = ExtractionCheckpoint(
            codebase_id="test",
            started_at=datetime.now(UTC),
            commits_total=100_000,
            commits_processed=100_000,
            last_processed_index=99_999,
            last_processed_sha="sha_99999",
            tree_size_cache=large_cache,
        )

        # Serialize and deserialize
        json_str = checkpoint.model_dump_json()
        restored = ExtractionCheckpoint.model_validate_json(json_str)

        assert len(restored.tree_size_cache) == 100_000
        assert restored.tree_size_cache["sha_50000"] == (5_000_000, 500_000)


class TestCheckpointValidation:
    """Tests for checkpoint validation logic."""

    def test_valid_checkpoint_passes(self):
        """Valid checkpoint should pass validation."""
        from analytics.checkpoint import ExtractionCheckpoint, validate_checkpoint

        checkpoint = ExtractionCheckpoint(
            codebase_id="test",
            started_at=datetime.now(UTC),
            commits_total=100,
            commits_processed=50,
            last_processed_index=49,
            last_processed_sha="abc123",
            tree_size_cache={"abc123": (100, 10)},
        )

        # Mock repo that can find the commit
        mock_repo = MagicMock()
        mock_repo.get.return_value = MagicMock()  # Commit exists

        assert validate_checkpoint(mock_repo, checkpoint) is True

    def test_missing_commit_fails_validation(self):
        """Checkpoint referencing missing commit should fail."""
        from analytics.checkpoint import ExtractionCheckpoint, validate_checkpoint

        checkpoint = ExtractionCheckpoint(
            codebase_id="test",
            started_at=datetime.now(UTC),
            commits_total=100,
            commits_processed=50,
            last_processed_index=49,
            last_processed_sha="deleted_sha",
            tree_size_cache={},
        )

        # Mock repo that can't find the commit
        mock_repo = MagicMock()
        mock_repo.get.side_effect = KeyError("Commit not found")

        assert validate_checkpoint(mock_repo, checkpoint) is False

    def test_version_mismatch_fails_validation(self):
        """Checkpoint with wrong version should fail."""
        from analytics.checkpoint import (
            validate_checkpoint_version,
        )

        # Manually create checkpoint data with wrong version
        checkpoint_data = {
            "version": "0.9",  # Old version
            "codebase_id": "test",
            "started_at": datetime.now(UTC).isoformat(),
            "last_updated_at": datetime.now(UTC).isoformat(),
            "commits_total": 100,
            "commits_processed": 50,
            "last_processed_index": 49,
            "last_processed_sha": "abc123",
            "tree_size_cache": {},
        }

        assert validate_checkpoint_version(checkpoint_data) is False

    def test_tree_size_cache_validation_with_valid_data(self):
        """Valid tree_size_cache should pass validation."""
        from analytics.checkpoint import _validate_tree_size_cache

        valid_cache = {
            "sha1": (100, 50),
            "sha2": (200, 100),
            "sha3": [300, 150],  # Lists should also be accepted
        }
        assert _validate_tree_size_cache(valid_cache) is True

    def test_tree_size_cache_validation_with_string_values(self):
        """tree_size_cache with string values should still pass (coercible to int)."""
        from analytics.checkpoint import _validate_tree_size_cache

        # Numeric strings are coercible to int
        cache_with_strings = {
            "sha1": ("100", "50"),
            "sha2": ("200", "100"),
        }
        assert _validate_tree_size_cache(cache_with_strings) is True

    def test_tree_size_cache_validation_with_non_numeric_strings(self):
        """tree_size_cache with non-numeric strings should fail validation."""
        from analytics.checkpoint import _validate_tree_size_cache

        invalid_cache = {
            "sha1": ("not_a_number", "50"),
        }
        assert _validate_tree_size_cache(invalid_cache) is False

    def test_tree_size_cache_validation_with_wrong_structure(self):
        """tree_size_cache with wrong structure should fail validation."""
        from analytics.checkpoint import _validate_tree_size_cache

        # Single value instead of tuple
        invalid_cache = {"sha1": 100}
        assert _validate_tree_size_cache(invalid_cache) is False

        # Tuple with wrong length
        invalid_cache = {"sha1": (100, 50, 25)}
        assert _validate_tree_size_cache(invalid_cache) is False

    def test_tree_size_cache_validation_empty_cache(self):
        """Empty tree_size_cache should pass validation."""
        from analytics.checkpoint import _validate_tree_size_cache

        assert _validate_tree_size_cache({}) is True


class TestCheckpointS3Storage:
    """Tests for S3 checkpoint upload/download."""

    def test_upload_checkpoint_to_s3(self):
        """Should upload checkpoint JSON to correct S3 path."""
        from analytics.checkpoint import ExtractionCheckpoint, upload_checkpoint

        checkpoint = ExtractionCheckpoint(
            codebase_id="test-uuid",
            started_at=datetime.now(UTC),
            commits_total=100,
            commits_processed=50,
            last_processed_index=49,
            last_processed_sha="abc123",
            tree_size_cache={"abc123": (100, 10)},
        )

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3

            upload_checkpoint(checkpoint, bucket="test-bucket")

            mock_s3.put_object.assert_called_once()
            call_kwargs = mock_s3.put_object.call_args.kwargs
            assert call_kwargs["Bucket"] == "test-bucket"
            assert call_kwargs["Key"] == "analytics/test-uuid/checkpoint.json"
            assert "Body" in call_kwargs

    def test_download_existing_checkpoint(self):
        """Should download and parse existing checkpoint."""
        from analytics.checkpoint import CHECKPOINT_VERSION, download_checkpoint

        checkpoint_data = {
            "version": CHECKPOINT_VERSION,  # Use current version (2.0)
            "codebase_id": "test-uuid",
            "started_at": "2024-01-15T10:30:00Z",
            "last_updated_at": "2024-01-15T11:00:00Z",
            "commits_total": 100,
            "commits_processed": 50,
            "last_processed_index": 49,
            "last_processed_sha": "abc123",
            "tree_size_cache": {"abc123": [100, 10]},
            "processed_commit_shas": [],
            # v2.0: chunk counts instead of records
            "commit_chunk_count": 5,
            "file_change_chunk_count": 3,
        }

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=lambda: json.dumps(checkpoint_data).encode())
            }

            result = download_checkpoint(bucket="test-bucket", codebase_id="test-uuid")

            assert result is not None
            assert result.codebase_id == "test-uuid"
            assert result.commits_processed == 50

    def test_download_returns_none_when_missing(self):
        """Should return None when checkpoint doesn't exist."""
        from analytics.checkpoint import download_checkpoint
        from botocore.exceptions import ClientError

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.get_object.side_effect = ClientError(
                {"Error": {"Code": "NoSuchKey"}}, "GetObject"
            )

            result = download_checkpoint(bucket="test-bucket", codebase_id="test-uuid")

            assert result is None

    def test_delete_checkpoint(self):
        """Should delete checkpoint from S3."""
        from analytics.checkpoint import delete_checkpoint

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3

            delete_checkpoint(bucket="test-bucket", codebase_id="test-uuid")

            mock_s3.delete_object.assert_called_once_with(
                Bucket="test-bucket", Key="analytics/test-uuid/checkpoint.json"
            )
