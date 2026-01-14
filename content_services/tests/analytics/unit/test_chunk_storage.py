"""Tests for ChunkStorage - S3 Parquet chunk operations.

This module tests the chunk storage functionality for memory-efficient
extraction:
- S3 key patterns for chunks
- Writing Parquet chunks with zstd compression
- Listing and deleting chunks
"""

from datetime import UTC, date, datetime
from io import BytesIO
from unittest.mock import MagicMock

import pyarrow.parquet as pq
from analytics.storage.chunk_storage import ChunkStorage


class TestChunkStorageKeyPatterns:
    """Verify S3 key patterns for chunks."""

    def test_commit_chunk_key_pattern(self):
        """Commit chunks use correct S3 key pattern."""
        mock_s3 = MagicMock()
        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")

        key = storage.get_commit_chunk_key(0)
        assert key == "analytics/codebase-123/chunks/commits/chunk_00000.parquet"

        key = storage.get_commit_chunk_key(42)
        assert key == "analytics/codebase-123/chunks/commits/chunk_00042.parquet"

        key = storage.get_commit_chunk_key(99999)
        assert key == "analytics/codebase-123/chunks/commits/chunk_99999.parquet"

    def test_file_change_chunk_key_pattern(self):
        """File change chunks use correct S3 key pattern."""
        mock_s3 = MagicMock()
        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")

        key = storage.get_file_change_chunk_key(0)
        assert key == "analytics/codebase-123/chunks/file_changes/chunk_00000.parquet"

        key = storage.get_file_change_chunk_key(15)
        assert key == "analytics/codebase-123/chunks/file_changes/chunk_00015.parquet"

    def test_chunk_prefix_pattern(self):
        """Chunk prefix for listing/deletion is correct."""
        mock_s3 = MagicMock()
        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")

        assert storage.get_chunks_prefix() == "analytics/codebase-123/chunks/"


