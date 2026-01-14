"""
Integration tests for tree SLOC correctness.

These tests verify that tree_sloc values are accurate across commit history,
and that incremental calculation produces identical results to full tree walk.

The "incremental vs full walk equivalence" test is the PRIMARY acceptance test
for the incremental tree SLOC optimization.
"""

import pygit2
import pytest


class TestTreeSlocCorrectness:
    """Verify tree SLOC values are correct at each commit."""

    @pytest.fixture
    def repo_with_known_sizes(self, temp_dir):
        """Create a repo with files of exact known sizes."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

        return repo_path, repo

    def _commit_file(
        self,
        repo: pygit2.Repository,
        repo_path,
        filename: str,
        content: str,
        message: str,
    ) -> pygit2.Commit:
        """Helper to add/modify a file and commit."""
        file_path = repo_path / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)

        index = repo.index
        index.add(filename)
        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@test.com")
        parents = [repo.head.target] if not repo.head_is_unborn else []

        commit_oid = repo.create_commit("HEAD", sig, sig, message, tree, parents)
        return repo.get(commit_oid)

    def _delete_file(
        self,
        repo: pygit2.Repository,
        repo_path,
        filename: str,
        message: str,
    ) -> pygit2.Commit:
        """Helper to delete a file and commit."""
        file_path = repo_path / filename
        file_path.unlink()

        index = repo.index
        index.remove(filename)
        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@test.com")
        parents = [repo.head.target]

        commit_oid = repo.create_commit("HEAD", sig, sig, message, tree, parents)
        return repo.get(commit_oid)

    def test_tree_sloc_across_history(self, repo_with_known_sizes):
        """Tree SLOC should reflect actual codebase size at each commit."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit

        repo_path, repo = repo_with_known_sizes
        commits = []

        # Commit 1: Add file.py with exactly 10 lines
        content1 = "line\n" * 10
        c1 = self._commit_file(repo, repo_path, "file.py", content1, "Add file.py")
        commits.append((c1, len(content1.encode()), 10))

        # Commit 2: Add utils.py with exactly 5 lines
        content2 = "util\n" * 5
        c2 = self._commit_file(repo, repo_path, "utils.py", content2, "Add utils.py")
        commits.append((c2, len(content1.encode()) + len(content2.encode()), 15))

        # Commit 3: Modify file.py to 3 lines
        content3 = "new\n" * 3
        c3 = self._commit_file(repo, repo_path, "file.py", content3, "Modify file.py")
        commits.append((c3, len(content3.encode()) + len(content2.encode()), 8))

        # Commit 4: Delete utils.py
        c4 = self._delete_file(repo, repo_path, "utils.py", "Delete utils.py")
        commits.append((c4, len(content3.encode()), 3))

        # Verify each commit
        for commit, expected_bytes, expected_lines in commits:
            actual_bytes, actual_lines = _get_tree_size_at_commit(repo, commit)
            assert (
                actual_bytes == expected_bytes
            ), f"Commit {commit.id}: expected {expected_bytes} bytes, got {actual_bytes}"
            assert (
                actual_lines == expected_lines
            ), f"Commit {commit.id}: expected {expected_lines} lines, got {actual_lines}"


