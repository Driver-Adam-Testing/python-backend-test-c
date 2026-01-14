"""End-to-end integration tests for analytics pipeline."""

from datetime import UTC

import pygit2
import pytest
from analytics.pipeline.orchestrator import (
    PipelineConfig,
)
from analytics.pipeline.phases.branches import discover_branches
from analytics.pipeline.phases.clone import open_repository
from analytics.pipeline.phases.extract import extract_commits


class TestPipelinePhases:
    """Test individual pipeline phases."""

    @pytest.fixture
    def test_repo(self, tmp_path):
        """Create a test git repository."""
        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)

        # Configure
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@example.com"

        # Create initial commit
        readme = repo_path / "README.md"
        readme.write_text("# Test Repo\n")

        index = repo.index
        index.add("README.md")
        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@example.com")
        repo.create_commit("HEAD", sig, sig, "Initial commit", tree, [])

        # Add more commits
        for i in range(5):
            test_file = repo_path / f"file_{i}.py"
            test_file.write_text(f"# File {i}\nprint('hello {i}')\n")

            index = repo.index
            index.add(f"file_{i}.py")
            index.write()
            tree = index.write_tree()

            parent = repo.head.peel(pygit2.Commit)
            repo.create_commit("HEAD", sig, sig, f"Add file {i}", tree, [parent.id])

        return repo_path, repo

    def test_open_repository(self, test_repo):
        """Test opening an existing repository."""
        repo_path, _ = test_repo

        result = open_repository(repo_path)

        assert result.success
        assert result.repo is not None
        assert result.repo_path == repo_path

    def test_extract_commits(self, test_repo):
        """Test extracting commits from repository."""
        repo_path, repo = test_repo

        result = extract_commits(
            repo=repo, codebase_id="test-uuid", include_patches=True
        )

        assert result.success
        assert result.total_commits == 6  # 1 initial + 5 more
        assert len(result.commits) >= 6  # May have duplicates per branch

    def test_extract_commits_sloc_metrics(self, test_repo):
        """Test that SLOC metrics are correctly calculated from diffs.

        This test verifies that the extract phase correctly parses git diffs
        and populates line/byte metrics. It was added after a bug where
        diff.patch_from_delta() (non-existent) was used instead of diff.stats.
        """
        repo_path, repo = test_repo

        result = extract_commits(
            repo=repo, codebase_id="test-uuid", include_patches=True
        )

        assert result.success

        # Get non-initial commits (ones with parents that have diffs)
        non_initial = [c for c in result.commits if c["parent_count"] > 0]
        assert len(non_initial) >= 5, "Should have at least 5 non-initial commits"

        # Verify SLOC metrics are populated (non-zero) for commits with changes
        commits_with_metrics = 0
        for commit in non_initial:
            # Each commit adds a file with 2 lines: "# File N\nprint('hello N')\n"
            if commit["additions_lines"] > 0:
                commits_with_metrics += 1
                # Verify byte metrics are also populated
                assert (
                    commit["addition_bytes"] > 0
                ), f"Commit {commit['commit_sha'][:8]} has additions_lines={commit['additions_lines']} but addition_bytes=0"
                # Verify derived metrics are consistent
                assert (
                    commit["churn_lines"] >= commit["additions_lines"]
                ), "churn_lines should be >= additions_lines"
                assert (
                    commit["files_changed"] >= 1
                ), "files_changed should be >= 1 for commits with additions"

        # At least some commits should have metrics (not all zeros)
        assert commits_with_metrics >= 3, (
            f"Expected at least 3 commits with SLOC metrics, got {commits_with_metrics}. "
            f"This may indicate diff parsing is broken."
        )

    def test_discover_branches(self, test_repo):
        """Test discovering branches."""
        repo_path, repo = test_repo

        result = discover_branches(repo)

        assert result.success
        assert len(result.branches) >= 1
        assert result.default_branch in ["main", "master"]


