"""
Integration tests for branch membership assignment.

These tests verify that commits are correctly assigned to all branches
they belong to. This is critical for the inverted index optimization
which changes how branch membership is computed.
"""

import pygit2
import pytest


class TestBranchMembershipAssignment:
    """Verify commits are assigned to correct branches."""

    @pytest.fixture
    def repo_with_branches(self, temp_dir):
        """Create a repo with multiple branches sharing commits."""
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
        # If no commits yet, check what HEAD would point to
        return "master"  # Default for pygit2

    def _create_commit(
        self,
        repo: pygit2.Repository,
        repo_path,
        filename: str,
        content: str,
        message: str,
        parents: list | None = None,
    ) -> pygit2.Commit:
        """Helper to create a commit."""
        file_path = repo_path / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)

        index = repo.index
        index.add(filename)
        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@test.com")
        if parents is None:
            parents = [repo.head.target] if not repo.head_is_unborn else []

        commit_oid = repo.create_commit("HEAD", sig, sig, message, tree, parents)
        return repo.get(commit_oid)

    def test_commit_on_single_branch(self, repo_with_branches):
        """Commit on only one branch should have that branch in its record."""
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = repo_with_branches

        # Create initial commit on main
        self._create_commit(repo, repo_path, "main.py", "x = 1\n", "Initial commit")

        # Create feature branch and add commit only on feature
        main_head = repo.head.peel()
        repo.branches.create("feature", main_head)
        repo.checkout(repo.branches["feature"])
        feature_commit = self._create_commit(
            repo, repo_path, "feature.py", "y = 2\n", "Feature commit"
        )

        # Extract commits for feature branch only
        result = extract_commits(
            repo=repo,
            codebase_id="test",
            branch_names=["feature"],
        )

        assert result.success

        # Find the feature commit record
        feature_records = [
            c for c in result.commits if c["commit_sha"] == str(feature_commit.id)
        ]

        assert len(feature_records) == 1
        assert feature_records[0]["branch_name"] == "feature"

    def test_commit_on_multiple_branches(self, repo_with_branches):
        """Commit that's an ancestor of multiple branches should appear in all."""
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = repo_with_branches

        # Create initial commit (will be ancestor of both branches)
        initial_commit = self._create_commit(
            repo, repo_path, "main.py", "x = 1\n", "Initial commit"
        )

        default_branch = self._get_default_branch(repo)

        # Create feature branch from initial commit
        repo.branches.create("feature", initial_commit)

        # Add commit on default branch
        self._create_commit(repo, repo_path, "main2.py", "z = 3\n", "Main commit")

        # Add commit on feature
        repo.checkout(repo.branches["feature"])
        self._create_commit(repo, repo_path, "feature.py", "y = 2\n", "Feature commit")

        # Extract commits for both branches
        result = extract_commits(
            repo=repo,
            codebase_id="test",
            branch_names=[default_branch, "feature"],
        )

        assert result.success

        # Find all records for the initial commit
        initial_sha = str(initial_commit.id)
        initial_records = [c for c in result.commits if c["commit_sha"] == initial_sha]

        # Initial commit should appear once per branch it belongs to
        branch_names = {r["branch_name"] for r in initial_records}
        assert (
            default_branch in branch_names
        ), f"Initial commit should be on {default_branch}"
        assert "feature" in branch_names, "Initial commit should be on feature"

    def test_branch_membership_count(self, repo_with_branches):
        """Verify correct number of records per commit based on branch membership."""
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = repo_with_branches

        # Create history:
        #   default: A -- B -- C
        #             \
        #   feature:   D -- E
        #
        # A should be on both branches (2 records)
        # B, C should be on default only (1 record each)
        # D, E should be on feature only (1 record each)

        # Commit A (ancestor of both)
        commit_a = self._create_commit(repo, repo_path, "a.py", "a = 1\n", "Commit A")

        default_branch = self._get_default_branch(repo)

        # Create feature branch at A
        repo.branches.create("feature", commit_a)

        # Commits B, C on default branch
        commit_b = self._create_commit(repo, repo_path, "b.py", "b = 2\n", "Commit B")
        commit_c = self._create_commit(repo, repo_path, "c.py", "c = 3\n", "Commit C")

        # Commits D, E on feature
        repo.checkout(repo.branches["feature"])
        commit_d = self._create_commit(repo, repo_path, "d.py", "d = 4\n", "Commit D")
        commit_e = self._create_commit(repo, repo_path, "e.py", "e = 5\n", "Commit E")

        # Extract all
        result = extract_commits(
            repo=repo,
            codebase_id="test",
            branch_names=[default_branch, "feature"],
        )

        assert result.success

        # Count records per commit
        def count_records(sha):
            return len([c for c in result.commits if c["commit_sha"] == sha])

        # A is ancestor of both -> 2 records
        assert count_records(str(commit_a.id)) == 2, "Commit A should have 2 records"

        # B, C are default only -> 1 record each
        assert count_records(str(commit_b.id)) == 1, "Commit B should have 1 record"
        assert count_records(str(commit_c.id)) == 1, "Commit C should have 1 record"

        # D, E are feature only -> 1 record each
        assert count_records(str(commit_d.id)) == 1, "Commit D should have 1 record"
        assert count_records(str(commit_e.id)) == 1, "Commit E should have 1 record"

    def test_total_commits_vs_total_records(self, repo_with_branches):
        """Total unique commits should differ from total records when branches share history."""
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = repo_with_branches

        # Create shared history
        commit_a = self._create_commit(repo, repo_path, "a.py", "a = 1\n", "Commit A")

        default_branch = self._get_default_branch(repo)
        repo.branches.create("feature", commit_a)

        # Add unique commits to each branch
        self._create_commit(repo, repo_path, "default.py", "m = 1\n", "Default only")
        repo.checkout(repo.branches["feature"])
        self._create_commit(repo, repo_path, "feature.py", "f = 1\n", "Feature only")

        result = extract_commits(
            repo=repo,
            codebase_id="test",
            branch_names=[default_branch, "feature"],
        )

        assert result.success

        # total_commits = unique commits = 3 (A, default-only, feature-only)
        # len(commits) = records = 4 (A*2 + default-only*1 + feature-only*1)
        assert result.total_commits == 3, "Should have 3 unique commits"
        assert (
            len(result.commits) == 4
        ), "Should have 4 commit records (A counted twice)"


