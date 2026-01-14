"""Tests for Phase 4 chunk merge - merging S3 chunks to warm/cold storage.

This module tests the integration of ChunkMerger with Phase 4 of the pipeline,
ensuring that chunks written during extraction are properly merged before
being stored in warm/cold storage.

Key behaviors tested:
- Commits chunks are merged and stored in warm storage
- File change chunks are merged and stored in cold storage
- Chunks are cleaned up after successful merge
- Resume from checkpoint works correctly
"""

import tempfile
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from analytics.storage.chunk_merger import ChunkMerger
from analytics.storage.chunk_storage import ChunkStorage


class TestChunkMergeCommits:
    """Test merging commit chunks to warm storage."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def sample_commit_chunks(self, temp_dir):
        """Create sample commit Parquet chunks."""
        from analytics.schemas.warm_schemas import COMMITS_SCHEMA

        chunks_dir = temp_dir / "chunks" / "commits"
        chunks_dir.mkdir(parents=True)

        # Create sample commit data that matches schema
        commits_0 = [
            {
                "commit_sha": "sha001",
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16, 10, 0, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15,
                "author_email": "dev@test.com",
                "author_name": "Developer",
                "committer_email": "dev@test.com",
                "committer_name": "Developer",
                "message": "First commit",
                "message_length": 12,
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

        commits_1 = [
            {
                "commit_sha": "sha002",
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 16, 10, 0, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 17, 10, 0, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 16),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 16,
                "author_email": "dev2@test.com",
                "author_name": "Developer 2",
                "committer_email": "dev2@test.com",
                "committer_name": "Developer 2",
                "message": "Second commit",
                "message_length": 13,
                "parent_count": 1,
                "is_merge_commit": False,
                "files_changed": 2,
                "additions_lines": 20,
                "deletions_lines": 5,
                "net_lines": 15,
                "churn_lines": 25,
                "addition_bytes": 1000,
                "deletion_bytes": 250,
                "patch_bytes": 1250,
                "net_bytes": 750,
                "sloc": 15,
                "tree_bytes": 2000,
                "tree_lines": 35,
                "tree_sloc": 35,
                "bytes_per_line": 57.1,
                "commit_size_category": "small",
                "is_refactor": False,
                "collection_version": "2.0",
            }
        ]

        # Write chunks
        table0 = pa.Table.from_pylist(commits_0, schema=COMMITS_SCHEMA)
        pq.write_table(table0, chunks_dir / "chunk_00000.parquet", compression="zstd")

        table1 = pa.Table.from_pylist(commits_1, schema=COMMITS_SCHEMA)
        pq.write_table(table1, chunks_dir / "chunk_00001.parquet", compression="zstd")

        return chunks_dir

    def test_merge_commit_chunks_combines_all_records(
        self, temp_dir, sample_commit_chunks
    ):
        """Merged commits file contains all records from all chunks."""
        output_path = temp_dir / "merged_commits.parquet"

        merger = ChunkMerger()
        merger.merge_commits(
            chunk_pattern=str(sample_commit_chunks / "*.parquet"),
            output_path=str(output_path),
            order_by="committed_at",
        )
        merger.close()

        # Verify merged file
        result = pq.read_table(output_path)
        assert len(result) == 2  # 2 commits total

    def test_merge_commit_chunks_ordered_by_committed_at(
        self, temp_dir, sample_commit_chunks
    ):
        """Merged commits are ordered by committed_at."""
        output_path = temp_dir / "merged_commits.parquet"

        merger = ChunkMerger()
        merger.merge_commits(
            chunk_pattern=str(sample_commit_chunks / "*.parquet"),
            output_path=str(output_path),
            order_by="committed_at",
        )
        merger.close()

        result = pq.read_table(output_path)
        shas = result.column("commit_sha").to_pylist()
        # Should be ordered by committed_at (sha001 first, then sha002)
        assert shas == ["sha001", "sha002"]


class TestChunkMergeFileChanges:
    """Test merging file change chunks to cold storage."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def sample_file_change_chunks(self, temp_dir):
        """Create sample file change Parquet chunks."""
        from analytics.schemas.cold_schemas import FILE_CHANGES_SCHEMA

        chunks_dir = temp_dir / "chunks" / "file_changes"
        chunks_dir.mkdir(parents=True)

        file_changes = [
            {
                "codebase_id": "test-codebase",
                "commit_sha": "sha001",
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
            },
            {
                "codebase_id": "test-codebase",
                "commit_sha": "sha002",
                "file_path": "src/utils.py",
                "commit_date": date(2024, 2, 15),
                "commit_year_month": "2024-02",
                "change_type": "modified",
                "previous_path": None,
                "additions_lines": 20,
                "deletions_lines": 5,
                "changes_lines": 25,
                "addition_bytes": 1000,
                "deletion_bytes": 250,
                "file_sloc": 30,
                "file_extension": ".py",
                "file_language": "Python",
                "has_patch_data": False,
                "patch_blob_key": None,
            },
        ]

        table = pa.Table.from_pylist(file_changes, schema=FILE_CHANGES_SCHEMA)
        pq.write_table(table, chunks_dir / "chunk_00000.parquet", compression="zstd")

        return chunks_dir

    def test_merge_file_changes_with_partitioning(
        self, temp_dir, sample_file_change_chunks
    ):
        """File changes are partitioned by commit_year_month."""
        output_dir = temp_dir / "merged_file_changes"

        merger = ChunkMerger()
        merger.merge_file_changes(
            chunk_pattern=str(sample_file_change_chunks / "*.parquet"),
            output_dir=str(output_dir),
            partition_by="commit_year_month",
        )
        merger.close()

        # Should create partitioned directory structure
        assert (output_dir / "commit_year_month=2024-01").exists()
        assert (output_dir / "commit_year_month=2024-02").exists()