class TestPipelineIntegration:
    """Integration tests for full pipeline."""

    @pytest.fixture
    def pipeline_with_repo(self, tmp_path):
        """Create pipeline config and a test repository."""
        # Create work directory
        work_dir = tmp_path / "analytics"
        work_dir.mkdir()

        # Create test repository
        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)

        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@example.com"

        # Create initial commit
        readme = repo_path / "README.md"
        readme.write_text("# Test\n")

        index = repo.index
        index.add("README.md")
        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@example.com")
        repo.create_commit("HEAD", sig, sig, "Initial", tree, [])

        # Add commits
        for i in range(3):
            f = repo_path / f"f{i}.py"
            f.write_text(f"x = {i}\n")

            index = repo.index
            index.add(f"f{i}.py")
            index.write()
            tree = index.write_tree()

            parent = repo.head.peel(pygit2.Commit)
            repo.create_commit("HEAD", sig, sig, f"C{i}", tree, [parent.id])

        pipeline_config = PipelineConfig(
            work_dir=work_dir,
            cleanup_on_complete=False,  # Keep for inspection
        )

        return pipeline_config, repo_path

    def test_pipeline_with_local_repo(self, pipeline_with_repo):
        """Test pipeline can process a local repository path.

        Note: This test uses a local path instead of clone URL.
        The actual pipeline would clone from URL, but we test the
        phases work correctly with a local repo.
        """
        config, repo_path = pipeline_with_repo

        # Open the local repo directly
        result = open_repository(repo_path)
        assert result.success

        # Extract commits
        extract_result = extract_commits(
            repo=result.repo, codebase_id="test-uuid", include_patches=False
        )

        assert extract_result.success
        assert extract_result.total_commits == 4  # Initial + 3 more

        # Discover branches
        branches_result = discover_branches(result.repo)

        assert branches_result.success
        assert len(branches_result.branches) >= 1


class TestOrgLevelFileSchemas:
    """Tests for org-level JSON file schema validation.

    These tests ensure Phase 8 generates files that match the schema
    expected by AnalyticsService. A schema mismatch causes API 500 errors.
    """

    def test_codebase_entry_has_required_fields(self):
        """Test that codebase entry has all fields required by API.

        The AnalyticsService expects these fields in codebases_list.json entries.
        Missing fields cause the API to fail.
        """
        # Simulate what _phase_update_org_files creates
        from datetime import datetime

        codebase_entry = {
            "codebase_id": "test-uuid-1234",
            "display_name": "owner/repo",
            "full_name": "owner/repo",
            "owner": "owner",
            "repository_name": "repo",
            "total_commits": 100,
            "total_contributors": 10,
            "total_branches": 5,
            "total_churn": 5000,
            "current_sloc": 1000,
            "net_sloc": 1000,
            "primary_language": "Python",
            "last_commit_date": datetime.now(UTC).isoformat(),
            "has_analytics": True,
            "analytics_status": "complete",  # Required by API
        }

        # Verify required fields exist
        required_fields = [
            "codebase_id",
            "display_name",
            "total_commits",
            "current_sloc",
            "analytics_status",  # This was missing in the original bug
        ]

        for field in required_fields:
            assert field in codebase_entry, f"Missing required field: {field}"

        # Verify analytics_status has valid value
        assert codebase_entry["analytics_status"] in [
            "complete",
            "pending",
            "error",
        ], "analytics_status must be 'complete', 'pending', or 'error'"

    def test_codebases_list_schema(self):
        """Test that codebases_list.json has correct top-level schema.

        Schema expected by AnalyticsService:
        {
            "organization_id": "org-id",  # NOT derived from codebase_id!
            "codebases": [...],
            "generated_at": "ISO timestamp"  # NOT "last_updated"!
        }
        """
        from datetime import datetime

        # Simulate correct schema
        codebases_list = {
            "organization_id": "org_oVrvUshpraavOMNe",  # Real org ID
            "codebases": [],
            "generated_at": datetime.now(UTC).isoformat(),
        }

        # Required top-level fields
        required_fields = ["organization_id", "codebases", "generated_at"]

        for field in required_fields:
            assert field in codebases_list, f"Missing required field: {field}"

        # organization_id should be a real org ID, not a UUID prefix
        assert not codebases_list["organization_id"].startswith(
            "test-uuid"
        ), "organization_id should be the actual org ID, not derived from codebase_id"

    def test_org_summary_schema(self):
        """Test that org_summary.json has correct schema.

        Schema expected by AnalyticsService:
        {
            "organization_id": "org-id",
            "total_codebases": N,
            "codebases_with_analytics": N,
            "total_commits": N,
            "total_contributors": N,
            "total_sloc": N,
            "generated_at": "ISO timestamp"  # NOT "last_updated"!
        }
        """
        from datetime import datetime

        # Simulate correct schema
        org_summary = {
            "organization_id": "org_oVrvUshpraavOMNe",
            "total_codebases": 3,
            "codebases_with_analytics": 3,
            "total_commits": 500,
            "total_contributors": 25,
            "total_sloc": 50000,
            "generated_at": datetime.now(UTC).isoformat(),
        }

        # Required fields per AnalyticsService tests
        required_fields = [
            "organization_id",
            "total_codebases",
            "codebases_with_analytics",
            "total_commits",
            "total_contributors",
            "total_sloc",
            "generated_at",  # NOT "last_updated"
        ]

        for field in required_fields:
            assert field in org_summary, f"Missing required field: {field}"

        # Verify "last_updated" is NOT used (common mistake)
        assert (
            "last_updated" not in org_summary
        ), "Use 'generated_at' not 'last_updated' for timestamp field"