class TestChunkStorageWrite:
    """Test writing Parquet chunks to S3."""

    def test_write_commit_chunk_creates_valid_parquet(self):
        """Writing commit chunk creates valid Parquet file."""
        mock_s3 = MagicMock()
        uploaded_data = {}

        def capture_upload(fileobj, bucket, key):
            uploaded_data["bucket"] = bucket
            uploaded_data["key"] = key
            uploaded_data["data"] = fileobj.read()

        mock_s3.upload_fileobj = capture_upload

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")

        # Sample commit records with all required fields
        records = [
            {
                "commit_sha": "abc123",
                "codebase_id": "codebase-123",
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
                "message": "Initial commit",
                "message_length": 14,
                "parent_count": 0,
                "is_merge_commit": False,
                "files_changed": 5,
                "additions_lines": 100,
                "deletions_lines": 0,
                "net_lines": 100,
                "churn_lines": 100,
                "addition_bytes": 5000,
                "deletion_bytes": 0,
                "patch_bytes": 5000,
                "net_bytes": 5000,
                "sloc": 100,
                "tree_bytes": 5000,
                "tree_lines": 100,
                "tree_sloc": 100,
                "bytes_per_line": 50.0,
                "commit_size_category": "small",
                "is_refactor": False,
                "collection_version": "2.0",
            }
        ]

        storage.write_commit_chunk(records, 0)

        # Verify upload was called
        assert uploaded_data["bucket"] == "test-bucket"
        assert (
            uploaded_data["key"]
            == "analytics/codebase-123/chunks/commits/chunk_00000.parquet"
        )

        # Verify data is valid Parquet
        buffer = BytesIO(uploaded_data["data"])
        table = pq.read_table(buffer)
        assert len(table) == 1
        assert table.column("commit_sha")[0].as_py() == "abc123"
        assert table.column("codebase_id")[0].as_py() == "codebase-123"

    def test_write_commit_chunk_uses_zstd_compression(self):
        """Chunks should use zstd compression."""
        mock_s3 = MagicMock()
        uploaded_data = {}

        def capture_upload(fileobj, bucket, key):
            uploaded_data["data"] = fileobj.read()

        mock_s3.upload_fileobj = capture_upload

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")

        records = [
            {
                "commit_sha": "abc123",
                "codebase_id": "codebase-123",
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
                "parent_count": 1,
                "is_merge_commit": False,
                "files_changed": 1,
                "additions_lines": 10,
                "deletions_lines": 5,
                "net_lines": 5,
                "churn_lines": 15,
                "addition_bytes": 500,
                "deletion_bytes": 250,
                "patch_bytes": 750,
                "net_bytes": 250,
                "sloc": 15,
                "tree_bytes": 10000,
                "tree_lines": 200,
                "tree_sloc": 200,
                "bytes_per_line": 50.0,
                "commit_size_category": "tiny",
                "is_refactor": False,
                "collection_version": "2.0",
            }
        ]
        storage.write_commit_chunk(records, 0)

        # Read back and check compression
        buffer = BytesIO(uploaded_data["data"])
        parquet_file = pq.ParquetFile(buffer)
        # zstd compression should be reflected in metadata
        compression = parquet_file.metadata.row_group(0).column(0).compression
        assert compression == "ZSTD"

    def test_write_file_change_chunk(self):
        """Writing file change chunk works correctly."""
        mock_s3 = MagicMock()
        uploaded_data = {}

        def capture_upload(fileobj, bucket, key):
            uploaded_data["key"] = key
            uploaded_data["data"] = fileobj.read()

        mock_s3.upload_fileobj = capture_upload

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")

        records = [
            {
                "codebase_id": "codebase-123",
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

        storage.write_file_change_chunk(records, 0)

        assert "file_changes" in uploaded_data["key"]
        assert (
            uploaded_data["key"]
            == "analytics/codebase-123/chunks/file_changes/chunk_00000.parquet"
        )

        buffer = BytesIO(uploaded_data["data"])
        table = pq.read_table(buffer)
        assert len(table) == 1
        assert table.column("file_path")[0].as_py() == "src/main.py"

    def test_write_multiple_records(self):
        """Can write multiple records in a single chunk."""
        mock_s3 = MagicMock()
        uploaded_data = {}

        def capture_upload(fileobj, bucket, key):
            uploaded_data["data"] = fileobj.read()

        mock_s3.upload_fileobj = capture_upload

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")

        records = [
            {
                "codebase_id": "codebase-123",
                "commit_sha": f"sha{i}",
                "file_path": f"src/file{i}.py",
                "commit_date": date(2024, 1, 15),
                "commit_year_month": "2024-01",
                "change_type": "modified",
                "previous_path": None,
                "additions_lines": i * 10,
                "deletions_lines": i * 5,
                "changes_lines": i * 15,
                "addition_bytes": i * 500,
                "deletion_bytes": i * 250,
                "file_sloc": i * 15,
                "file_extension": ".py",
                "file_language": "Python",
                "has_patch_data": False,
                "patch_blob_key": None,
            }
            for i in range(100)
        ]

        storage.write_file_change_chunk(records, 5)

        buffer = BytesIO(uploaded_data["data"])
        table = pq.read_table(buffer)
        assert len(table) == 100


class TestChunkStorageList:
    """Test listing chunks from S3."""

    def test_list_commit_chunks_returns_sorted_keys(self):
        """List chunks returns keys in correct order."""
        mock_s3 = MagicMock()

        # Mock S3 list_objects_v2 response (out of order)
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "analytics/codebase-123/chunks/commits/chunk_00002.parquet"},
                {"Key": "analytics/codebase-123/chunks/commits/chunk_00000.parquet"},
                {"Key": "analytics/codebase-123/chunks/commits/chunk_00001.parquet"},
            ]
        }

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")
        chunks = storage.list_commit_chunks()

        # Should be sorted by index
        assert chunks == [
            "analytics/codebase-123/chunks/commits/chunk_00000.parquet",
            "analytics/codebase-123/chunks/commits/chunk_00001.parquet",
            "analytics/codebase-123/chunks/commits/chunk_00002.parquet",
        ]

    def test_list_file_change_chunks(self):
        """List file change chunks works correctly."""
        mock_s3 = MagicMock()

        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": "analytics/codebase-123/chunks/file_changes/chunk_00001.parquet"
                },
                {
                    "Key": "analytics/codebase-123/chunks/file_changes/chunk_00000.parquet"
                },
            ]
        }

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")
        chunks = storage.list_file_change_chunks()

        assert chunks == [
            "analytics/codebase-123/chunks/file_changes/chunk_00000.parquet",
            "analytics/codebase-123/chunks/file_changes/chunk_00001.parquet",
        ]

    def test_list_chunks_empty_bucket(self):
        """List chunks returns empty list when no chunks exist."""
        mock_s3 = MagicMock()
        mock_s3.list_objects_v2.return_value = {}

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")
        chunks = storage.list_commit_chunks()

        assert chunks == []

    def test_list_chunks_handles_pagination(self):
        """List chunks handles paginated S3 responses."""
        mock_s3 = MagicMock()

        # First call returns truncated response
        mock_s3.list_objects_v2.side_effect = [
            {
                "Contents": [
                    {
                        "Key": "analytics/codebase-123/chunks/commits/chunk_00000.parquet"
                    },
                ],
                "IsTruncated": True,
                "NextContinuationToken": "token123",
            },
            {
                "Contents": [
                    {
                        "Key": "analytics/codebase-123/chunks/commits/chunk_00001.parquet"
                    },
                ],
                "IsTruncated": False,
            },
        ]

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")
        chunks = storage.list_commit_chunks()

        assert len(chunks) == 2
        assert chunks[0].endswith("chunk_00000.parquet")
        assert chunks[1].endswith("chunk_00001.parquet")


class TestChunkStorageDelete:
    """Test deleting chunks from S3."""

    def test_delete_all_chunks(self):
        """Delete all chunks for a codebase."""
        mock_s3 = MagicMock()

        # Mock list response with both commit and file change chunks
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "analytics/codebase-123/chunks/commits/chunk_00000.parquet"},
                {"Key": "analytics/codebase-123/chunks/commits/chunk_00001.parquet"},
                {
                    "Key": "analytics/codebase-123/chunks/file_changes/chunk_00000.parquet"
                },
            ]
        }

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")
        storage.delete_all_chunks()

        # Verify delete_objects was called with correct keys
        mock_s3.delete_objects.assert_called_once()
        call_args = mock_s3.delete_objects.call_args
        assert call_args[1]["Bucket"] == "test-bucket"
        deleted_keys = [obj["Key"] for obj in call_args[1]["Delete"]["Objects"]]
        assert len(deleted_keys) == 3

    def test_delete_chunks_handles_empty(self):
        """Delete handles case when no chunks exist."""
        mock_s3 = MagicMock()
        mock_s3.list_objects_v2.return_value = {}

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")
        storage.delete_all_chunks()  # Should not raise

        mock_s3.delete_objects.assert_not_called()

    def test_delete_chunks_batches_large_deletes(self):
        """Delete batches requests for >1000 objects (S3 limit)."""
        mock_s3 = MagicMock()

        # Mock list response with many chunks
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": f"analytics/codebase-123/chunks/commits/chunk_{i:05d}.parquet"}
                for i in range(1500)
            ]
        }

        storage = ChunkStorage(mock_s3, "test-bucket", "codebase-123")
        storage.delete_all_chunks()

        # Should make 2 delete calls (1000 + 500)
        assert mock_s3.delete_objects.call_count == 2
