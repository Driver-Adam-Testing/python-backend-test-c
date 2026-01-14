"""
Shared fixtures and utilities for analytics tests.

Adapted from GitStats test suite patterns.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add src directory to Python path for imports
_src_path = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))

# Add shared package to Python path (for shared.file_storage, etc.)
_shared_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "packages" / "shared"
)
if str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock Hatchet before any imports that might trigger it
sys.modules["hatchet_client"] = MagicMock()

import tempfile
from datetime import UTC, date, datetime

import pygit2
import pytest
from analytics.storage.hot_storage import HotStorage
from analytics.storage.parquet_storage import ParquetStorage

# ============================================================================
# Storage Fixtures
# ============================================================================


@pytest.fixture(scope="function")
def temp_dir():
    """Create a temporary directory for test data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture(scope="function")
def hot_storage(temp_dir):
    """Create a temporary hot storage instance."""
    hot_path = temp_dir / "hot" / "analytics.duckdb"
    hot_path.parent.mkdir(parents=True, exist_ok=True)

    storage = HotStorage(hot_path)
    storage.connect()

    yield storage

    storage.close()


@pytest.fixture(scope="function")
def warm_storage(temp_dir):
    """Create a temporary warm storage instance."""
    warm_path = temp_dir / "warm"
    warm_path.mkdir(parents=True, exist_ok=True)

    return ParquetStorage(warm_path)


@pytest.fixture(scope="function")
def cold_storage(temp_dir):
    """Create a temporary cold storage instance."""
    cold_path = temp_dir / "cold"
    cold_path.mkdir(parents=True, exist_ok=True)

    return ParquetStorage(cold_path)


@pytest.fixture(scope="function")
def all_storage(hot_storage, warm_storage, cold_storage):
    """Provide all three storage tiers."""
    return {
        "hot": hot_storage,
        "warm": warm_storage,
        "cold": cold_storage,
    }


# ============================================================================
# Git Repository Fixtures
# ============================================================================


@pytest.fixture(scope="function")
def git_repo(temp_dir):
    """
    Create a real git repository for testing using pygit2.

    Returns:
        Tuple of (repo_path, pygit2.Repository)
    """
    repo_path = temp_dir / "test_repo"
    repo_path.mkdir(parents=True, exist_ok=True)

    # Initialize repository
    repo = pygit2.init_repository(str(repo_path), bare=False)

    # Configure identity
    config = repo.config
    config["user.name"] = "Test User"
    config["user.email"] = "test@example.com"

    # Create initial commit
    readme = repo_path / "README.md"
    readme.write_text("# Test Repository\n\nThis is a test repository.\n")

    index = repo.index
    index.add("README.md")
    index.write()
    tree = index.write_tree()

    author = pygit2.Signature("Test User", "test@example.com")
    repo.create_commit("HEAD", author, author, "Initial commit", tree, [])

    return repo_path, repo


@pytest.fixture(scope="function")
def git_repo_with_history(git_repo):
    """
    Create a git repository with multiple commits.

    Returns:
        Tuple of (repo_path, pygit2.Repository, commit_count)
    """
    repo_path, repo = git_repo

    # Create 10 more commits
    for i in range(1, 11):
        # Create or modify a file
        file_path = repo_path / f"file_{i}.py"
        file_path.write_text(f'''
def function_{i}():
    """Function number {i}"""
    return {i}


class Class{i}:
    """Class number {i}"""

    def method(self):
        return {i}
''')

        index = repo.index
        index.add(f"file_{i}.py")
        index.write()
        tree = index.write_tree()

        author = pygit2.Signature("Test User", "test@example.com")
        parent = repo.head.peel(pygit2.Commit)

        repo.create_commit("HEAD", author, author, f"Add file {i}", tree, [parent.id])

    return repo_path, repo, 11  # 1 initial + 10 new commits


@pytest.fixture(scope="function")
def test_repo_with_commits(temp_dir):
    """Create a test repo with 100 commits for checkpoint testing.

    Returns:
        Tuple of (repo_path, commits) where commits is a list of (sha, parent_sha) tuples.
    """
    repo_path = temp_dir / "checkpoint_test_repo"
    repo_path.mkdir()

    repo = pygit2.init_repository(str(repo_path), bare=False)
    config = repo.config
    config["user.name"] = "Test"
    config["user.email"] = "test@test.com"

    commits = []  # List of (sha, parent_sha) tuples

    for i in range(100):
        # Create/modify file
        file_path = repo_path / f"file_{i % 10}.py"
        file_path.write_text(f"# File {i % 10}\nx = {i}\n" + "y = 1\n" * (i % 5))

        index = repo.index
        index.add(f"file_{i % 10}.py")
        index.write()
        tree = index.write_tree()

        sig = pygit2.Signature("Test", "test@test.com")
        parents = [repo.head.target] if not repo.head_is_unborn else []
        parent_sha = str(parents[0]) if parents else None

        commit_oid = repo.create_commit("HEAD", sig, sig, f"Commit {i}", tree, parents)
        commits.append((str(commit_oid), parent_sha))

    return str(repo_path), commits


