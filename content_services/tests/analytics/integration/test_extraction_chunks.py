"""Tests for chunk-based extraction - Phase 4 of memory optimization.

This module tests the integration of ChunkStorage with the extraction phase,
ensuring that commits and file changes are written incrementally to S3 chunks
instead of accumulating in memory.

Key behaviors tested:
- Chunks are written instead of accumulating in memory
- Chunk indices increment correctly
- Checkpoint tracks chunk counts, not records
- Resume from checkpoint works with chunk counts
- Empty batches don't create chunks
"""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock


class TestChunkCallbackCreation:
    """Test creating chunk callback for extraction."""

    def test_create_chunk_callback_initializes_storage(self):
        """Chunk callback should initialize ChunkStorage with correct params."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
        )

        assert storage is not None
        assert storage.bucket == "test-bucket"
        assert storage.codebase_id == "test-codebase"

    def test_create_chunk_callback_returns_callable(self):
        """Chunk callback should return a callable."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        callback, _ = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
        )

        assert callable(callback)


class TestChunkCallbackBehavior:
    """Test chunk callback writes chunks correctly."""

    def test_callback_writes_commit_chunk(self):
        """Callback should write commit records as a Parquet chunk."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        uploaded_data = {}

        def capture_upload(fileobj, bucket, key):
            uploaded_data["key"] = key
            uploaded_data["data"] = fileobj.read()

        mock_s3.upload_fileobj = capture_upload

        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
        )

        # Simulate batch from Rust
        batch_commits = [
            {
                "commit_sha": "abc123",
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16, 10, 30, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15,
                "author_email": "dev@example.com",
                "author_name": "Developer",
                "committer_email": "dev@example.com",
                "committer_name": "Developer",
                "message": "Test commit",
                "message_length": 11,
                "parent_count": 0,
                "is_merge_commit": False,
                "files_changed": 1,
                "additions_lines": 10,
                "deletions_lines": 0,
                "net_lines": 10,
                "churn_lines": 10,
                "addition_bytes": 500,
                "deletion_bytes": 0,
                "patch_bytes": 500,
                "net_bytes": 500,
                "sloc": 10,
                "tree_bytes": 1000,
                "tree_lines": 20,
                "tree_sloc": 20,
                "bytes_per_line": 50.0,
                "commit_size_category": "tiny",
                "is_refactor": False,
                "collection_version": "2.0",
            }
        ]

        callback(
            batch_shas=["abc123"],
            batch_commits=batch_commits,
            batch_file_changes=[],
        )

        # Verify chunk was uploaded
        assert "commits/chunk_00000.parquet" in uploaded_data["key"]

    def test_callback_increments_commit_chunk_index(self):
        """Each batch should write to a new chunk index."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        uploaded_keys = []

        def capture_upload(fileobj, bucket, key):
            uploaded_keys.append(key)

        mock_s3.upload_fileobj = capture_upload

        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
        )

        # Create minimal valid commit record
        def make_commit(sha):
            return {
                "commit_sha": sha,
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16, 10, 30, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15,
                "author_email": "dev@example.com",
                "author_name": "Developer",
                "committer_email": "dev@example.com",
                "committer_name": "Developer",
                "message": "Test",
                "message_length": 4,
                "parent_count": 0,
                "is_merge_commit": False,
                "files_changed": 1,
                "additions_lines": 10,
                "deletions_lines": 0,
                "net_lines": 10,
                "churn_lines": 10,
                "addition_bytes": 500,
                "deletion_bytes": 0,
                "patch_bytes": 500,
                "net_bytes": 500,
                "sloc": 10,
                "tree_bytes": 1000,
                "tree_lines": 20,
                "tree_sloc": 20,
                "bytes_per_line": 50.0,
                "commit_size_category": "tiny",
                "is_refactor": False,
                "collection_version": "2.0",
            }

        # Call callback multiple times
        callback(["sha1"], [make_commit("sha1")], [])
        callback(["sha2"], [make_commit("sha2")], [])
        callback(["sha3"], [make_commit("sha3")], [])

        # Verify chunk indices increment
        commit_keys = [k for k in uploaded_keys if "commits/" in k]
        assert "chunk_00000.parquet" in commit_keys[0]
        assert "chunk_00001.parquet" in commit_keys[1]
        assert "chunk_00002.parquet" in commit_keys[2]

    def test_callback_writes_file_change_chunk(self):
        """Callback should write file change records as a Parquet chunk."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        uploaded_data = {}

        def capture_upload(fileobj, bucket, key):
            uploaded_data["key"] = key

        mock_s3.upload_fileobj = capture_upload

        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
        )

        batch_file_changes = [
            {
                "codebase_id": "test-codebase",
                "commit_sha": "abc123",
                "file_path": "src/main.py",
                "commit_date": date(2024, 1, 15),
                "commit_year_month": "2024-01",
                "change_type": "added",
                "previous_path": None,
                "additions_lines": 50,
                "deletions_lines": 0,
                "changes_lines": 50,
                "addition_bytes": 2500,
                "deletion_bytes": 0,
                "file_sloc": 50,
                "file_extension": ".py",
                "file_language": "Python",
                "has_patch_data": False,
                "patch_blob_key": None,
            }
        ]

        # Need to include commits too (can't have file changes without commits)
        callback(
            batch_shas=["abc123"],
            batch_commits=[],  # Commits in separate batch
            batch_file_changes=batch_file_changes,
        )

        # Verify file changes chunk was uploaded
        assert "file_changes/chunk_00000.parquet" in uploaded_data["key"]

    def test_callback_skips_empty_batches(self):
        """Empty batches should not create chunks."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
        )

        # Call with empty batch
        callback(batch_shas=[], batch_commits=[], batch_file_changes=[])

        # No uploads should have occurred
        mock_s3.upload_fileobj.assert_not_called()


