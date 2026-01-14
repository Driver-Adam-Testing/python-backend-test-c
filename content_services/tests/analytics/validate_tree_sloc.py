#!/usr/bin/env python3
"""
Validate tree SLOC calculation by comparing full walk vs incremental methods.

Run from content_services directory:
    poetry run python tests/analytics/validate_tree_sloc.py /path/to/repo

This script is the safety net for the incremental tree SLOC implementation.
Run it after every change to catch regressions immediately.
"""

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pygit2

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from analytics.pipeline.phases.extract import (
    _calculate_tree_sizes_incremental,
    _get_tree_size_at_commit,
)


@dataclass
class TreeSize:
    bytes: int
    lines: int

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TreeSize):
            return False
        return self.bytes == other.bytes and self.lines == other.lines


@dataclass
class ValidationResult:
    total_commits: int
    mismatches: int
    full_walk_time: float
    incremental_time: float
    mismatch_details: list[dict]


def get_all_commits_topological(repo: pygit2.Repository) -> list[pygit2.Commit]:
    """Return all reachable commits in topological order (parents before children)."""
    seen = set()
    commits = []

    for branch in repo.branches.local:
        branch_ref = repo.branches[branch]
        head_oid = branch_ref.peel().id

        flags = pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE
        for commit in repo.walk(head_oid, flags):
            if commit.id not in seen:
                seen.add(commit.id)
                commits.append(commit)

    return commits


def calculate_full_walk(
    repo: pygit2.Repository, commits: list[pygit2.Commit]
) -> dict[str, TreeSize]:
    """Calculate tree sizes using full tree walk for each commit."""
    results = {}
    for commit in commits:
        bytes_count, lines_count = _get_tree_size_at_commit(repo, commit)
        results[str(commit.id)] = TreeSize(bytes=bytes_count, lines=lines_count)
    return results


def calculate_incremental(
    repo: pygit2.Repository, commits: list[pygit2.Commit]
) -> dict[str, TreeSize]:
    """
    Calculate tree sizes using incremental method.

    TODO: This will be implemented in Phase C. For now, it falls back to full walk
    to establish the validation infrastructure.
    """
    # Placeholder: currently uses full walk
    # Will be replaced with actual incremental implementation
    return calculate_full_walk(repo, commits)


def validate_results(
    full_walk: dict[str, TreeSize], incremental: dict[str, TreeSize]
) -> list[dict]:
    """Compare results and return list of mismatches."""
    mismatches = []

    for sha, full_size in full_walk.items():
        inc_size = incremental.get(sha)
        if inc_size is None:
            mismatches.append(
                {
                    "sha": sha[:8],
                    "error": "missing from incremental",
                    "full": full_size,
                    "incremental": None,
                }
            )
        elif full_size != inc_size:
            mismatches.append(
                {
                    "sha": sha[:8],
                    "error": "value mismatch",
                    "full": full_size,
                    "incremental": inc_size,
                }
            )

    return mismatches


def print_progress(current: int, total: int, width: int = 40) -> None:
    """Print a progress bar."""
    percent = current / total
    filled = int(width * percent)
    bar = "=" * filled + "-" * (width - filled)
    sys.stdout.write(f"\r[{bar}] {current}/{total}")
    sys.stdout.flush()


def run_validation(repo_path: Path) -> ValidationResult:
    """Run full validation on a repository."""
    repo = pygit2.Repository(str(repo_path))

    print(f"Repository: {repo_path}")
    print("Collecting commits...")

    commits = get_all_commits_topological(repo)
    commit_shas = {str(c.id) for c in commits}
    total = len(commits)
    print(f"Found {total} commits\n")

    # Full walk timing
    print("Running full tree walk...")
    start = time.perf_counter()
    full_walk_results = {}
    for i, commit in enumerate(commits):
        bytes_count, lines_count = _get_tree_size_at_commit(repo, commit, None)
        full_walk_results[str(commit.id)] = TreeSize(
            bytes=bytes_count, lines=lines_count
        )
        if (i + 1) % 100 == 0 or i + 1 == total:
            print_progress(i + 1, total)
    full_walk_time = time.perf_counter() - start
    print(f"\nFull walk: {full_walk_time:.1f}s\n")

    # Incremental timing - use the actual incremental implementation
    print("Running incremental calculation...")
    start = time.perf_counter()
    incremental_cache = _calculate_tree_sizes_incremental(repo, commit_shas)
    incremental_results = {
        sha: TreeSize(bytes=byte_count, lines=line_count)
        for sha, (byte_count, line_count) in incremental_cache.items()
    }
    incremental_time = time.perf_counter() - start
    print(f"Incremental: {incremental_time:.1f}s\n")

    # Compare
    mismatches = validate_results(full_walk_results, incremental_results)

    return ValidationResult(
        total_commits=total,
        mismatches=len(mismatches),
        full_walk_time=full_walk_time,
        incremental_time=incremental_time,
        mismatch_details=mismatches,
    )


def print_results(result: ValidationResult) -> None:
    """Print validation results."""
    print("=" * 60)
    print("VALIDATION RESULTS")
    print("=" * 60)
    print(f"Commits validated:  {result.total_commits}")
    print(f"Full walk time:     {result.full_walk_time:.1f}s")
    print(f"Incremental time:   {result.incremental_time:.1f}s")

    if result.incremental_time > 0:
        speedup = result.full_walk_time / result.incremental_time
        print(f"Speedup:            {speedup:.1f}x")

    print(f"Mismatches:         {result.mismatches}")
    print()

    if result.mismatches > 0:
        print("MISMATCH DETAILS:")
        for m in result.mismatch_details[:10]:
            print(f"  {m['sha']}: {m['error']}")
            print(f"    full: {m['full']}")
            print(f"    inc:  {m['incremental']}")
        if len(result.mismatch_details) > 10:
            print(f"  ... and {len(result.mismatch_details) - 10} more")
        print()
        print("FAILED: Results do not match")
    else:
        print("SUCCESS: All commits match!")


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: poetry run python tests/analytics/validate_tree_sloc.py /path/to/repo"
        )
        return 1

    repo_path = Path(sys.argv[1])
    if not repo_path.exists():
        print(f"ERROR: Repository not found: {repo_path}")
        return 1

    result = run_validation(repo_path)
    print_results(result)

    return 0 if result.mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
