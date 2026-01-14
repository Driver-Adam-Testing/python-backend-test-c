"""Unit tests for Rust checkpoint callback functionality.

TDD: These tests are written BEFORE implementation.
"""

from analytics_native import CommitWithParent, calculate_tree_sizes_incremental


class TestRustCheckpointCallback:
    """Tests for checkpoint callback in Rust native extension."""

    def test_callback_receives_index_and_results(self, test_repo_with_commits):
        """Callback should receive (index, results_dict) on each checkpoint."""
        repo_path, commits = test_repo_with_commits  # Fixture creates 100 commits

        callback_calls = []

        def capture_callback(index, results):
            callback_calls.append((index, dict(results)))

        commits_with_parents = [
            CommitWithParent(sha, parent) for sha, parent in commits
        ]

        calculate_tree_sizes_incremental(
            repo_path,
            commits_with_parents,
            checkpoint_callback=capture_callback,
            checkpoint_interval_secs=0.001,  # Very short interval for testing
        )

        # Should have received at least one callback
        assert len(callback_calls) > 0

        # Each callback should have (index, results_dict)
        for idx, results in callback_calls:
            assert isinstance(idx, int)
            assert isinstance(results, dict)
            assert idx >= 0
            # Results should contain processed commits up to index
            assert len(results) >= idx

    def test_no_callback_when_not_provided(self, test_repo_with_commits):
        """Should work without callback (backwards compatible)."""
        repo_path, commits = test_repo_with_commits

        commits_with_parents = [
            CommitWithParent(sha, parent) for sha, parent in commits
        ]

        # Should not raise, should return results as before
        result = calculate_tree_sizes_incremental(
            repo_path,
            commits_with_parents,
        )

        assert len(result) == len(commits)

    def test_callback_interval_respected(self, test_repo_with_commits):
        """Callbacks should respect the interval setting."""
        repo_path, commits = test_repo_with_commits

        callback_times = []
        import time

        def capture_callback(index, results):
            callback_times.append(time.time())

        commits_with_parents = [
            CommitWithParent(sha, parent) for sha, parent in commits
        ]

        calculate_tree_sizes_incremental(
            repo_path,
            commits_with_parents,
            checkpoint_callback=capture_callback,
            checkpoint_interval_secs=0.1,  # 100ms interval
        )

        # If we got multiple callbacks, they should be ~100ms apart
        if len(callback_times) >= 2:
            for i in range(1, len(callback_times)):
                gap = callback_times[i] - callback_times[i - 1]
                assert gap >= 0.08  # Allow some tolerance


class TestRustResumeFromCheckpoint:
    """Tests for resuming from initial_results."""

    def test_resume_from_initial_results(self, test_repo_with_commits):
        """Should skip already-processed commits when initial_results provided."""
        repo_path, commits = test_repo_with_commits

        commits_with_parents = [
            CommitWithParent(sha, parent) for sha, parent in commits
        ]

        # First run: process all commits, capture checkpoint at midpoint
        midpoint_checkpoint = None

        def capture_midpoint(index, results):
            nonlocal midpoint_checkpoint
            if index >= len(commits) // 2 and midpoint_checkpoint is None:
                midpoint_checkpoint = dict(results)

        full_result = calculate_tree_sizes_incremental(
            repo_path,
            commits_with_parents,
            checkpoint_callback=capture_midpoint,
            checkpoint_interval_secs=0.001,
        )

        assert midpoint_checkpoint is not None

        # Second run: resume from midpoint
        resumed_result = calculate_tree_sizes_incremental(
            repo_path,
            commits_with_parents,
            initial_results=midpoint_checkpoint,
        )

        # Results should be identical
        assert resumed_result == full_result

    def test_initial_results_empty_processes_all(self, test_repo_with_commits):
        """Empty initial_results should process all commits."""
        repo_path, commits = test_repo_with_commits

        commits_with_parents = [
            CommitWithParent(sha, parent) for sha, parent in commits
        ]

        result_no_initial = calculate_tree_sizes_incremental(
            repo_path,
            commits_with_parents,
        )

        result_empty_initial = calculate_tree_sizes_incremental(
            repo_path,
            commits_with_parents,
            initial_results={},
        )

        assert result_no_initial == result_empty_initial

    def test_resume_produces_identical_output(self, test_repo_with_commits):
        """PRIMARY ACCEPTANCE TEST: Resume must produce bit-identical results."""
        repo_path, commits = test_repo_with_commits

        commits_with_parents = [
            CommitWithParent(sha, parent) for sha, parent in commits
        ]

        # Full run
        full_result = calculate_tree_sizes_incremental(
            repo_path,
            commits_with_parents,
        )

        # Simulate resume at various points
        for resume_point in [10, 25, 50, 75, 90]:
            if resume_point >= len(commits):
                continue

            # Create partial results up to resume_point
            partial_results = {
                sha: full_result[sha] for sha, _ in commits[:resume_point]
            }

            resumed_result = calculate_tree_sizes_incremental(
                repo_path,
                commits_with_parents,
                initial_results=partial_results,
            )

            # Must be identical
            assert (
                resumed_result == full_result
            ), f"Mismatch when resuming at {resume_point}"