@pytest.fixture(scope="function")
def git_repo_with_branches(git_repo_with_history):
    """
    Create a git repository with multiple branches.

    Returns:
        Tuple of (repo_path, pygit2.Repository, branches_info)
    """
    repo_path, repo, commit_count = git_repo_with_history

    # Get current commit (main branch)
    main_commit = repo.head.peel(pygit2.Commit)

    # Create feature branch
    feature_branch = repo.branches.create("feature", main_commit)

    # Checkout feature branch and add commits
    repo.checkout(feature_branch)

    for i in range(3):
        file_path = repo_path / f"feature_{i}.py"
        file_path.write_text(
            f"# Feature {i}\ndef feature_func_{i}():\n    return {i}\n"
        )

        index = repo.index
        index.add(f"feature_{i}.py")
        index.write()
        tree = index.write_tree()

        author = pygit2.Signature("Feature Dev", "feature@example.com")
        parent = repo.head.peel(pygit2.Commit)

        repo.create_commit(
            "HEAD", author, author, f"Add feature {i}", tree, [parent.id]
        )

    # Checkout main branch again
    main_branch = (
        repo.branches["main"] if "main" in repo.branches else repo.branches["master"]
    )
    repo.checkout(main_branch)

    branches_info = {
        "main": {
            "name": main_branch.name,
            "commits": commit_count,
        },
        "feature": {
            "name": feature_branch.name,
            "commits": commit_count + 3,  # main commits + feature commits
            "unique_commits": 3,
        },
    }

    return repo_path, repo, branches_info


# ============================================================================
# Data Generation Helpers
# ============================================================================


def generate_commits(
    count: int,
    codebase_id: str = "550e8400-e29b-41d4-a716-446655440000",
    branch: str = "main",
    start_date: datetime | None = None,
) -> list[dict]:
    """
    Generate test commit data.

    Args:
        count: Number of commits to generate
        codebase_id: Repository ID
        branch: Branch name
        start_date: Starting date for commits (default: 2024-01-01)

    Returns:
        List of commit dictionaries
    """
    if start_date is None:
        start_date = datetime(2024, 1, 1, tzinfo=UTC)

    commits = []
    for i in range(count):
        day = (i // 10) + 1  # 10 commits per day
        commits.append(
            {
                "commit_sha": f"{codebase_id[:8]}{branch[:8]}{i:024d}"[:40],
                "codebase_id": codebase_id,
                "branch_name": branch,
                "committed_at": datetime(2024, 1, day, 10 + (i % 10), 0, 0, tzinfo=UTC),
                "collected_at": datetime.now(UTC),
                "commit_date": date(2024, 1, day),
                "commit_year": 2024,
                "commit_month": 1,
                "commit_day": day,
                "author_email": f"author{i % 5}@example.com",
                "author_name": f"Author {i % 5}",
                "committer_email": f"author{i % 5}@example.com",
                "committer_name": f"Author {i % 5}",
                "message": f"Commit {i}: Implement feature",
                "message_length": 25,
                "parent_count": 1 if i > 0 else 0,
                "is_merge_commit": (i % 20) == 0,
                "files_changed": 3 + (i % 5),
                "additions_lines": 50 + (i % 100),
                "deletions_lines": 10 + (i % 30),
                "net_lines": 40 + (i % 70),
                "churn_lines": 60 + (i % 130),
                "addition_bytes": 2500 + (i * 50),
                "deletion_bytes": 500 + (i * 10),
                "patch_bytes": 3000 + (i * 60),
                "net_bytes": 2000 + (i * 40),
                "sloc": 50 + (i % 100),
                "bytes_per_line": 50.0,
                "commit_size_category": ["tiny", "small", "medium", "large"][i % 4],
                "is_refactor": (i % 15) == 0,
                "collection_version": "2.0",
            }
        )

    return commits


def generate_file_changes(commits: list[dict], files_per_commit: int = 2) -> list[dict]:
    """
    Generate file changes for a list of commits.

    Args:
        commits: List of commit dictionaries
        files_per_commit: Number of file changes per commit

    Returns:
        List of file change dictionaries
    """
    file_changes = []

    for commit in commits:
        for j in range(files_per_commit):
            file_changes.append(
                {
                    "commit_sha": commit["commit_sha"],
                    "codebase_id": commit["codebase_id"],
                    "file_path": f"src/module{j}/file.py",
                    "commit_date": commit["commit_date"],
                    "change_type": ["A", "M", "D"][len(file_changes) % 3],
                    "previous_path": None,
                    "additions_lines": commit["additions_lines"] // files_per_commit,
                    "deletions_lines": commit["deletions_lines"] // files_per_commit,
                    "changes_lines": (
                        commit["additions_lines"] + commit["deletions_lines"]
                    )
                    // files_per_commit,
                    "addition_bytes": commit["addition_bytes"] // files_per_commit,
                    "deletion_bytes": commit["deletion_bytes"] // files_per_commit,
                    "file_sloc": commit["sloc"] // files_per_commit,
                    "file_extension": ".py",
                    "file_language": "python",
                    "has_patch_data": False,
                    "patch_blob_key": None,
                }
            )

    return file_changes


def generate_contributors(
    codebase_id: str = "550e8400-e29b-41d4-a716-446655440000", count: int = 5
) -> list[dict]:
    """
    Generate test contributor data.

    Args:
        codebase_id: Repository ID
        count: Number of contributors

    Returns:
        List of contributor dictionaries
    """
    contributors = []

    for i in range(count):
        contributors.append(
            {
                "codebase_id": codebase_id,
                "contributor_email": f"author{i}@example.com",
                "contributor_name": f"Author {i}",
                "total_commits": 20 + (i * 5),
                "first_commit_at": datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC),
                "last_commit_at": datetime(2024, 1, 31, 18, 0, 0, tzinfo=UTC),
                "branches_contributed_to": ["main", "develop"],
                "primary_branch": "main",
                "branches_count": 2,
                "commits_last_30_days": 15,
                "commits_last_90_days": 20,
                "commits_last_365_days": 20,
                "total_additions_lines": 1000 + (i * 200),
                "total_deletions_lines": 200 + (i * 40),
                "avg_commit_size_lines": 60.0,
                "total_sloc_contributed": 900 + (i * 180),
                "total_addition_bytes": 50000 + (i * 10000),
                "total_deletion_bytes": 10000 + (i * 2000),
                "avg_commit_size_sloc": 45.0,
                "collected_at": datetime.now(UTC),
            }
        )

    return contributors


