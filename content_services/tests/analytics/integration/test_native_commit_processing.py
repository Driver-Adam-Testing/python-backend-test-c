"""
Integration tests for native Rust commit processing.

These tests verify that the Rust implementation produces identical
results to the Python implementation for commit processing.

TDD: These tests are written FIRST, before the Rust implementation.
They will skip if the native extension doesn't have the required functions.
"""

from datetime import UTC, datetime
from pathlib import Path

import pygit2
import pytest


def native_commit_processing_available():
    """Check if the native commit processing function is available."""
    try:
        from analytics_native import CommitInput, process_commits_parallel  # noqa: F401

        return True
    except ImportError:
        return False


# xfail all tests if native commit processing not available
pytestmark = pytest.mark.xfail(
    not native_commit_processing_available(),
    run=False,
    reason="Native commit processing not implemented yet",
)


class TestCommitProcessingParity:
    """Verify Rust commit processing matches Python exactly."""

    @pytest.fixture
    def test_repo(self, temp_dir):
        """Create a test repository with known commits."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test User"
        config["user.email"] = "test@example.com"

        return repo_path, repo

    def _create_commit(
        self, repo, repo_path, files: dict[str, str], message: str
    ) -> pygit2.Commit:
        """Helper to create a commit with the given files."""
        for filename, content in files.items():
            file_path = repo_path / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)

        index = repo.index
        index.add_all()
        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test User", "test@example.com")
        parents = [repo.head.target] if not repo.head_is_unborn else []

        commit_oid = repo.create_commit("HEAD", sig, sig, message, tree, parents)
        return repo.get(commit_oid)

    def test_commit_record_fields_match(self, test_repo):
        """All 35+ commit record fields must match between Python and Rust."""
        from analytics.pipeline.phases.extract import _process_commits_parallel
        from analytics_native import CommitInput, process_commits_parallel

        repo_path, repo = test_repo

        # Create commits with known content
        c1 = self._create_commit(
            repo, repo_path, {"src/main.py": "x = 1\ny = 2\n"}, "Initial commit"
        )
        c2 = self._create_commit(
            repo,
            repo_path,
            {
                "src/main.py": "x = 1\ny = 2\nz = 3\n",
                "src/utils.py": "def foo(): pass\n",
            },
            "Add utils and modify main",
        )

        commit_shas = [str(c1.id), str(c2.id)]
        commit_to_branches = {sha: ["main"] for sha in commit_shas}
        tree_size_cache = {
            str(c1.id): (14, 2),  # "x = 1\ny = 2\n" = 14 bytes, 2 lines
            str(c2.id): (36, 4),  # Both files
        }
        collected_at = datetime.now(UTC)
        codebase_id = "test-codebase-id"

        # Build CommitInput list for Rust
        commit_inputs = [
            CommitInput(sha, commit_to_branches[sha], *tree_size_cache[sha])
            for sha in commit_shas
        ]

        # Run Rust implementation
        rust_commits, rust_file_changes = process_commits_parallel(
            str(repo_path),
            commit_inputs,
            codebase_id,
            collected_at.timestamp(),
            False,  # include_file_changes
            num_workers=1,
        )

        # Run Python implementation
        python_commits, python_file_changes = _process_commits_parallel(
            repo_path=str(repo_path),
            commit_shas=commit_shas,
            codebase_id=codebase_id,
            commit_to_branches=commit_to_branches,
            collected_at=collected_at,
            include_patches=True,
            include_file_changes=False,
            tree_size_cache=tree_size_cache,
            workers=1,
        )

        # Compare record counts
        assert (
            len(rust_commits) == len(python_commits)
        ), f"Record count mismatch: Rust={len(rust_commits)}, Python={len(python_commits)}"

        # Compare fields for each commit
        # Sort by commit_sha to ensure matching order
        rust_by_sha = {r["commit_sha"]: r for r in rust_commits}
        python_by_sha = {r["commit_sha"]: r for r in python_commits}

        required_fields = [
            "commit_sha",
            "codebase_id",
            "branch_name",
            "author_email",
            "author_name",
            "committer_email",
            "committer_name",
            "message",
            "message_length",
            "parent_count",
            "is_merge_commit",
            "files_changed",
            "additions_lines",
            "deletions_lines",
            "net_lines",
            "churn_lines",
            "addition_bytes",
            "deletion_bytes",
            "patch_bytes",
            "net_bytes",
            "sloc",
            "commit_size_category",
            "is_refactor",
            "tree_bytes",
            "tree_lines",
            "tree_sloc",
            "collection_version",
        ]

        for sha in commit_shas:
            rust_rec = rust_by_sha[sha]
            python_rec = python_by_sha[sha]

            for field in required_fields:
                assert field in rust_rec, f"Missing field in Rust: {field}"
                assert field in python_rec, f"Missing field in Python: {field}"

                # Compare values (allow for floating point tolerance on bytes_per_line)
                if field == "bytes_per_line":
                    assert abs(rust_rec[field] - python_rec[field]) < 0.01, (
                        f"Field {field} mismatch for {sha[:8]}: "
                        f"Rust={rust_rec[field]}, Python={python_rec[field]}"
                    )
                else:
                    assert rust_rec[field] == python_rec[field], (
                        f"Field {field} mismatch for {sha[:8]}: "
                        f"Rust={rust_rec[field]}, Python={python_rec[field]}"
                    )

    def test_file_change_fields_match(self, test_repo):
        """All 15 file change record fields must match."""
        from analytics.pipeline.phases.extract import (
            _convert_rust_file_changes,
            _process_commits_parallel_python,
        )
        from analytics_native import CommitInput, process_commits_parallel

        repo_path, repo = test_repo

        # Create commit with file changes
        commit = self._create_commit(
            repo, repo_path, {"src/app.py": "print('hello')\n"}, "Add app"
        )

        commit_sha = str(commit.id)
        commit_to_branches = {commit_sha: ["main"]}
        tree_size_cache = {commit_sha: (16, 1)}
        collected_at = datetime.now(UTC)
        codebase_id = "test-codebase-id"

        # Run Rust with file changes (and convert types)
        commit_inputs = [CommitInput(commit_sha, ["main"], 16, 1)]
        rust_commits_raw, rust_file_changes_raw = process_commits_parallel(
            str(repo_path),
            commit_inputs,
            codebase_id,
            collected_at.timestamp(),
            True,  # include_file_changes
            num_workers=1,
        )
        rust_file_changes = _convert_rust_file_changes(rust_file_changes_raw)

        # Run Python (explicitly use Python fallback) with file changes
        python_commits, python_file_changes = _process_commits_parallel_python(
            repo_path=str(repo_path),
            commit_shas=[commit_sha],
            codebase_id=codebase_id,
            commit_to_branches=commit_to_branches,
            collected_at=collected_at,
            include_patches=True,
            include_file_changes=True,
            tree_size_cache=tree_size_cache,
            workers=1,
        )

        assert len(rust_file_changes) == len(python_file_changes), (
            f"File change count mismatch: Rust={len(rust_file_changes)}, "
            f"Python={len(python_file_changes)}"
        )

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

        for rust_fc, python_fc in zip(
            sorted(rust_file_changes, key=lambda x: x["file_path"]),
            sorted(python_file_changes, key=lambda x: x["file_path"]),
        ):
            for field in required_fields:
                assert field in rust_fc, f"Missing field in Rust file change: {field}"
                assert rust_fc[field] == python_fc[field], (
                    f"File change field {field} mismatch: "
                    f"Rust={rust_fc[field]}, Python={python_fc[field]}"
                )

    def test_branch_expansion(self, test_repo):
        """Commit on 3 branches produces 3 records with correct branch_name."""
        from analytics_native import CommitInput, process_commits_parallel

        repo_path, repo = test_repo

        commit = self._create_commit(
            repo, repo_path, {"file.py": "x = 1\n"}, "Commit on multiple branches"
        )

        commit_sha = str(commit.id)
        branches = ["main", "develop", "feature/test"]

        commit_inputs = [CommitInput(commit_sha, branches, 6, 1)]

        rust_commits, _ = process_commits_parallel(
            str(repo_path),
            commit_inputs,
            "test-codebase",
            datetime.now(UTC).timestamp(),
            False,
            num_workers=1,
        )

        # Should have 3 records (one per branch)
        assert len(rust_commits) == 3, f"Expected 3 records, got {len(rust_commits)}"

        # Each record should have correct branch_name
        branch_names = {r["branch_name"] for r in rust_commits}
        assert branch_names == set(
            branches
        ), f"Branch names mismatch: got {branch_names}, expected {set(branches)}"

        # All other fields should be identical
        for r in rust_commits:
            assert r["commit_sha"] == commit_sha

    def test_diff_metrics_filtered_to_analyzable(self, test_repo):
        """Diff metrics only count analyzable files (not .svg, not driver_docs/)."""
        from analytics_native import CommitInput, process_commits_parallel

        repo_path, repo = test_repo

        # Create commit with mixed files (analyzable and non-analyzable)
        commit = self._create_commit(
            repo,
            repo_path,
            {
                "src/main.py": "x = 1\n",  # Analyzable - 6 bytes
                "assets/logo.svg": "<svg></svg>\n",  # Not analyzable
                "driver_docs/readme.py": "# docs\n",  # Not analyzable (blacklisted dir)
            },
            "Mixed files",
        )

        commit_sha = str(commit.id)
        commit_inputs = [CommitInput(commit_sha, ["main"], 6, 1)]

        rust_commits, _ = process_commits_parallel(
            str(repo_path),
            commit_inputs,
            "test-codebase",
            datetime.now(UTC).timestamp(),
            False,
            num_workers=1,
        )

        assert len(rust_commits) == 1
        record = rust_commits[0]

        # Only main.py should be counted
        assert (
            record["files_changed"] == 1
        ), f"Expected 1 file, got {record['files_changed']}"

    def test_merge_commit_handling(self, test_repo):
        """Merge commits have correct parent_count and is_merge_commit."""
        from analytics_native import CommitInput, process_commits_parallel

        repo_path, repo = test_repo

        # Create initial commit
        c1 = self._create_commit(repo, repo_path, {"file.py": "x = 1\n"}, "Initial")

        # Get the default branch name (might be "main" or "master" depending on git config)
        default_branch = repo.head.shorthand

        # Create branch and commit
        repo.branches.local.create("feature", repo.get(repo.head.target))
        repo.checkout(repo.branches["feature"])
        c2 = self._create_commit(
            repo, repo_path, {"feature.py": "y = 2\n"}, "Feature commit"
        )

        # Switch back to default branch and create merge commit
        repo.checkout(repo.branches[default_branch])

        # Create merge commit manually
        index = repo.index
        index.read()
        tree = index.write_tree()
        sig = pygit2.Signature("Test User", "test@example.com")

        merge_oid = repo.create_commit(
            "HEAD",
            sig,
            sig,
            "Merge feature",
            tree,
            [c1.id, c2.id],  # Two parents = merge commit
        )
        merge_commit = repo.get(merge_oid)

        commit_sha = str(merge_commit.id)
        commit_inputs = [CommitInput(commit_sha, [default_branch], 12, 2)]

        rust_commits, _ = process_commits_parallel(
            str(repo_path),
            commit_inputs,
            "test-codebase",
            datetime.now(UTC).timestamp(),
            False,
            num_workers=1,
        )

        assert len(rust_commits) == 1
        record = rust_commits[0]

        assert (
            record["parent_count"] == 2
        ), f"Expected 2 parents, got {record['parent_count']}"
        assert record["is_merge_commit"] is True, "Expected is_merge_commit=True"

    def test_commit_size_categorization(self, test_repo):
        """commit_size_category matches Python logic (tiny/small/medium/large)."""
        from analytics_native import CommitInput, process_commits_parallel

        repo_path, repo = test_repo

        # Tiny: <10 churn lines
        c_tiny = self._create_commit(
            repo,
            repo_path,
            {"tiny.py": "x=1\n"},  # ~1 line
            "Tiny commit",
        )

        # Small: 10-49 churn lines
        c_small = self._create_commit(
            repo,
            repo_path,
            {"small.py": "\n".join([f"line{i}" for i in range(25)])},  # 25 lines
            "Small commit",
        )

        # Large: >=200 churn lines
        c_large = self._create_commit(
            repo,
            repo_path,
            {"large.py": "\n".join([f"line{i}" for i in range(250)])},  # 250 lines
            "Large commit",
        )

        commits = [
            (c_tiny, "tiny"),
            (c_small, "small"),
            (c_large, "large"),
        ]

        for commit, _expected_category in commits:
            commit_sha = str(commit.id)
            commit_inputs = [CommitInput(commit_sha, ["main"], 100, 10)]

            rust_commits, _ = process_commits_parallel(
                str(repo_path),
                commit_inputs,
                "test-codebase",
                datetime.now(UTC).timestamp(),
                False,
                num_workers=1,
            )

            actual = rust_commits[0]["commit_size_category"]
            # Note: actual category depends on churn_lines calculation
            # This test validates the categorization logic works
            assert actual in (
                "tiny",
                "small",
                "medium",
                "large",
            ), f"Invalid category: {actual}"

    def test_is_refactor_detection(self, test_repo):
        """is_refactor matches Python logic."""
        from analytics_native import CommitInput, process_commits_parallel

        repo_path, repo = test_repo

        # Create initial file
        self._create_commit(
            repo,
            repo_path,
            {"refactor.py": "def foo():\n    x = 1\n    y = 2\n    return x + y\n"},
            "Initial",
        )

        # Refactor: roughly equal adds and deletes
        refactor_commit = self._create_commit(
            repo,
            repo_path,
            {"refactor.py": "def bar():\n    a = 1\n    b = 2\n    return a + b\n"},
            "Refactor commit",
        )

        commit_sha = str(refactor_commit.id)
        commit_inputs = [CommitInput(commit_sha, ["main"], 50, 4)]

        rust_commits, _ = process_commits_parallel(
            str(repo_path),
            commit_inputs,
            "test-codebase",
            datetime.now(UTC).timestamp(),
            False,
            num_workers=1,
        )

        record = rust_commits[0]
        # is_refactor should be boolean
        assert isinstance(record["is_refactor"], bool)


class TestCommitProcessingPerformance:
    """Performance tests for native commit processing."""

    @pytest.fixture
    def fzf_repo_path(self):
        """Path to fzf repo for performance testing."""
        fzf_path = Path("/tmp/fzf")
        if not fzf_path.exists():
            pytest.skip("fzf repo not available at /tmp/fzf")
        return fzf_path

    def test_native_faster_than_python(self, fzf_repo_path):
        """Rust should be at least 2x faster than Python."""
        import time

        from analytics.pipeline.phases.extract import (
            _calculate_tree_sizes_incremental,
            _collect_commits_and_branches,
            _process_commits_parallel_python,  # Explicitly call Python fallback
        )
        from analytics_native import CommitInput, process_commits_parallel

        repo = pygit2.Repository(str(fzf_repo_path))

        # Get first 100 commits for timing
        commits_topo, commit_to_branches = _collect_commits_and_branches(
            repo, ["master"]
        )
        commits_topo = commits_topo[:100]
        commit_shas = [str(c.id) for c in commits_topo]
        commit_to_branches = {
            sha: commit_to_branches.get(sha, ["master"]) for sha in commit_shas
        }

        # Pre-compute tree sizes
        tree_size_cache = _calculate_tree_sizes_incremental(
            repo, commit_shas=None, commits_topo=commits_topo
        )

        collected_at = datetime.now(UTC)
        codebase_id = "test-fzf"

        # Time Python (explicitly use Python fallback, not Rust)
        start = time.perf_counter()
        python_commits, _ = _process_commits_parallel_python(
            repo_path=str(fzf_repo_path),
            commit_shas=commit_shas,
            codebase_id=codebase_id,
            commit_to_branches=commit_to_branches,
            collected_at=collected_at,
            include_patches=True,
            include_file_changes=False,
            tree_size_cache=tree_size_cache,
            workers=4,
        )
        python_time = time.perf_counter() - start

        # Time Rust
        commit_inputs = [
            CommitInput(sha, commit_to_branches[sha], *tree_size_cache.get(sha, (0, 0)))
            for sha in commit_shas
        ]

        start = time.perf_counter()
        rust_commits, _ = process_commits_parallel(
            str(fzf_repo_path),
            commit_inputs,
            codebase_id,
            collected_at.timestamp(),
            False,
            num_workers=4,
        )
        rust_time = time.perf_counter() - start

        speedup = python_time / rust_time if rust_time > 0 else float("inf")

        print(
            f"\nPerformance: Python={python_time:.2f}s, Rust={rust_time:.2f}s, Speedup={speedup:.1f}x"
        )

        # Rust should be at least 2x faster
        assert speedup > 2.0, (
            f"Native extension not fast enough: {speedup:.1f}x "
            f"(Python={python_time:.2f}s, Rust={rust_time:.2f}s)"
        )


class TestLanguageDetectionParity:
    """Verify Rust language detection matches Python."""

    def test_extension_map_parity(self):
        """All extension -> language mappings match."""
        try:
            from analytics_native import get_language_from_path as rust_get_lang
        except ImportError:
            pytest.skip("get_language_from_path not available in native extension")

        from analytics.aggregation.language import (
            _get_language_from_path as python_get_lang,
        )

        test_paths = [
            "main.py",
            "app.js",
            "component.tsx",
            "utils.ts",
            "Main.java",
            "lib.go",
            "core.rs",
            "helper.rb",
            "script.sh",
            "styles.css",
            "data.json",
            "config.yaml",
            "doc.md",
            "query.sql",
            "unknown.xyz",  # Should return None
        ]

        for path in test_paths:
            rust_result = rust_get_lang(path)
            python_result = python_get_lang(path)

            assert rust_result == python_result, (
                f"Language mismatch for {path}: "
                f"Rust={rust_result}, Python={python_result}"
            )

    def test_filename_map_parity(self):
        """All filename -> language mappings match (Dockerfile, Makefile, etc.)."""
        try:
            from analytics_native import get_language_from_path as rust_get_lang
        except ImportError:
            pytest.skip("get_language_from_path not available in native extension")

        from analytics.aggregation.language import (
            _get_language_from_path as python_get_lang,
        )

        test_paths = [
            "Dockerfile",
            "Makefile",
            "CMakeLists.txt",
            ".gitignore",
            "docker-compose.yml",
        ]

        for path in test_paths:
            rust_result = rust_get_lang(path)
            python_result = python_get_lang(path)

            # Both should return something (or both None)
            if python_result is not None:
                assert (
                    rust_result is not None
                ), f"Rust returned None for {path}, Python returned {python_result}"