class TestBranchMembershipAfterMerge:
    """Test branch membership behavior with merge commits."""

    @pytest.fixture
    def repo_for_merge(self, temp_dir):
        """Create a repo for merge testing."""
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

    def _create_commit_on_branch(
        self,
        repo: pygit2.Repository,
        repo_path,
        filename: str,
        content: str,
        message: str,
    ) -> pygit2.Commit:
        """Helper to create a commit on current branch."""
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

    def test_merge_commit_branch_membership(self, repo_for_merge):
        """Merge commit should belong to the target branch."""
        from analytics.pipeline.phases.extract import extract_commits

        repo_path, repo = repo_for_merge

        # Create initial commit
        initial = self._create_commit_on_branch(
            repo, repo_path, "initial.py", "x = 1\n", "Initial"
        )

        default_branch = self._get_default_branch(repo)

        # Create feature branch
        repo.branches.create("feature", initial)
        repo.checkout(repo.branches["feature"])

        # Add commit on feature
        self._create_commit_on_branch(
            repo, repo_path, "feature.py", "y = 2\n", "Feature work"
        )
        feature_head = repo.head.peel()

        # Back to default branch, add another commit
        repo.checkout(repo.branches[default_branch])
        self._create_commit_on_branch(
            repo, repo_path, "default2.py", "z = 3\n", "Default work"
        )

        # Merge feature into default branch
        repo.merge(feature_head.id)
        index = repo.index
        index.add_all()
        index.write()
        merge_tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@test.com")
        default_head = repo.head.peel()
        merge_oid = repo.create_commit(
            "HEAD",
            sig,
            sig,
            "Merge feature",
            merge_tree,
            [default_head.id, feature_head.id],
        )

        # Extract from default branch only
        result = extract_commits(
            repo=repo,
            codebase_id="test",
            branch_names=[default_branch],
        )

        assert result.success

        # Find merge commit
        merge_records = [c for c in result.commits if c["commit_sha"] == str(merge_oid)]

        assert len(merge_records) == 1
        assert merge_records[0]["branch_name"] == default_branch
        assert merge_records[0]["is_merge_commit"] is True