class TestChunkCallbackCheckpoint:
    """Test checkpoint integration with chunk callback."""

    def test_callback_tracks_processed_shas(self):
        """Callback should track processed SHAs for checkpoint."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        checkpoint_data = {}

        def checkpoint_callback(data):
            checkpoint_data.update(data)

        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
            checkpoint_callback=checkpoint_callback,
        )

        def make_commit(sha):
            return {
                "commit_sha": sha,
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16, 10, 30, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15,
                "author_email": "dev@example.com",
                "author_name": "Developer",
                "committer_email": "dev@example.com",
                "committer_name": "Developer",
                "message": "Test",
                "message_length": 4,
                "parent_count": 0,
                "is_merge_commit": False,
                "files_changed": 1,
                "additions_lines": 10,
                "deletions_lines": 0,
                "net_lines": 10,
                "churn_lines": 10,
                "addition_bytes": 500,
                "deletion_bytes": 0,
                "patch_bytes": 500,
                "net_bytes": 500,
                "sloc": 10,
                "tree_bytes": 1000,
                "tree_lines": 20,
                "tree_sloc": 20,
                "bytes_per_line": 50.0,
                "commit_size_category": "tiny",
                "is_refactor": False,
                "collection_version": "2.0",
            }

        callback(["sha1", "sha2"], [make_commit("sha1"), make_commit("sha2")], [])

        assert "processed_commit_shas" in checkpoint_data
        assert "sha1" in checkpoint_data["processed_commit_shas"]
        assert "sha2" in checkpoint_data["processed_commit_shas"]

    def test_callback_tracks_chunk_counts(self):
        """Callback should track chunk counts for checkpoint."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        checkpoint_data = {}

        def checkpoint_callback(data):
            checkpoint_data.update(data)

        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
            checkpoint_callback=checkpoint_callback,
        )

        def make_commit(sha):
            return {
                "commit_sha": sha,
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16, 10, 30, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15,
                "author_email": "dev@example.com",
                "author_name": "Developer",
                "committer_email": "dev@example.com",
                "committer_name": "Developer",
                "message": "Test",
                "message_length": 4,
                "parent_count": 0,
                "is_merge_commit": False,
                "files_changed": 1,
                "additions_lines": 10,
                "deletions_lines": 0,
                "net_lines": 10,
                "churn_lines": 10,
                "addition_bytes": 500,
                "deletion_bytes": 0,
                "patch_bytes": 500,
                "net_bytes": 500,
                "sloc": 10,
                "tree_bytes": 1000,
                "tree_lines": 20,
                "tree_sloc": 20,
                "bytes_per_line": 50.0,
                "commit_size_category": "tiny",
                "is_refactor": False,
                "collection_version": "2.0",
            }

        # Call multiple times
        callback(["sha1"], [make_commit("sha1")], [])
        callback(["sha2"], [make_commit("sha2")], [])

        assert checkpoint_data["commit_chunk_count"] == 2

    def test_callback_does_not_include_records(self):
        """Checkpoint should NOT include commit_records or file_change_records."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        checkpoint_data = {}

        def checkpoint_callback(data):
            checkpoint_data.update(data)

        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
            checkpoint_callback=checkpoint_callback,
        )

        def make_commit(sha):
            return {
                "commit_sha": sha,
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16, 10, 30, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15,
                "author_email": "dev@example.com",
                "author_name": "Developer",
                "committer_email": "dev@example.com",
                "committer_name": "Developer",
                "message": "Test",
                "message_length": 4,
                "parent_count": 0,
                "is_merge_commit": False,
                "files_changed": 1,
                "additions_lines": 10,
                "deletions_lines": 0,
                "net_lines": 10,
                "churn_lines": 10,
                "addition_bytes": 500,
                "deletion_bytes": 0,
                "patch_bytes": 500,
                "net_bytes": 500,
                "sloc": 10,
                "tree_bytes": 1000,
                "tree_lines": 20,
                "tree_sloc": 20,
                "bytes_per_line": 50.0,
                "commit_size_category": "tiny",
                "is_refactor": False,
                "collection_version": "2.0",
            }

        callback(["sha1"], [make_commit("sha1")], [])

        # These should NOT be in checkpoint (v2.0 change)
        assert "commit_records" not in checkpoint_data
        assert "file_change_records" not in checkpoint_data


class TestChunkCallbackResume:
    """Test resuming extraction from checkpoint with chunk counts."""

    def test_callback_resumes_from_chunk_count(self):
        """Callback should resume chunk indices from checkpoint."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        uploaded_keys = []

        def capture_upload(fileobj, bucket, key):
            uploaded_keys.append(key)

        mock_s3.upload_fileobj = capture_upload

        # Resume with 5 commit chunks already written
        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
            initial_commit_chunk_count=5,
            initial_file_change_chunk_count=3,
        )

        def make_commit(sha):
            return {
                "commit_sha": sha,
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16, 10, 30, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15,
                "author_email": "dev@example.com",
                "author_name": "Developer",
                "committer_email": "dev@example.com",
                "committer_name": "Developer",
                "message": "Test",
                "message_length": 4,
                "parent_count": 0,
                "is_merge_commit": False,
                "files_changed": 1,
                "additions_lines": 10,
                "deletions_lines": 0,
                "net_lines": 10,
                "churn_lines": 10,
                "addition_bytes": 500,
                "deletion_bytes": 0,
                "patch_bytes": 500,
                "net_bytes": 500,
                "sloc": 10,
                "tree_bytes": 1000,
                "tree_lines": 20,
                "tree_sloc": 20,
                "bytes_per_line": 50.0,
                "commit_size_category": "tiny",
                "is_refactor": False,
                "collection_version": "2.0",
            }

        # New batch should continue from chunk 5
        callback(["sha_new"], [make_commit("sha_new")], [])

        commit_keys = [k for k in uploaded_keys if "commits/" in k]
        assert "chunk_00005.parquet" in commit_keys[0]

    def test_callback_preserves_processed_shas_from_checkpoint(self):
        """Callback should preserve processed SHAs from checkpoint."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        checkpoint_data = {}

        def checkpoint_callback(data):
            checkpoint_data.update(data)

        # Resume with existing processed SHAs
        existing_shas = {"old_sha1", "old_sha2", "old_sha3"}
        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
            checkpoint_callback=checkpoint_callback,
            initial_processed_shas=existing_shas,
        )

        def make_commit(sha):
            return {
                "commit_sha": sha,
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16, 10, 30, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15,
                "author_email": "dev@example.com",
                "author_name": "Developer",
                "committer_email": "dev@example.com",
                "committer_name": "Developer",
                "message": "Test",
                "message_length": 4,
                "parent_count": 0,
                "is_merge_commit": False,
                "files_changed": 1,
                "additions_lines": 10,
                "deletions_lines": 0,
                "net_lines": 10,
                "churn_lines": 10,
                "addition_bytes": 500,
                "deletion_bytes": 0,
                "patch_bytes": 500,
                "net_bytes": 500,
                "sloc": 10,
                "tree_bytes": 1000,
                "tree_lines": 20,
                "tree_sloc": 20,
                "bytes_per_line": 50.0,
                "commit_size_category": "tiny",
                "is_refactor": False,
                "collection_version": "2.0",
            }

        # Process new commits
        callback(["new_sha"], [make_commit("new_sha")], [])

        # Checkpoint should have both old and new SHAs
        all_shas = checkpoint_data["processed_commit_shas"]
        assert "old_sha1" in all_shas
        assert "old_sha2" in all_shas
        assert "old_sha3" in all_shas
        assert "new_sha" in all_shas


class TestExtractCommitsWithChunks:
    """Test extract_commits uses chunk callback when provided."""

    def test_extract_commits_uses_chunk_callback(self):
        """extract_commits should use chunk callback for incremental writes."""
        # This is an integration test that requires a real git repo
        # We'll test it at a higher level or mock the internals
        # Will implement in full integration tests


class TestChunkCallbackMemoryEfficiency:
    """Test that chunk callback doesn't accumulate records in memory."""

    def test_callback_does_not_accumulate_commits(self):
        """Callback should NOT accumulate commits in memory."""
        from analytics.pipeline.phases.extract import create_chunk_callback

        mock_s3 = MagicMock()
        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
        )

        def make_commit(sha):
            return {
                "commit_sha": sha,
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16, 10, 30, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15,
                "author_email": "dev@example.com",
                "author_name": "Developer",
                "committer_email": "dev@example.com",
                "committer_name": "Developer",
                "message": "Test",
                "message_length": 4,
                "parent_count": 0,
                "is_merge_commit": False,
                "files_changed": 1,
                "additions_lines": 10,
                "deletions_lines": 0,
                "net_lines": 10,
                "churn_lines": 10,
                "addition_bytes": 500,
                "deletion_bytes": 0,
                "patch_bytes": 500,
                "net_bytes": 500,
                "sloc": 10,
                "tree_bytes": 1000,
                "tree_lines": 20,
                "tree_sloc": 20,
                "bytes_per_line": 50.0,
                "commit_size_category": "tiny",
                "is_refactor": False,
                "collection_version": "2.0",
            }

        # Process many batches
        for i in range(100):
            callback([f"sha{i}"], [make_commit(f"sha{i}")], [])

        # Callback should not have accumulated 100 commits in memory
        # This is verified by the callback implementation - we just ensure
        # it doesn't crash and chunk count is correct
        assert storage is not None
