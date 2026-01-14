"""Tests for ChunkMerger - DuckDB streaming merge of Parquet chunks.

This module tests the chunk merger functionality for combining Parquet
chunks into final storage files:
- Merging commit chunks with optional ordering
- Merging file changes with partitioning
- Memory-efficient streaming (not loading all data into memory)
"""

import tempfile
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from analytics.storage.chunk_merger import ChunkMerger


@pytest.fixture
def temp_dir():
    """Create temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_commit_chunks(temp_dir):
    """Create sample commit Parquet chunks."""
    chunks_dir = temp_dir / "chunks" / "commits"
    chunks_dir.mkdir(parents=True)

    schema = pa.schema(
        [
            ("commit_sha", pa.string()),
            ("codebase_id", pa.string()),
            ("committed_at", pa.string()),
            ("author_email", pa.string()),
        ]
    )

    # Chunk 0: 2 commits (later timestamps)
    table0 = pa.table(
        {
            "commit_sha": ["sha001", "sha002"],
            "codebase_id": ["test", "test"],
            "committed_at": ["2024-01-03T10:00:00Z", "2024-01-04T10:00:00Z"],
            "author_email": ["a@test.com", "b@test.com"],
        },
        schema=schema,
    )
    pq.write_table(table0, chunks_dir / "chunk_00000.parquet", compression="zstd")

    # Chunk 1: 3 commits (earlier timestamps - to verify ordering)
    table1 = pa.table(
        {
            "commit_sha": ["sha003", "sha004", "sha005"],
            "codebase_id": ["test", "test", "test"],
            "committed_at": [
                "2024-01-01T10:00:00Z",
                "2024-01-02T10:00:00Z",
                "2024-01-05T10:00:00Z",
            ],
            "author_email": ["c@test.com", "a@test.com", "b@test.com"],
        },
        schema=schema,
    )
    pq.write_table(table1, chunks_dir / "chunk_00001.parquet", compression="zstd")

    return chunks_dir


@pytest.fixture
def sample_file_change_chunks(temp_dir):
    """Create sample file change Parquet chunks with partition column."""
    chunks_dir = temp_dir / "chunks" / "file_changes"
    chunks_dir.mkdir(parents=True)

    schema = pa.schema(
        [
            ("commit_sha", pa.string()),
            ("file_path", pa.string()),
            ("commit_year_month", pa.string()),
            ("additions_lines", pa.int32()),
        ]
    )

    # Chunk with mixed months
    table = pa.table(
        {
            "commit_sha": ["sha001", "sha001", "sha002", "sha003"],
            "file_path": ["a.py", "b.py", "c.py", "d.py"],
            "commit_year_month": ["2024-01", "2024-01", "2024-02", "2024-02"],
            "additions_lines": [10, 20, 30, 40],
        },
        schema=schema,
    )
    pq.write_table(table, chunks_dir / "chunk_00000.parquet", compression="zstd")

    return chunks_dir


class TestChunkMergerCommits:
    """Test merging commit chunks."""

    def test_merge_commit_chunks_combines_all_records(
        self, temp_dir, sample_commit_chunks
    ):
        """Merged file contains all records from all chunks."""
        output_path = temp_dir / "merged_commits.parquet"

        merger = ChunkMerger()
        merger.merge_commits(
            chunk_pattern=str(sample_commit_chunks / "*.parquet"),
            output_path=str(output_path),
        )

        # Verify merged file
        result = pq.read_table(output_path)
        assert len(result) == 5  # 2 + 3 records

    def test_merge_commit_chunks_preserves_all_columns(
        self, temp_dir, sample_commit_chunks
    ):
        """All columns are preserved in merged output."""
        output_path = temp_dir / "merged_commits.parquet"

        merger = ChunkMerger()
        merger.merge_commits(
            chunk_pattern=str(sample_commit_chunks / "*.parquet"),
            output_path=str(output_path),
        )

        result = pq.read_table(output_path)
        assert "commit_sha" in result.column_names
        assert "author_email" in result.column_names
        assert "committed_at" in result.column_names
        assert "codebase_id" in result.column_names

    def test_merge_commit_chunks_orders_by_committed_at(
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

        result = pq.read_table(output_path)
        timestamps = result.column("committed_at").to_pylist()
        assert timestamps == sorted(timestamps)

    def test_merge_commit_chunks_uses_zstd_compression(
        self, temp_dir, sample_commit_chunks
    ):
        """Output uses zstd compression."""
        output_path = temp_dir / "merged_commits.parquet"

        merger = ChunkMerger()
        merger.merge_commits(
            chunk_pattern=str(sample_commit_chunks / "*.parquet"),
            output_path=str(output_path),
        )

        parquet_file = pq.ParquetFile(output_path)
        compression = parquet_file.metadata.row_group(0).column(0).compression
        assert compression == "ZSTD"

    def test_merge_without_ordering(self, temp_dir, sample_commit_chunks):
        """Merge works without ordering specification."""
        output_path = temp_dir / "merged_commits.parquet"

        merger = ChunkMerger()
        merger.merge_commits(
            chunk_pattern=str(sample_commit_chunks / "*.parquet"),
            output_path=str(output_path),
        )

        result = pq.read_table(output_path)
        assert len(result) == 5


class TestChunkMergerFileChanges:
    """Test merging file change chunks with partitioning."""

    def test_merge_file_changes_with_partition(
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

        # Should create partitioned directory structure
        assert (output_dir / "commit_year_month=2024-01").exists()
        assert (output_dir / "commit_year_month=2024-02").exists()

    def test_merge_file_changes_partition_has_correct_records(
        self, temp_dir, sample_file_change_chunks
    ):
        """Each partition contains only records for that month."""
        output_dir = temp_dir / "merged_file_changes"

        merger = ChunkMerger()
        merger.merge_file_changes(
            chunk_pattern=str(sample_file_change_chunks / "*.parquet"),
            output_dir=str(output_dir),
            partition_by="commit_year_month",
        )

        # Check January partition
        jan_dir = output_dir / "commit_year_month=2024-01"
        jan_files = list(jan_dir.glob("*.parquet"))
        assert len(jan_files) > 0
        jan_table = pq.read_table(jan_files[0])
        assert len(jan_table) == 2

        # Check February partition
        feb_dir = output_dir / "commit_year_month=2024-02"
        feb_files = list(feb_dir.glob("*.parquet"))
        assert len(feb_files) > 0
        feb_table = pq.read_table(feb_files[0])
        assert len(feb_table) == 2

    def test_merge_file_changes_preserves_data(
        self, temp_dir, sample_file_change_chunks
    ):
        """All data is preserved across partitions."""
        output_dir = temp_dir / "merged_file_changes"

        merger = ChunkMerger()
        merger.merge_file_changes(
            chunk_pattern=str(sample_file_change_chunks / "*.parquet"),
            output_dir=str(output_dir),
            partition_by="commit_year_month",
        )

        # Read all partitions
        result = pq.read_table(output_dir)
        assert len(result) == 4  # Total records


class TestChunkMergerMemory:
    """Test that merger doesn't load all data into memory."""

    def test_merge_streams_without_full_memory_load(self, temp_dir):
        """DuckDB streaming should not require full dataset in memory."""
        # Create many chunks to ensure streaming is necessary
        chunks_dir = temp_dir / "chunks"
        chunks_dir.mkdir()

        schema = pa.schema([("id", pa.int64()), ("data", pa.string())])

        # Create 10 chunks with 10K records each (100K total)
        for i in range(10):
            table = pa.table(
                {
                    "id": list(range(i * 10000, (i + 1) * 10000)),
                    "data": [f"record_{j}" for j in range(i * 10000, (i + 1) * 10000)],
                },
                schema=schema,
            )
            pq.write_table(table, chunks_dir / f"chunk_{i:05d}.parquet")

        output_path = temp_dir / "merged.parquet"

        # This should complete without OOM
        merger = ChunkMerger()
        merger.merge_generic(
            chunk_pattern=str(chunks_dir / "*.parquet"),
            output_path=str(output_path),
        )

        result = pq.read_table(output_path)
        assert len(result) == 100000

    def test_merge_large_chunk_count(self, temp_dir):
        """Can merge many small chunks efficiently."""
        chunks_dir = temp_dir / "chunks"
        chunks_dir.mkdir()

        schema = pa.schema([("id", pa.int64())])

        # Create 100 small chunks
        for i in range(100):
            table = pa.table({"id": [i]}, schema=schema)
            pq.write_table(table, chunks_dir / f"chunk_{i:05d}.parquet")

        output_path = temp_dir / "merged.parquet"

        merger = ChunkMerger()
        merger.merge_generic(
            chunk_pattern=str(chunks_dir / "*.parquet"),
            output_path=str(output_path),
        )

        result = pq.read_table(output_path)
        assert len(result) == 100