# ============================================================================
# Populated Storage Fixtures
# ============================================================================


@pytest.fixture(scope="function")
def populated_storage(all_storage):
    """
    Provide storage with test data already populated.

    Includes:
    - 100 commits
    - 5 contributors
    - 200 file changes
    - Aggregates built
    """
    from analytics.aggregation.engine import AggregationEngine

    hot = all_storage["hot"]
    warm = all_storage["warm"]
    cold = all_storage["cold"]

    codebase_id = "550e8400-e29b-41d4-a716-446655440000"

    # Generate and write data
    commits = generate_commits(100, codebase_id)
    warm.write_commits(codebase_id, commits)

    file_changes = generate_file_changes(commits, files_per_commit=2)
    cold.write_file_changes(codebase_id, file_changes)

    contributors = generate_contributors(codebase_id, count=5)
    warm.write_contributors(codebase_id, contributors)

    # Build aggregates
    engine = AggregationEngine(hot, warm, cold)
    engine.build_all_aggregates(codebase_id, force_rebuild=True)
    engine.refresh_branch_metrics(codebase_id)

    return {
        "hot": hot,
        "warm": warm,
        "cold": cold,
        "codebase_id": codebase_id,
        "commit_count": 100,
        "contributor_count": 5,
    }


# ============================================================================
# Assertion Helpers
# ============================================================================


def assert_dual_sloc_metrics(data: dict):
    """
    Assert that dual SLOC metrics are present and valid.

    Args:
        data: Dictionary containing metrics
    """
    # Line-based metrics
    assert "additions_lines" in data or "total_additions_lines" in data
    assert "deletions_lines" in data or "total_deletions_lines" in data

    # Byte-based metrics
    assert "addition_bytes" in data or "total_addition_bytes" in data
    assert "sloc" in data or "total_sloc" in data

    # Values should be non-negative
    for key, value in data.items():
        if "lines" in key or "bytes" in key or "sloc" in key:
            if value is not None:
                assert value >= 0, f"{key} should be non-negative, got {value}"


def assert_storage_consistency(hot, warm, codebase_id: str):
    """
    Assert that storage tiers are consistent.

    Args:
        hot: Hot storage instance
        warm: Warm storage instance
        codebase_id: Repository ID to check
    """
    # Get metrics from hot storage
    repo_metrics = hot.get_repository_metrics(codebase_id)
    assert repo_metrics is not None

    # Get commits from warm storage
    commits_df = warm.read_commits(codebase_id)
    assert len(commits_df) > 0

    # Verify commit count matches (deduplicate by commit_sha)
    unique_commits = commits_df.drop_duplicates(subset=["commit_sha"])
    assert repo_metrics["total_commits"] == len(unique_commits)


# ============================================================================
# Performance Helpers
# ============================================================================


class PerformanceTimer:
    """Context manager for timing operations."""

    def __init__(self, name: str, target_seconds: float | None = None):
        """
        Initialize timer.

        Args:
            name: Name of the operation
            target_seconds: Target time in seconds (optional)
        """
        self.name = name
        self.target_seconds = target_seconds
        self.start_time = None
        self.elapsed_seconds = None

    def __enter__(self):
        """Start timer."""
        import time

        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop timer and check target."""
        import time

        self.elapsed_seconds = time.time() - self.start_time

        print(f"\n{self.name}: {self.elapsed_seconds:.2f}s")

        if self.target_seconds is not None:
            assert self.elapsed_seconds < self.target_seconds, (
                f"{self.name} took {self.elapsed_seconds:.2f}s, "
                f"target was <{self.target_seconds}s"
            )