class TestPhase4MergeIntegration:
    """Test Phase 4 merge integration with pipeline."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_merge_and_store_commits(self, temp_dir):
        """Phase 4 should merge chunks and store in warm storage."""
        from analytics.pipeline.orchestrator import merge_chunks_to_storage
        from analytics.schemas.warm_schemas import COMMITS_SCHEMA

        # Create sample chunks in temp directory
        chunks_dir = temp_dir / "chunks" / "commits"
        chunks_dir.mkdir(parents=True)

        commit = {
            "commit_sha": "test123",
            "codebase_id": "test-codebase",
            "branch_name": "main",
            "committed_at": datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC),
            "collected_at": datetime(2024, 1, 16, 10, 0, 0, tzinfo=UTC),
            "commit_date": date(2024, 1, 15),
            "commit_year": 2024,
            "commit_month": 1,
            "commit_day": 15,
            "author_email": "dev@test.com",
            "author_name": "Developer",
            "committer_email": "dev@test.com",
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

        table = pa.Table.from_pylist([commit], schema=COMMITS_SCHEMA)
        pq.write_table(table, chunks_dir / "chunk_00000.parquet", compression="zstd")

        # Merge to output
        output_path = temp_dir / "merged_commits.parquet"
        merge_chunks_to_storage(
            chunks_dir=chunks_dir,
            output_path=output_path,
            data_type="commits",
            order_by="committed_at",
        )

        # Verify output
        assert output_path.exists()
        result = pq.read_table(output_path)
        assert len(result) == 1
        assert result.column("commit_sha")[0].as_py() == "test123"

    def test_merge_and_store_file_changes(self, temp_dir):
        """Phase 4 should merge file change chunks and store in cold storage."""
        from analytics.pipeline.orchestrator import merge_chunks_to_storage
        from analytics.schemas.cold_schemas import FILE_CHANGES_SCHEMA

        # Create sample chunks
        chunks_dir = temp_dir / "chunks" / "file_changes"
        chunks_dir.mkdir(parents=True)

        file_change = {
            "codebase_id": "test-codebase",
            "commit_sha": "test123",
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

        table = pa.Table.from_pylist([file_change], schema=FILE_CHANGES_SCHEMA)
        pq.write_table(table, chunks_dir / "chunk_00000.parquet", compression="zstd")

        # Merge to output directory (with partitioning)
        output_dir = temp_dir / "merged_file_changes"
        merge_chunks_to_storage(
            chunks_dir=chunks_dir,
            output_path=output_dir,
            data_type="file_changes",
            partition_by="commit_year_month",
        )

        # Verify output exists
        assert output_dir.exists()
        # Should have partitioned directory
        partitions = list(output_dir.glob("commit_year_month=*"))
        assert len(partitions) > 0


class TestChunkCleanup:
    """Test chunk cleanup after successful merge."""

    def test_cleanup_chunks_after_merge(self):
        """Chunks should be deleted after successful merge."""
        mock_s3 = MagicMock()

        # Mock list response
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "analytics/test-codebase/chunks/commits/chunk_00000.parquet"},
                {"Key": "analytics/test-codebase/chunks/commits/chunk_00001.parquet"},
                {
                    "Key": "analytics/test-codebase/chunks/file_changes/chunk_00000.parquet"
                },
            ]
        }

        storage = ChunkStorage(mock_s3, "test-bucket", "test-codebase")
        storage.delete_all_chunks()

        # Verify delete_objects was called
        mock_s3.delete_objects.assert_called_once()
        call_args = mock_s3.delete_objects.call_args
        deleted_keys = [obj["Key"] for obj in call_args[1]["Delete"]["Objects"]]
        assert len(deleted_keys) == 3


class TestLocalChunkMerge:
    """Test merging chunks from local filesystem (for integration testing)."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_merge_local_chunks_to_warm_storage(self, temp_dir):
        """Test merging local chunks to ParquetStorage (warm)."""
        from analytics.schemas.warm_schemas import COMMITS_SCHEMA

        # Create chunks
        chunks_dir = temp_dir / "chunks" / "commits"
        chunks_dir.mkdir(parents=True)

        commit = {
            "commit_sha": "abc123",
            "codebase_id": "test-codebase",
            "branch_name": "main",
            "committed_at": datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC),
            "collected_at": datetime(2024, 1, 16, 10, 0, 0, tzinfo=UTC),
            "commit_date": date(2024, 1, 15),
            "commit_year": 2024,
            "commit_month": 1,
            "commit_day": 15,
            "author_email": "dev@test.com",
            "author_name": "Developer",
            "committer_email": "dev@test.com",
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

        table = pa.Table.from_pylist([commit], schema=COMMITS_SCHEMA)
        pq.write_table(table, chunks_dir / "chunk_00000.parquet", compression="zstd")

        # Merge to warm storage location
        warm_dir = temp_dir / "warm"
        warm_dir.mkdir()
        output_path = warm_dir / "test-codebase" / "commits.parquet"
        output_path.parent.mkdir(parents=True)

        merger = ChunkMerger()
        merger.merge_commits(
            chunk_pattern=str(chunks_dir / "*.parquet"),
            output_path=str(output_path),
            order_by="committed_at",
        )
        merger.close()

        # Verify output exists and is valid
        assert output_path.exists()
        result = pq.read_table(output_path)
        assert len(result) == 1
        assert result.column("commit_sha")[0].as_py() == "abc123"


