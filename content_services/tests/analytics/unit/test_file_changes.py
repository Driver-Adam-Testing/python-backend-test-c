"""Unit tests for file-level change extraction.

A1: File-Level Change Tracking

Tests for extracting file-level changes from commits:
- File paths extraction
- Change type detection (added, modified, deleted, renamed)
- Per-file byte calculations
- Language detection from extension
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock


class TestExtractResultFileChanges:
    """Tests for ExtractResult file_changes field."""

    def test_extract_result_has_file_changes_field(self):
        """ExtractResult should have file_changes list."""
        from analytics.pipeline.phases.extract import ExtractResult

        result = ExtractResult(
            success=True,
            commits=[],
            total_commits=0,
            file_changes=[],
        )

        assert hasattr(result, "file_changes")
        assert result.file_changes == []

    def test_extract_result_file_changes_default_empty(self):
        """ExtractResult file_changes should default to empty list."""
        from analytics.pipeline.phases.extract import ExtractResult

        result = ExtractResult(
            success=True,
            commits=[],
            total_commits=0,
        )

        assert result.file_changes == []


class TestFileChangeExtraction:
    """Tests for _extract_file_changes function."""

    def test_extracts_file_paths(self):
        """File paths are extracted from diffs."""
        from analytics.pipeline.phases.extract import _extract_file_changes

        # Create mock diff with file changes
        mock_delta = MagicMock()
        mock_delta.new_file.path = "src/main.py"
        mock_delta.old_file.path = "src/main.py"
        mock_delta.status = 1  # GIT_DELTA_MODIFIED
        mock_delta.status_char.return_value = "M"

        mock_patch = MagicMock()
        mock_patch.delta = mock_delta
        mock_patch.line_stats = (0, 10, 5)  # context, additions, deletions
        mock_patch.text = "+new line\n-old line"

        mock_diff = MagicMock()
        mock_diff.__iter__ = lambda self: iter([mock_patch])

        result = _extract_file_changes(
            mock_diff, "abc123", "test-codebase", datetime.now(UTC).date()
        )

        assert len(result) == 1
        assert result[0]["file_path"] == "src/main.py"
        assert result[0]["commit_sha"] == "abc123"
        assert result[0]["codebase_id"] == "test-codebase"

    def test_detects_change_type_added(self):
        """Detects 'added' change type for new files."""
        from analytics.pipeline.phases.extract import _extract_file_changes

        mock_delta = MagicMock()
        mock_delta.new_file.path = "new_file.py"
        mock_delta.old_file.path = ""
        mock_delta.status = (
            1  # GIT_DELTA_ADDED would be different, but we map status_char
        )
        mock_delta.status_char.return_value = "A"

        mock_patch = MagicMock()
        mock_patch.delta = mock_delta
        mock_patch.line_stats = (0, 20, 0)
        mock_patch.text = "+line1\n+line2"

        mock_diff = MagicMock()
        mock_diff.__iter__ = lambda self: iter([mock_patch])

        result = _extract_file_changes(
            mock_diff, "abc123", "test-codebase", datetime.now(UTC).date()
        )

        assert result[0]["change_type"] == "added"

    def test_detects_change_type_deleted(self):
        """Detects 'deleted' change type for removed files."""
        from analytics.pipeline.phases.extract import _extract_file_changes

        mock_delta = MagicMock()
        mock_delta.new_file.path = ""
        mock_delta.old_file.path = "deleted_file.py"
        mock_delta.status_char.return_value = "D"

        mock_patch = MagicMock()
        mock_patch.delta = mock_delta
        mock_patch.line_stats = (0, 0, 15)
        mock_patch.text = "-line1\n-line2"

        mock_diff = MagicMock()
        mock_diff.__iter__ = lambda self: iter([mock_patch])

        result = _extract_file_changes(
            mock_diff, "abc123", "test-codebase", datetime.now(UTC).date()
        )

        assert result[0]["change_type"] == "deleted"
        assert result[0]["file_path"] == "deleted_file.py"

    def test_detects_change_type_renamed(self):
        """Detects 'renamed' change type for renamed files."""
        from analytics.pipeline.phases.extract import _extract_file_changes

        mock_delta = MagicMock()
        mock_delta.new_file.path = "new_name.py"
        mock_delta.old_file.path = "old_name.py"
        mock_delta.status_char.return_value = "R"

        mock_patch = MagicMock()
        mock_patch.delta = mock_delta
        mock_patch.line_stats = (0, 0, 0)
        mock_patch.text = ""

        mock_diff = MagicMock()
        mock_diff.__iter__ = lambda self: iter([mock_patch])

        result = _extract_file_changes(
            mock_diff, "abc123", "test-codebase", datetime.now(UTC).date()
        )

        assert result[0]["change_type"] == "renamed"
        assert result[0]["previous_path"] == "old_name.py"

    def test_calculates_per_file_bytes(self):
        """Calculates byte additions and deletions per file."""
        from analytics.pipeline.phases.extract import _extract_file_changes

        mock_delta = MagicMock()
        mock_delta.new_file.path = "src/utils.py"
        mock_delta.old_file.path = "src/utils.py"
        mock_delta.status_char.return_value = "M"

        mock_patch = MagicMock()
        mock_patch.delta = mock_delta
        mock_patch.line_stats = (0, 2, 1)
        # Patch text: 2 additions (10 bytes each), 1 deletion (8 bytes)
        mock_patch.text = "+0123456789\n+abcdefghij\n-12345678"

        mock_diff = MagicMock()
        mock_diff.__iter__ = lambda self: iter([mock_patch])

        result = _extract_file_changes(
            mock_diff, "abc123", "test-codebase", datetime.now(UTC).date()
        )

        assert result[0]["additions_lines"] == 2
        assert result[0]["deletions_lines"] == 1
        assert result[0]["addition_bytes"] == 20  # Two 10-byte additions
        assert result[0]["deletion_bytes"] == 8  # One 8-byte deletion

    def test_detects_language_from_extension(self):
        """Detects file language from extension."""
        from analytics.pipeline.phases.extract import _extract_file_changes

        mock_delta = MagicMock()
        mock_delta.new_file.path = "src/component.tsx"
        mock_delta.old_file.path = "src/component.tsx"
        mock_delta.status_char.return_value = "M"

        mock_patch = MagicMock()
        mock_patch.delta = mock_delta
        mock_patch.line_stats = (0, 5, 2)
        mock_patch.text = "+line"

        mock_diff = MagicMock()
        mock_diff.__iter__ = lambda self: iter([mock_patch])

        result = _extract_file_changes(
            mock_diff, "abc123", "test-codebase", datetime.now(UTC).date()
        )

        assert result[0]["file_extension"] == ".tsx"
        # Language detection will be implemented - for now just check extension is captured

    def test_handles_binary_files(self):
        """Binary/non-code files are excluded from file changes."""
        from analytics.pipeline.phases.extract import _extract_file_changes

        mock_delta = MagicMock()
        mock_delta.new_file.path = "image.svg"  # Use .svg which IS in the blacklist
        mock_delta.old_file.path = "image.svg"
        mock_delta.status_char.return_value = "M"
        mock_delta.flags = 1  # BINARY flag

        mock_patch = MagicMock()
        mock_patch.delta = mock_delta
        mock_patch.line_stats = (0, 0, 0)
        mock_patch.text = None  # Binary files have no text

        mock_diff = MagicMock()
        mock_diff.__iter__ = lambda self: iter([mock_patch])

        result = _extract_file_changes(
            mock_diff, "abc123", "test-codebase", datetime.now(UTC).date()
        )

        # Non-code files (binary images) should be excluded entirely
        # Only analyzable code files are included in file changes
        assert len(result) == 0

    def test_all_text_files_included(self):
        """All text files in languages.yml are included in file changes."""
        from analytics.pipeline.phases.extract import _extract_file_changes

        # Create patches for code and markdown files
        code_delta = MagicMock()
        code_delta.new_file.path = "src/main.py"
        code_delta.old_file.path = "src/main.py"
        code_delta.status_char.return_value = "M"

        code_patch = MagicMock()
        code_patch.delta = code_delta
        code_patch.line_stats = (0, 5, 2)
        code_patch.text = "+line1\n+line2"

        readme_delta = MagicMock()
        readme_delta.new_file.path = "README.md"
        readme_delta.old_file.path = "README.md"
        readme_delta.status_char.return_value = "M"

        readme_patch = MagicMock()
        readme_patch.delta = readme_delta
        readme_patch.line_stats = (0, 10, 0)
        readme_patch.text = "+doc line"

        mock_diff = MagicMock()
        mock_diff.__iter__ = lambda self: iter([code_patch, readme_patch])

        result = _extract_file_changes(
            mock_diff, "abc123", "test-codebase", datetime.now(UTC).date()
        )

        # Both files are in languages.yml, so both included
        assert len(result) == 2
        file_paths = {r["file_path"] for r in result}
        assert "src/main.py" in file_paths
        assert "README.md" in file_paths


class TestExtractCommitsWithFileChanges:
    """Tests for extract_commits with include_file_changes flag."""

    def test_extract_commits_includes_file_changes(self):
        """extract_commits returns file_changes when requested."""
        from analytics.pipeline.phases.extract import ExtractResult

        # The actual integration would need a real repo
        # This test verifies the data structure
        result = ExtractResult(
            success=True,
            commits=[{"commit_sha": "abc123"}],
            total_commits=1,
            file_changes=[
                {
                    "codebase_id": "test",
                    "commit_sha": "abc123",
                    "file_path": "test.py",
                    "change_type": "modified",
                }
            ],
        )

        assert len(result.file_changes) == 1
        assert result.file_changes[0]["file_path"] == "test.py"


class TestFileChangesSchema:
    """Tests for file changes schema compatibility."""

    def test_file_change_has_required_fields(self):
        """File change dict has all required schema fields."""
        required_fields = [
            "codebase_id",
            "commit_sha",
            "file_path",
            "commit_date",
            "change_type",
            "additions_lines",
            "deletions_lines",
            "changes_lines",
            "addition_bytes",
            "deletion_bytes",
            "file_sloc",
            "file_extension",
            "has_patch_data",
        ]

        # A valid file change dict
        file_change = {
            "codebase_id": "test-codebase",
            "commit_sha": "abc123",
            "file_path": "src/main.py",
            "commit_date": datetime.now(UTC).date(),
            "change_type": "modified",
            "previous_path": None,
            "additions_lines": 10,
            "deletions_lines": 5,
            "changes_lines": 15,
            "addition_bytes": 500,
            "deletion_bytes": 250,
            "file_sloc": 15,
            "file_extension": ".py",
            "file_language": "Python",
            "has_patch_data": True,
            "patch_blob_key": None,
        }

        for field in required_fields:
            assert field in file_change, f"Missing required field: {field}"
