"""Tests for checkpoint v2.0 schema with chunk-based storage.

This module tests the checkpoint schema changes needed for memory-efficient
chunk-based extraction:
- Version 2.0 schema
- Chunk count fields instead of record lists
- Serialization/deserialization compatibility
"""

from datetime import UTC, datetime

from analytics.checkpoint import CHECKPOINT_VERSION, ExtractionCheckpoint


class TestCheckpointV2Schema:
    """Verify checkpoint v2.0 schema supports chunk-based storage."""

    def test_checkpoint_version_is_2_0(self):
        """New checkpoints should have version 2.0."""
        checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            commits_total=1000,
            commits_processed=500,
            last_processed_index=499,
            last_processed_sha="abc123",
            tree_size_cache={},
            processed_commit_shas=set(),
        )
        assert checkpoint.version == "2.0"
        assert CHECKPOINT_VERSION == "2.0"

    def test_checkpoint_has_chunk_count_fields(self):
        """Checkpoint must have commit_chunk_count and file_change_chunk_count."""
        checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            commits_total=1000,
            commits_processed=500,
            last_processed_index=499,
            last_processed_sha="abc123",
            tree_size_cache={},
            processed_commit_shas=set(),
            commit_chunk_count=5,
            file_change_chunk_count=5,
        )
        assert checkpoint.commit_chunk_count == 5
        assert checkpoint.file_change_chunk_count == 5

    def test_checkpoint_chunk_counts_default_to_zero(self):
        """Chunk counts should default to 0 for backward compatibility."""
        checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            commits_total=1000,
            commits_processed=0,
            last_processed_index=-1,
            last_processed_sha="",
            tree_size_cache={},
            processed_commit_shas=set(),
        )
        assert checkpoint.commit_chunk_count == 0
        assert checkpoint.file_change_chunk_count == 0

    def test_checkpoint_no_commit_records_field(self):
        """v2.0 checkpoint should not have commit_records field."""
        checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            commits_total=1000,
            commits_processed=0,
            last_processed_index=-1,
            last_processed_sha="",
            tree_size_cache={},
            processed_commit_shas=set(),
        )
        # Should not have commit_records attribute in model fields
        field_names = set(checkpoint.model_fields.keys())
        assert "commit_records" not in field_names

    def test_checkpoint_no_file_change_records_field(self):
        """v2.0 checkpoint should not have file_change_records field."""
        checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            commits_total=1000,
            commits_processed=0,
            last_processed_index=-1,
            last_processed_sha="",
            tree_size_cache={},
            processed_commit_shas=set(),
        )
        field_names = set(checkpoint.model_fields.keys())
        assert "file_change_records" not in field_names

    def test_checkpoint_serialization_roundtrip(self):
        """Checkpoint should serialize and deserialize correctly."""
        original = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
            last_updated_at=datetime(2024, 1, 15, 11, 0, 0, tzinfo=UTC),
            commits_total=1000,
            commits_processed=500,
            last_processed_index=499,
            last_processed_sha="abc123def456",
            tree_size_cache={"sha1": (100, 10), "sha2": (200, 20)},
            processed_commit_shas={"sha1", "sha2", "sha3"},
            commit_chunk_count=3,
            file_change_chunk_count=3,
        )

        # Serialize to JSON
        json_str = original.model_dump_json()

        # Deserialize back
        restored = ExtractionCheckpoint.model_validate_json(json_str)

        assert restored.version == original.version
        assert restored.codebase_id == original.codebase_id
        assert restored.commit_chunk_count == original.commit_chunk_count
        assert restored.file_change_chunk_count == original.file_change_chunk_count
        assert restored.processed_commit_shas == original.processed_commit_shas
        assert restored.commits_total == original.commits_total
        assert restored.commits_processed == original.commits_processed

    def test_checkpoint_json_size_without_records(self):
        """Checkpoint JSON should be small without record data."""
        # Create checkpoint with many SHAs but no records
        checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            commits_total=100000,
            commits_processed=50000,
            last_processed_index=49999,
            last_processed_sha="abc123",
            tree_size_cache={f"sha{i}": (i * 100, i * 10) for i in range(1000)},
            processed_commit_shas={f"sha{i}" for i in range(50000)},
            commit_chunk_count=50,
            file_change_chunk_count=50,
        )

        json_str = checkpoint.model_dump_json()
        size_mb = len(json_str) / (1024 * 1024)

        # Should be under 20MB (was 2.4GB with records)
        assert size_mb < 20, f"Checkpoint JSON is {size_mb:.1f}MB, expected <20MB"

    def test_checkpoint_preserves_tree_size_cache(self):
        """Tree size cache should be preserved through serialization."""
        original_cache = {
            "abc123": (1000, 100),
            "def456": (2000, 200),
            "ghi789": (3000, 300),
        }

        checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            commits_total=100,
            commits_processed=50,
            last_processed_index=49,
            last_processed_sha="abc123",
            tree_size_cache=original_cache,
            processed_commit_shas=set(),
        )

        json_str = checkpoint.model_dump_json()
        restored = ExtractionCheckpoint.model_validate_json(json_str)

        # Verify cache is preserved (may be list instead of tuple after JSON)
        for sha, (bytes_val, lines_val) in original_cache.items():
            assert sha in restored.tree_size_cache
            restored_val = restored.tree_size_cache[sha]
            assert restored_val[0] == bytes_val
            assert restored_val[1] == lines_val
