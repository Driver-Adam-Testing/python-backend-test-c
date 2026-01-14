"""
Integration tests for the Rust native extension.

These tests verify that the native Rust implementation produces
identical results to the Python implementation.

The tests will SKIP if the native extension is not installed,
making them safe to run in CI before the extension is available.
"""

import pygit2
import pytest


def native_extension_available():
    """Check if the native extension is installed."""
    try:
        import analytics_native  # noqa: F401

        return True
    except ImportError:
        return False


# Skip all tests in this module if native extension not available
pytestmark = pytest.mark.skipif(
    not native_extension_available(), reason="Native extension not installed"
)


class TestFilterParity:
    """Verify Rust filter implementation matches Python exactly."""

    def test_blacklisted_directories_match(self):
        """Rust should blacklist the same directories as Python."""
        from analytics.utils.file_filter import is_analyzable_path
        from analytics_native import is_path_analyzable as native_is_analyzable

        test_paths = [
            # Should be blacklisted
            ".git/config",
            ".git/objects/pack/file",
            "driver_docs/README.md",
            "driver_docs/api/overview.md",
            # Should NOT be blacklisted
            "src/main.py",
            "git_utils.py",  # "git" in name, not directory
            "docs/readme.md",
        ]

        for path in test_paths:
            python_result = is_analyzable_path(path)
            rust_result = native_is_analyzable(path)
            assert python_result == rust_result, (
                f"Filter mismatch for {path}: "
                f"Python={python_result}, Rust={rust_result}"
            )

    def test_blacklisted_extensions_match(self):
        """Rust should blacklist the same extensions as Python."""
        from analytics.utils.file_filter import is_analyzable_path
        from analytics_native import is_path_analyzable as native_is_analyzable

        test_paths = [
            # Should be blacklisted
            "assets/logo.svg",
            "build/output.exe",
            "lib/mylib.dll",
            "obj/file.o",
            "data/dump.hex",
            "firmware.bin",
            # Should NOT be blacklisted
            "src/main.py",
            "src/app.js",
            "src/Component.tsx",
            "package.json",
            "README.md",
        ]

        for path in test_paths:
            python_result = is_analyzable_path(path)
            rust_result = native_is_analyzable(path)
            assert python_result == rust_result, (
                f"Filter mismatch for {path}: "
                f"Python={python_result}, Rust={rust_result}"
            )

    def test_special_filenames_match(self):
        """Rust should handle special filenames like Python."""
        from analytics.utils.file_filter import is_analyzable_path
        from analytics_native import is_path_analyzable as native_is_analyzable

        test_paths = [
            # Should be blacklisted
            ".DS_Store",
            "src/.DS_Store",
            ".driverignore",
            # Should NOT be blacklisted
            "Dockerfile",
            "Makefile",
            ".gitignore",
        ]

        for path in test_paths:
            python_result = is_analyzable_path(path)
            rust_result = native_is_analyzable(path)
            assert python_result == rust_result, (
                f"Filter mismatch for {path}: "
                f"Python={python_result}, Rust={rust_result}"
            )