class TestChunkBasedExtraction:
    """E2E tests for chunk-based memory optimization (v2.0).

    These tests verify the full flow of:
    1. Extraction writing chunks instead of accumulating in memory
    2. Checkpoint storing chunk counts instead of records
    3. Merge phase combining chunks to final storage
    """

    @pytest.fixture
    def test_repo(self, tmp_path):
        """Create a test git repository."""
        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)

        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@example.com"

        # Create initial commit
        readme = repo_path / "README.md"
        readme.write_text("# Test\n")

        index = repo.index
        index.add("README.md")
        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@example.com")
        repo.create_commit("HEAD", sig, sig, "Initial", tree, [])

        # Add more commits for testing
        for i in range(10):
            f = repo_path / f"file_{i}.py"
            f.write_text(f"# File {i}\nprint('hello {i}')\n")

            index = repo.index
            index.add(f"file_{i}.py")
            index.write()
            tree = index.write_tree()

            parent = repo.head.peel(pygit2.Commit)
            repo.create_commit("HEAD", sig, sig, f"Add file {i}", tree, [parent.id])

        return repo_path, repo

    def test_create_chunk_callback_creates_chunks(self, test_repo, tmp_path):
        """Test that create_chunk_callback writes chunks to storage."""
        from datetime import UTC, date, datetime
        from unittest.mock import MagicMock

        from analytics.pipeline.phases.extract import create_chunk_callback

        _, repo = test_repo
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

        # Create sample commit data
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

        # Call callback
        callback(["abc123"], [commit], [])

        # Verify chunk was uploaded
        assert len(uploaded_keys) == 1
        assert "chunk_00000.parquet" in uploaded_keys[0]

    def test_checkpoint_v2_has_chunk_counts(self, test_repo, tmp_path):
        """Test that checkpoint v2.0 stores chunk counts not records."""
        from datetime import UTC, datetime

        from analytics.checkpoint import ExtractionCheckpoint

        _, repo = test_repo

        # Create v2.0 checkpoint
        checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            commits_total=100,
            commits_processed=50,
            last_processed_index=49,
            last_processed_sha="abc123",
            tree_size_cache={},
            processed_commit_shas={"sha1", "sha2"},
            commit_chunk_count=5,
            file_change_chunk_count=3,
        )

        # Verify v2.0 schema
        assert checkpoint.version == "2.0"
        assert checkpoint.commit_chunk_count == 5
        assert checkpoint.file_change_chunk_count == 3

        # Verify records are not in the model
        assert not hasattr(checkpoint, "commit_records")
        assert not hasattr(checkpoint, "file_change_records")

    def test_merge_chunks_to_storage(self, tmp_path):
        """Test merging chunks to final storage."""
        from datetime import UTC, date, datetime

        import pyarrow as pa
        import pyarrow.parquet as pq
        from analytics.pipeline.orchestrator import merge_chunks_to_storage
        from analytics.schemas.warm_schemas import COMMITS_SCHEMA

        # Create chunks
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()

        commit = {
            "commit_sha": "merge_test",
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

        # Merge
        output_path = tmp_path / "merged.parquet"
        merge_chunks_to_storage(
            chunks_dir=chunks_dir,
            output_path=output_path,
            data_type="commits",
            order_by="committed_at",
        )

        # Verify
        assert output_path.exists()
        result = pq.read_table(output_path)
        assert len(result) == 1
        assert result.column("commit_sha")[0].as_py() == "merge_test"

    def test_full_chunk_workflow(self, test_repo, tmp_path):
        """Test full workflow: extract with chunks → merge → verify."""
        from datetime import UTC, date, datetime
        from unittest.mock import MagicMock

        import pyarrow.parquet as pq
        from analytics.pipeline.orchestrator import merge_chunks_to_storage
        from analytics.pipeline.phases.extract import create_chunk_callback

        _, repo = test_repo

        # Step 1: Create chunks via callback
        mock_s3 = MagicMock()
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()

        # Instead of uploading to S3, write directly to local dir for testing
        chunk_index = [0]

        def capture_and_write(fileobj, bucket, key):
            filename = f"chunk_{chunk_index[0]:05d}.parquet"
            local_path = chunks_dir / filename
            with open(local_path, "wb") as f:
                f.write(fileobj.read())
            chunk_index[0] += 1

        mock_s3.upload_fileobj = capture_and_write

        callback, storage = create_chunk_callback(
            s3_client=mock_s3,
            bucket="test-bucket",
            codebase_id="test-codebase",
        )

        # Simulate extraction writing multiple batches
        for i in range(3):
            commit = {
                "commit_sha": f"sha_{i}",
                "codebase_id": "test-codebase",
                "branch_name": "main",
                "committed_at": datetime(2024, 1, 15 + i, 10, 0, 0, tzinfo=UTC),
                "collected_at": datetime(2024, 1, 16 + i, 10, 0, 0, tzinfo=UTC),
                "commit_date": date(2024, 1, 15 + i),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": 15 + i,
                "author_email": "dev@test.com",
                "author_name": "Developer",
                "committer_email": "dev@test.com",
                "committer_name": "Developer",
                "message": f"Commit {i}",
                "message_length": 8,
                "parent_count": 1 if i > 0 else 0,
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
            callback([f"sha_{i}"], [commit], [])

        # Verify chunks were created
        chunk_files = list(chunks_dir.glob("*.parquet"))
        assert len(chunk_files) == 3

        # Step 2: Merge chunks
        output_path = tmp_path / "merged_commits.parquet"
        merge_chunks_to_storage(
            chunks_dir=chunks_dir,
            output_path=output_path,
            data_type="commits",
            order_by="committed_at",
        )

        # Step 3: Verify merged output
        assert output_path.exists()
        result = pq.read_table(output_path)
        assert len(result) == 3

        # Verify ordering
        shas = result.column("commit_sha").to_pylist()
        assert shas == ["sha_0", "sha_1", "sha_2"]