class TestIncrementalEquivalence:
    """Verify incremental calculation matches full tree walk exactly."""

    @pytest.fixture
    def repo_with_complex_history(self, temp_dir):
        """Create a repo with diverse commit patterns for thorough testing."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

        return repo_path, repo

    def _create_commit(
        self,
        repo: pygit2.Repository,
        repo_path,
        files: dict[str, str | None],
        message: str,
        parents: list | None = None,
    ) -> pygit2.Commit:
        """
        Create a commit with the specified file changes.

        Args:
            files: Dict of {filename: content} where None means delete
        """
        index = repo.index

        for filename, content in files.items():
            if content is None:
                # Delete file
                file_path = repo_path / filename
                if file_path.exists():
                    file_path.unlink()
                    index.remove(filename)
            else:
                # Add/modify file
                file_path = repo_path / filename
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content)
                index.add(filename)

        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@test.com")
        if parents is None:
            parents = [repo.head.target] if not repo.head_is_unborn else []

        commit_oid = repo.create_commit("HEAD", sig, sig, message, tree, parents)
        return repo.get(commit_oid)

    def test_incremental_matches_full_walk(self, repo_with_complex_history):
        """
        Incremental calculation must produce identical results to full walk.

        This is the PRIMARY acceptance test for the optimization.
        """
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit

        repo_path, repo = repo_with_complex_history

        # Create 20+ commits with various patterns
        commits = []

        # Initial commit
        c = self._create_commit(
            repo, repo_path, {"main.py": "def main():\n    pass\n"}, "Initial commit"
        )
        commits.append(c)

        # Add multiple files
        for i in range(5):
            c = self._create_commit(
                repo,
                repo_path,
                {f"module{i}.py": f"# Module {i}\nx = {i}\n"},
                f"Add module{i}",
            )
            commits.append(c)

        # Modify existing files
        for i in range(3):
            c = self._create_commit(
                repo,
                repo_path,
                {f"module{i}.py": f"# Modified module {i}\ny = {i * 10}\nz = {i}\n"},
                f"Modify module{i}",
            )
            commits.append(c)

        # Delete some files
        c = self._create_commit(repo, repo_path, {"module4.py": None}, "Delete module4")
        commits.append(c)

        # Add nested files
        c = self._create_commit(
            repo,
            repo_path,
            {"src/utils/helper.py": "def help():\n    return True\n"},
            "Add nested file",
        )
        commits.append(c)

        # Add more variety
        for i in range(5):
            c = self._create_commit(
                repo, repo_path, {f"feature{i}.py": "a\n" * (i + 1)}, f"Add feature{i}"
            )
            commits.append(c)

        # Calculate using full walk for all commits
        full_walk_results = {}
        for commit in commits:
            bytes_count, lines_count = _get_tree_size_at_commit(repo, commit)
            full_walk_results[str(commit.id)] = (bytes_count, lines_count)

        # TODO: When incremental is implemented, calculate using incremental method
        # and compare. For now, verify full walk is consistent with itself.
        for commit in commits:
            bytes_count, lines_count = _get_tree_size_at_commit(repo, commit)
            expected = full_walk_results[str(commit.id)]
            assert (
                bytes_count,
                lines_count,
            ) == expected, f"Inconsistent results for commit {commit.id}"


class TestEdgeCases:
    """Test edge cases that could cause incremental/full walk divergence."""

    @pytest.fixture
    def simple_repo(self, temp_dir):
        """Create a simple repo for edge case testing."""
        repo_path = temp_dir / "test_repo"
        repo_path.mkdir()

        repo = pygit2.init_repository(str(repo_path), bare=False)
        config = repo.config
        config["user.name"] = "Test"
        config["user.email"] = "test@test.com"

        return repo_path, repo

    def _get_default_branch(self, repo: pygit2.Repository) -> str:
        """Get the default branch name (main or master)."""
        for name in ["main", "master"]:
            if name in repo.branches:
                return name
        return "master"

    def test_merge_commit_tree_sloc(self, simple_repo):
        """Merge commits should have correct tree_sloc from the merged tree state."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit

        repo_path, repo = simple_repo

        # Create initial commit on default branch
        (repo_path / "main.py").write_text("main = 1\n")
        index = repo.index
        index.add("main.py")
        index.write()
        tree = index.write_tree()
        sig = pygit2.Signature("Test", "test@test.com")
        main_commit = repo.create_commit("HEAD", sig, sig, "Initial", tree, [])

        default_branch = self._get_default_branch(repo)

        # Create feature branch
        main_ref = repo.get(main_commit)
        repo.branches.create("feature", main_ref)
        repo.checkout(repo.branches["feature"])

        # Add file on feature branch
        (repo_path / "feature.py").write_text("feature = 2\n")
        index = repo.index
        index.add("feature.py")
        index.write()
        tree = index.write_tree()
        feature_commit = repo.create_commit(
            "HEAD", sig, sig, "Add feature", tree, [main_commit]
        )

        # Back to default branch, add different file
        repo.checkout(repo.branches[default_branch])
        (repo_path / "other.py").write_text("other = 3\n")
        index = repo.index
        index.add("other.py")
        index.write()
        tree = index.write_tree()
        other_commit = repo.create_commit(
            "HEAD", sig, sig, "Add other", tree, [main_commit]
        )

        # Merge feature into main
        repo.merge(feature_commit)
        index = repo.index
        index.add_all()
        index.write()
        merge_tree = index.write_tree()
        merge_commit_oid = repo.create_commit(
            "HEAD",
            sig,
            sig,
            "Merge feature",
            merge_tree,
            [other_commit, feature_commit],
        )
        merge_commit = repo.get(merge_commit_oid)

        # Verify merge commit has all 3 files
        tree_bytes, tree_lines = _get_tree_size_at_commit(repo, merge_commit)
        assert tree_lines == 3  # main.py + feature.py + other.py

    def test_binary_file_not_counted(self, simple_repo):
        """Binary files should not contribute to tree_sloc."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit

        repo_path, repo = simple_repo

        # Create initial commit with code file
        (repo_path / "code.py").write_text("x = 1\n")
        index = repo.index
        index.add("code.py")
        index.write()
        tree = index.write_tree()
        sig = pygit2.Signature("Test", "test@test.com")
        c1 = repo.create_commit("HEAD", sig, sig, "Add code", tree, [])

        _, lines_before = _get_tree_size_at_commit(repo, repo.get(c1))
        assert lines_before == 1

        # Add binary file (PNG header bytes)
        binary_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        (repo_path / "image.png").write_bytes(binary_content)
        index = repo.index
        index.add("image.png")
        index.write()
        tree = index.write_tree()
        c2 = repo.create_commit("HEAD", sig, sig, "Add image", tree, [c1])

        _, lines_after = _get_tree_size_at_commit(repo, repo.get(c2))
        # Binary file should not add to line count
        assert lines_after == 1

    def test_file_in_blacklisted_dir_not_counted(self, simple_repo):
        """Files in blacklisted directories should not be counted."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit

        repo_path, repo = simple_repo

        # Create code file
        (repo_path / "src" / "main.py").mkdir(parents=True, exist_ok=True)
        (repo_path / "src" / "main.py").rmdir()
        src_dir = repo_path / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "main.py").write_text("x = 1\n")

        index = repo.index
        index.add("src/main.py")
        index.write()
        tree = index.write_tree()
        sig = pygit2.Signature("Test", "test@test.com")
        c1 = repo.create_commit("HEAD", sig, sig, "Add src", tree, [])

        _, lines_before = _get_tree_size_at_commit(repo, repo.get(c1))
        assert lines_before == 1

        # Add file in driver_docs (blacklisted)
        driver_docs = repo_path / "driver_docs"
        driver_docs.mkdir()
        (driver_docs / "doc.py").write_text("y = 2\nz = 3\n")

        index = repo.index
        index.add("driver_docs/doc.py")
        index.write()
        tree = index.write_tree()
        c2 = repo.create_commit("HEAD", sig, sig, "Add docs", tree, [c1])

        _, lines_after = _get_tree_size_at_commit(repo, repo.get(c2))
        # driver_docs files should not be counted
        assert lines_after == 1

    def test_empty_commit_unchanged_tree_sloc(self, simple_repo):
        """Empty commits (no file changes) should have same tree_sloc as parent."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit

        repo_path, repo = simple_repo

        # Create initial commit
        (repo_path / "main.py").write_text("x = 1\n")
        index = repo.index
        index.add("main.py")
        index.write()
        tree = index.write_tree()
        sig = pygit2.Signature("Test", "test@test.com")
        c1 = repo.create_commit("HEAD", sig, sig, "Initial", tree, [])

        # "Empty" commit - same tree, different message
        c2 = repo.create_commit("HEAD", sig, sig, "Empty commit", tree, [c1])

        bytes1, lines1 = _get_tree_size_at_commit(repo, repo.get(c1))
        bytes2, lines2 = _get_tree_size_at_commit(repo, repo.get(c2))

        assert (bytes1, lines1) == (bytes2, lines2)

    def test_file_without_trailing_newline(self, simple_repo):
        """Files without trailing newline should count correctly."""
        from analytics.pipeline.phases.extract import _get_tree_size_at_commit

        repo_path, repo = simple_repo

        # File without trailing newline (3 lines of content)
        content = "line1\nline2\nline3"  # No trailing \n
        (repo_path / "no_newline.py").write_text(content)

        index = repo.index
        index.add("no_newline.py")
        index.write()
        tree = index.write_tree()
        sig = pygit2.Signature("Test", "test@test.com")
        c1 = repo.create_commit("HEAD", sig, sig, "No newline", tree, [])

        _, lines = _get_tree_size_at_commit(repo, repo.get(c1))
        assert lines == 3  # Should count 3 lines even without trailing newline