class TestChunkMergerCleanup:
    """Test merger resource cleanup."""

    def test_close_releases_connection(self, temp_dir, sample_commit_chunks):
        """Close method releases DuckDB connection."""
        merger = ChunkMerger()
        output_path = temp_dir / "merged.parquet"

        merger.merge_commits(
            chunk_pattern=str(sample_commit_chunks / "*.parquet"),
            output_path=str(output_path),
        )

        merger.close()

        # Connection should be closed - verify file is still readable
        result = pq.read_table(output_path)
        assert len(result) == 5


class TestChunkMergerEdgeCases:
    """Test edge cases for chunk merger."""

    def test_merge_single_chunk(self, temp_dir):
        """Can merge a single chunk."""
        chunks_dir = temp_dir / "chunks"
        chunks_dir.mkdir()

        schema = pa.schema([("id", pa.int64()), ("value", pa.string())])
        table = pa.table({"id": [1, 2, 3], "value": ["a", "b", "c"]}, schema=schema)
        pq.write_table(table, chunks_dir / "chunk_00000.parquet")

        output_path = temp_dir / "merged.parquet"

        merger = ChunkMerger()
        merger.merge_generic(
            chunk_pattern=str(chunks_dir / "*.parquet"),
            output_path=str(output_path),
        )

        result = pq.read_table(output_path)
        assert len(result) == 3

    def test_merge_empty_result(self, temp_dir):
        """Handles case with no matching chunks gracefully."""
        chunks_dir = temp_dir / "empty_chunks"
        chunks_dir.mkdir()

        output_path = temp_dir / "merged.parquet"

        merger = ChunkMerger()

        # Pattern matches no files - should raise or handle gracefully
        with pytest.raises(duckdb.IOException):
            merger.merge_generic(
                chunk_pattern=str(chunks_dir / "*.parquet"),
                output_path=str(output_path),
            )