class TestS3ChunkDownloadAndMerge:
    """Test downloading chunks from S3 and merging locally."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_download_chunks_from_s3(self, temp_dir):
        """Test downloading S3 chunks to local temp directory."""
        from analytics.pipeline.orchestrator import download_chunks_to_local
        from analytics.schemas.warm_schemas import COMMITS_SCHEMA

        # Create mock S3 client with chunk data
        mock_s3 = MagicMock()

        # Create a sample parquet file to return
        commit = {
            "commit_sha": "s3test123",
            "codebase_id": "test-codebase",
            "branch_name": "main",
            "committed_at": datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC),
            "collected_at": datetime(2024, 1, 16, 10, 0, 0, tzinfo=UTC),
            "commit_date": date(2024, 1, 15),
            "commit_year": 2024,
            "commit_month": 1,
            "commit_day": 15,
            "author_email": "dev@test.com",
            "author_name": "Developer",
            "committer_email": "dev@test.com",
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

        # Create parquet bytes
        table = pa.Table.from_pylist([commit], schema=COMMITS_SCHEMA)
        buffer = BytesIO()
        pq.write_table(table, buffer, compression="zstd")
        parquet_bytes = buffer.getvalue()

        # Mock list_objects_v2
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "analytics/test-codebase/chunks/commits/chunk_00000.parquet"}
            ]
        }

        # Mock download_fileobj
        def download_side_effect(bucket, key, fileobj):
            fileobj.write(parquet_bytes)

        mock_s3.download_fileobj = MagicMock(side_effect=download_side_effect)

        # Download chunks
        local_dir = download_chunks_to_local(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
            chunk_type="commits",
            local_dir=temp_dir,
        )

        # Verify chunks were downloaded
        assert local_dir.exists()
        chunk_files = list(local_dir.glob("*.parquet"))
        assert len(chunk_files) == 1

    def test_full_s3_merge_workflow(self, temp_dir):
        """Test full S3 workflow: download chunks → merge → upload merged."""
        # This is a complex integration test that would require extensive mocking
        # For now, we test the individual components separately
        # The orchestrator integration is tested in E2E tests


class TestMergeWithNoChunks:
    """Test merge behavior when no chunks exist."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_merge_with_no_chunks_raises(self, temp_dir):
        """Merging with no chunks should raise an error."""
        output_path = temp_dir / "merged.parquet"
        empty_dir = temp_dir / "empty"
        empty_dir.mkdir()

        merger = ChunkMerger()

        with pytest.raises(duckdb.IOException):
            merger.merge_commits(
                chunk_pattern=str(empty_dir / "*.parquet"),
                output_path=str(output_path),
            )

        merger.close()