class TestTreeSizeEquivalence:
    """Verify Rust tree size calculation matches Python exactly."""

    @pytest.fixture
    def test_repo(self, temp_dir):
        """Create a test repository with known content."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

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

        sig = pygit2.Signature("Test", "test@test.com")
        parents = [repo.head.target] if not repo.head_is_unborn else []

        commit_oid = repo.create_commit("HEAD", sig, sig, message, tree, parents)
        return repo.get(commit_oid)

    def test_single_commit_tree_size(self, test_repo):
        """Rust and Python should calculate identical tree size for single commit."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit
        from analytics_native import calculate_tree_sizes

        repo_path, repo = test_repo

        # Create commit with known content
        content = "line1\nline2\nline3\n"
        commit = self._create_commit(
            repo, repo_path, {"main.py": content}, "Add main.py"
        )

        # Python result
        python_bytes, python_lines = _get_tree_size_at_commit(repo, commit)

        # Rust result
        rust_results = calculate_tree_sizes(str(repo_path), [str(commit.id)], 1)
        rust_bytes, rust_lines = rust_results[str(commit.id)]

        assert (
            python_bytes == rust_bytes
        ), f"Byte count mismatch: Python={python_bytes}, Rust={rust_bytes}"
        assert (
            python_lines == rust_lines
        ), f"Line count mismatch: Python={python_lines}, Rust={rust_lines}"

    def test_multiple_commits_tree_sizes(self, test_repo):
        """Rust and Python should match across multiple commits."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit
        from analytics_native import calculate_tree_sizes

        repo_path, repo = test_repo
        commits = []

        # Create several commits
        c1 = self._create_commit(repo, repo_path, {"main.py": "x = 1\n"}, "Commit 1")
        commits.append(c1)

        c2 = self._create_commit(
            repo, repo_path, {"utils.py": "y = 2\nz = 3\n"}, "Commit 2"
        )
        commits.append(c2)

        c3 = self._create_commit(repo, repo_path, {"main.py": "x = 100\n"}, "Commit 3")
        commits.append(c3)

        # Get Rust results for all commits
        commit_shas = [str(c.id) for c in commits]
        rust_results = calculate_tree_sizes(str(repo_path), commit_shas, 1)

        # Compare each commit
        for commit in commits:
            sha = str(commit.id)
            python_bytes, python_lines = _get_tree_size_at_commit(repo, commit)
            rust_bytes, rust_lines = rust_results[sha]

            assert python_bytes == rust_bytes, (
                f"Commit {sha[:8]}: byte mismatch - "
                f"Python={python_bytes}, Rust={rust_bytes}"
            )
            assert python_lines == rust_lines, (
                f"Commit {sha[:8]}: line mismatch - "
                f"Python={python_lines}, Rust={rust_lines}"
            )

    def test_blacklisted_files_excluded(self, test_repo):
        """Both implementations should exclude blacklisted files."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit
        from analytics_native import calculate_tree_sizes

        repo_path, repo = test_repo

        # Create commit with both analyzable and non-analyzable files
        commit = self._create_commit(
            repo,
            repo_path,
            {
                "src/main.py": "x = 1\n",  # Counted
                "driver_docs/doc.py": "y = 2\n",  # NOT counted (blacklisted dir)
                "data/file.svg": "<svg></svg>\n",  # NOT counted (blacklisted ext)
            },
            "Mixed files",
        )

        python_bytes, python_lines = _get_tree_size_at_commit(repo, commit)
        rust_results = calculate_tree_sizes(str(repo_path), [str(commit.id)], 1)
        rust_bytes, rust_lines = rust_results[str(commit.id)]

        # Both should only count main.py (6 bytes, 1 line)
        assert python_bytes == rust_bytes
        assert python_lines == rust_lines
        assert python_lines == 1  # Only main.py

    def test_binary_files_excluded(self, test_repo):
        """Both implementations should exclude binary files."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit
        from analytics_native import calculate_tree_sizes

        repo_path, repo = test_repo

        # Create code file
        (repo_path / "main.py").write_text("x = 1\n")

        # Create binary file
        binary_path = repo_path / "image.png"
        binary_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        index = repo.index
        index.add_all()
        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@test.com")
        commit_oid = repo.create_commit("HEAD", sig, sig, "Add files", tree, [])
        commit = repo.get(commit_oid)

        python_bytes, python_lines = _get_tree_size_at_commit(repo, commit)
        rust_results = calculate_tree_sizes(str(repo_path), [str(commit.id)], 1)
        rust_bytes, rust_lines = rust_results[str(commit.id)]

        assert python_bytes == rust_bytes
        assert python_lines == rust_lines
        assert python_lines == 1  # Only main.py, not binary


class TestRealRepoValidation:
    """Validate native extension against real repositories."""

    @pytest.fixture
    def fzf_repo_path(self):
        """Path to fzf repo for validation (skip if not available)."""
        from pathlib import Path

        fzf_path = Path("/tmp/fzf")
        if not fzf_path.exists():
            pytest.skip("fzf repo not available at /tmp/fzf")
        return fzf_path

    def test_fzf_tree_sizes_match(self, fzf_repo_path):
        """Validate Rust results match Python on real fzf repo."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit
        from analytics_native import calculate_tree_sizes

        repo = pygit2.Repository(str(fzf_repo_path))

        # Get first 100 commits
        commits = []
        for commit in repo.walk(repo.head.target):
            commits.append(commit)
            if len(commits) >= 100:
                break

        # Calculate using Rust
        commit_shas = [str(c.id) for c in commits]
        rust_results = calculate_tree_sizes(str(fzf_repo_path), commit_shas, 4)

        # Compare each commit
        mismatches = []
        for commit in commits:
            sha = str(commit.id)
            python_bytes, python_lines = _get_tree_size_at_commit(repo, commit)
            rust_bytes, rust_lines = rust_results[sha]

            if python_bytes != rust_bytes or python_lines != rust_lines:
                mismatches.append(
                    {
                        "sha": sha[:8],
                        "python": (python_bytes, python_lines),
                        "rust": (rust_bytes, rust_lines),
                    }
                )

        assert len(mismatches) == 0, (
            f"Found {len(mismatches)} mismatches:\n"
            + "\n".join(
                f"  {m['sha']}: Python={m['python']}, Rust={m['rust']}"
                for m in mismatches[:10]
            )
        )


class TestPerformance:
    """Basic performance tests for the native extension."""

    @pytest.fixture
    def fzf_repo_path(self):
        """Path to fzf repo for performance testing."""
        from pathlib import Path

        fzf_path = Path("/tmp/fzf")
        if not fzf_path.exists():
            pytest.skip("fzf repo not available at /tmp/fzf")
        return fzf_path

    def test_native_is_faster_than_python(self, fzf_repo_path):
        """Native extension should be significantly faster than Python."""
        import time

        from analytics.pipeline.phases.extract import _get_tree_size_at_commit
        from analytics_native import calculate_tree_sizes

        repo = pygit2.Repository(str(fzf_repo_path))

        # Get 50 commits for timing
        commits = []
        for commit in repo.walk(repo.head.target):
            commits.append(commit)
            if len(commits) >= 50:
                break

        # Time Python
        start = time.perf_counter()
        for commit in commits:
            _get_tree_size_at_commit(repo, commit)
        python_time = time.perf_counter() - start

        # Time Rust
        commit_shas = [str(c.id) for c in commits]
        start = time.perf_counter()
        calculate_tree_sizes(str(fzf_repo_path), commit_shas, 4)
        rust_time = time.perf_counter() - start

        speedup = python_time / rust_time

        # Native should be at least 2x faster (conservative)
        assert speedup > 2.0, (
            f"Native extension not fast enough: {speedup:.1f}x "
            f"(Python={python_time:.2f}s, Rust={rust_time:.2f}s)"
        )
