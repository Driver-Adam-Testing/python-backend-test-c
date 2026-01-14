"""Integration tests for extraction checkpoint/resume.

TDD: These tests are written BEFORE implementation.

These tests verify that:
1. Extraction can be interrupted and resumed
2. Resumed extraction produces identical results to full extraction
3. Checkpoint callback is called at appropriate intervals
"""

from datetime import UTC, datetime

import pygit2
import pytest


class TestExtractCheckpointIntegration:
    """Integration tests for checkpoint during extraction."""

    @pytest.fixture
    def test_repo_with_history(self, temp_dir):
        """Create a test repo with diverse commit history."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

        # Create 50 commits with various file changes
        for i in range(50):
            # Vary file operations
            if i % 3 == 0:
                # Add new file
                (repo_path / f"module_{i}.py").write_text(f"x = {i}\n" * (i % 5 + 1))
            elif i % 3 == 1:
                # Modify existing file
                existing = list(repo_path.glob("module_*.py"))
                if existing:
                    existing[0].write_text(f"modified = {i}\n" * 3)
            else:
                # Add to subdirectory
                subdir = repo_path / "src"
                subdir.mkdir(exist_ok=True)
                (subdir / f"util_{i}.py").write_text(f"util = {i}\n")

            index = repo.index
            index.add_all()
            index.write()
            tree = index.write_tree()

            sig = pygit2.Signature("Test", "test@test.com")
            parents = [repo.head.target] if not repo.head_is_unborn else []
            repo.create_commit("HEAD", sig, sig, f"Commit {i}", tree, parents)

        return repo_path, repo

    def test_extract_with_checkpoint_callback(self, test_repo_with_history):
        """Checkpoint callback should be called during extraction."""
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = test_repo_with_history

        callback_calls = []

        def capture_callback(checkpoint_data):
            callback_calls.append(checkpoint_data)

        extract_commits(
            repo=repo,
            codebase_id="test-codebase",
            checkpoint_callback=capture_callback,
            checkpoint_interval_secs=0.001,  # Very short for testing
        )

        # Should have received callbacks
        assert len(callback_calls) > 0

        # Each callback should have expected fields
        for data in callback_calls:
            assert "last_processed_index" in data
            assert "tree_size_cache" in data
            assert "codebase_id" in data

    def test_extract_resume_produces_identical_results(self, test_repo_with_history):
        """PRIMARY ACCEPTANCE TEST: Resume must produce identical results."""
        from analytics.checkpoint import ExtractionCheckpoint
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = test_repo_with_history

        # Step 1: Full extraction (baseline)
        full_result = extract_commits(
            repo=repo,
            codebase_id="test-codebase",
        )

        # Step 2: Extraction with simulated interrupt at midpoint
        midpoint_checkpoint = None
        call_count = [0]

        def capture_midpoint(checkpoint_data):
            call_count[0] += 1
            nonlocal midpoint_checkpoint
            # Capture checkpoint around the midpoint
            if (
                checkpoint_data["commits_processed"] >= 25
                and midpoint_checkpoint is None
            ):
                midpoint_checkpoint = ExtractionCheckpoint(
                    codebase_id="test-codebase",
                    started_at=datetime.now(UTC),
                    commits_total=checkpoint_data["commits_total"],
                    commits_processed=checkpoint_data["commits_processed"],
                    last_processed_index=checkpoint_data["last_processed_index"],
                    last_processed_sha=checkpoint_data["last_processed_sha"],
                    tree_size_cache=checkpoint_data["tree_size_cache"],
                )

        # Run to capture midpoint
        extract_commits(
            repo=repo,
            codebase_id="test-codebase",
            checkpoint_callback=capture_midpoint,
            checkpoint_interval_secs=0.001,
        )

        assert (
            midpoint_checkpoint is not None
        ), "Should have captured midpoint checkpoint"

        # Step 3: Resume from midpoint
        resumed_result = extract_commits(
            repo=repo,
            codebase_id="test-codebase",
            checkpoint=midpoint_checkpoint,
        )

        # Step 4: Verify identical results
        assert len(resumed_result.commits) == len(full_result.commits)

        for i, (resumed_commit, full_commit) in enumerate(
            zip(resumed_result.commits, full_result.commits)
        ):
            assert (
                resumed_commit["commit_sha"] == full_commit["commit_sha"]
            ), f"SHA mismatch at {i}"
            assert resumed_commit["tree_sloc"] == full_commit["tree_sloc"], (
                f"tree_sloc mismatch at {i}: "
                f"resumed={resumed_commit['tree_sloc']} vs full={full_commit['tree_sloc']}"
            )
            assert (
                resumed_commit["tree_bytes"] == full_commit["tree_bytes"]
            ), f"tree_bytes mismatch at {i}"

    def test_extract_from_empty_checkpoint_equals_full(self, test_repo_with_history):
        """Extraction with empty checkpoint should equal full extraction."""
        from analytics.checkpoint import ExtractionCheckpoint
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = test_repo_with_history

        # Full extraction
        full_result = extract_commits(
            repo=repo,
            codebase_id="test-codebase",
        )

        # With empty checkpoint (nothing processed yet)
        empty_checkpoint = ExtractionCheckpoint(
            codebase_id="test-codebase",
            started_at=datetime.now(UTC),
            commits_total=0,  # Will be updated
            commits_processed=0,
            last_processed_index=-1,
            last_processed_sha="",
            tree_size_cache={},
        )

        resumed_result = extract_commits(
            repo=repo,
            codebase_id="test-codebase",
            checkpoint=empty_checkpoint,
        )

        assert len(resumed_result.commits) == len(full_result.commits)

    def test_checkpoint_contains_tree_size_cache(self, test_repo_with_history):
        """Checkpoint callback should include tree_size_cache from Rust."""
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = test_repo_with_history

        last_checkpoint = None

        def capture_callback(checkpoint_data):
            nonlocal last_checkpoint
            last_checkpoint = checkpoint_data

        extract_commits(
            repo=repo,
            codebase_id="test-codebase",
            checkpoint_callback=capture_callback,
            checkpoint_interval_secs=0.001,
        )

        assert last_checkpoint is not None
        assert "tree_size_cache" in last_checkpoint
        assert isinstance(last_checkpoint["tree_size_cache"], dict)

        # Should have entries for processed commits
        assert len(last_checkpoint["tree_size_cache"]) > 0

        # Each entry should be (bytes, lines)
        for sha, (bytes_count, lines_count) in last_checkpoint[
            "tree_size_cache"
        ].items():
            assert isinstance(sha, str)
            assert isinstance(bytes_count, int)
            assert isinstance(lines_count, int)


class TestResumeAtVariousPoints:
    """Test resume at different points in the extraction process."""

    @pytest.fixture
    def test_repo_100_commits(self, temp_dir):
        """Create repo with exactly 100 commits."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

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

    @pytest.mark.parametrize("resume_percentage", [10, 25, 50, 75, 90])
    def test_resume_at_percentage(self, test_repo_100_commits, resume_percentage):
        """Resume at various percentages should produce identical results."""
        from analytics.checkpoint import ExtractionCheckpoint
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = test_repo_100_commits
        resume_at = resume_percentage  # Out of 100 commits

        # Full run
        full_result = extract_commits(repo=repo, codebase_id="test")

        # Capture checkpoint at target percentage
        target_checkpoint = None

        def capture_at_target(checkpoint_data):
            nonlocal target_checkpoint
            if (
                checkpoint_data["commits_processed"] >= resume_at
                and target_checkpoint is None
            ):
                target_checkpoint = ExtractionCheckpoint(
                    codebase_id="test",
                    started_at=datetime.now(UTC),
                    commits_total=checkpoint_data["commits_total"],
                    commits_processed=checkpoint_data["commits_processed"],
                    last_processed_index=checkpoint_data["last_processed_index"],
                    last_processed_sha=checkpoint_data["last_processed_sha"],
                    tree_size_cache=checkpoint_data["tree_size_cache"],
                )

        extract_commits(
            repo=repo,
            codebase_id="test",
            checkpoint_callback=capture_at_target,
            checkpoint_interval_secs=0.001,
        )

        assert target_checkpoint is not None

        # Resume
        resumed_result = extract_commits(
            repo=repo,
            codebase_id="test",
            checkpoint=target_checkpoint,
        )

        # Verify identical
        assert len(resumed_result.commits) == len(full_result.commits)
        for rc, fc in zip(resumed_result.commits, full_result.commits):
            assert (
                rc["tree_sloc"] == fc["tree_sloc"]
            ), f"Mismatch at {resume_percentage}%"


class TestCheckpointNoRegression:
    """Verify checkpoint feature doesn't break existing functionality."""

    @pytest.fixture
    def test_repo_with_history(self, temp_dir):
        """Create a test repo with diverse commit history."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

        # Create 50 commits
        for i in range(50):
            (repo_path / f"file_{i}.py").write_text(f"x = {i}\n")
            index = repo.index
            index.add_all()
            index.write()
            tree = index.write_tree()

            sig = pygit2.Signature("Test", "test@test.com")
            parents = [repo.head.target] if not repo.head_is_unborn else []
            repo.create_commit("HEAD", sig, sig, f"Commit {i}", tree, parents)

        return repo_path, repo

    def test_extract_without_checkpoint_unchanged(self, test_repo_with_history):
        """Extraction without checkpoint params should work as before."""
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = test_repo_with_history

        # Call without any checkpoint params (backwards compatible)
        result = extract_commits(
            repo=repo,
            codebase_id="test-codebase",
        )

        assert result is not None
        assert len(result.commits) == 50  # Our fixture has 50 commits
        assert all("tree_sloc" in c for c in result.commits)
