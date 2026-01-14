"""
Branches phase: Discover branches and detect lineage using pygit2.

This phase identifies all branches, their parent relationships,
and divergence points from the default branch.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pygit2

logger = logging.getLogger(__name__)


@dataclass
class BranchInfo:
    """Information about a discovered branch."""

    name: str
    head_sha: str
    divergence_point_sha: str | None
    parent_branch: str | None
    created_at: datetime | None
    last_commit_at: datetime
    is_default: bool


@dataclass
class BranchesResult:
    """Result of branch discovery."""

    success: bool
    branches: list[BranchInfo]
    default_branch: str | None
    error: str | None = None


def discover_branches(
    repo: pygit2.Repository, default_branch_only: bool = False
) -> BranchesResult:
    """
    Discover all branches in repository.

    Args:
        repo: pygit2.Repository instance
        default_branch_only: If True, only discover the default branch

    Returns:
        BranchesResult with branch information
    """
    logger.info("Discovering branches...")

    try:
        # Get default branch name
        default_branch_name = _get_default_branch(repo)
        logger.debug(f"Default branch: {default_branch_name}")

        branches = []

        if default_branch_only:
            logger.info("Only discovering default branch")
            branch_info = _analyze_branch(
                repo, default_branch_name, default_branch_name
            )
            if branch_info:
                branches.append(branch_info)
        else:
            # Discover all branches
            branch_names = _get_all_branch_names(repo)
            logger.info(f"Found {len(branch_names)} branches")

            for branch_name in branch_names:
                try:
                    branch_info = _analyze_branch(
                        repo, branch_name, default_branch_name
                    )
                    if branch_info:
                        branches.append(branch_info)
                except Exception as e:
                    logger.warning(f"Error analyzing branch {branch_name}: {e}")
                    continue

        logger.info(f"Successfully discovered {len(branches)} branches")

        return BranchesResult(
            success=True, branches=branches, default_branch=default_branch_name
        )

    except Exception as e:
        error_msg = f"Failed to discover branches: {e}"
        logger.error(error_msg)
        return BranchesResult(
            success=False, branches=[], default_branch=None, error=error_msg
        )


def _get_default_branch(repo: pygit2.Repository) -> str:
    """Get the default branch name.

    Priority:
    1. refs/remotes/origin/HEAD (GitHub sets this to the actual default)
    2. Common default branch names that exist locally
    3. Local HEAD (what's currently checked out)
    4. First local branch
    """
    # Try origin/HEAD first - this is what GitHub sets as the default
    try:
        origin_head = repo.references.get("refs/remotes/origin/HEAD")
        if origin_head:
            # origin/HEAD is a symbolic ref like "refs/remotes/origin/main"
            target = origin_head.target
            if isinstance(target, str) and target.startswith("refs/remotes/origin/"):
                branch_name = target.replace("refs/remotes/origin/", "")
                logger.debug(f"Default branch from origin/HEAD: {branch_name}")
                return branch_name
    except Exception as e:
        logger.debug(f"Could not get origin/HEAD: {e}")

    # Fallback: check common default branch names
    for candidate in ["main", "master", "develop"]:
        if candidate in repo.branches.local:
            logger.debug(f"Default branch from common names: {candidate}")
            return candidate

    # Try local HEAD (what's checked out)
    try:
        if not repo.head_is_unborn:
            branch_name = repo.head.shorthand
            logger.debug(f"Default branch from HEAD: {branch_name}")
            return branch_name
    except Exception as e:
        logger.debug(f"Could not get HEAD: {e}")

    # Last resort: use first branch
    branches = list(repo.branches.local)
    if branches:
        logger.debug(f"Default branch from first local: {branches[0]}")
        return branches[0]

    return "main"


def _get_all_branch_names(repo: pygit2.Repository) -> list[str]:
    """Get all branch names (local + remote, deduplicated)."""
    branch_names = set()

    # Add local branches
    local_branches = list(repo.branches.local)
    logger.debug(f"Local branches in clone: {local_branches}")
    for branch_name in local_branches:
        branch_names.add(branch_name)

    # Add remote branches (strip "origin/" prefix)
    remote_branches = list(repo.branches.remote)
    logger.debug(f"Remote branches in clone: {remote_branches}")
    for remote_branch in remote_branches:
        if "/" in remote_branch:
            branch_name = remote_branch.split("/", 1)[1]
            if branch_name.upper() != "HEAD":
                branch_names.add(branch_name)

    logger.info(f"All discovered branch names: {sorted(branch_names)}")
    return sorted(branch_names)


def _analyze_branch(
    repo: pygit2.Repository, branch_name: str, default_branch_name: str
) -> BranchInfo | None:
    """Analyze a single branch and extract information."""
    # Get branch reference
    branch = None
    if branch_name in repo.branches.local:
        branch = repo.branches[branch_name]
    elif f"origin/{branch_name}" in repo.branches.remote:
        branch = repo.branches[f"origin/{branch_name}"]
    else:
        logger.warning(f"Branch not found: {branch_name}")
        return None

    head_sha = str(branch.target)

    # Get last commit time
    try:
        last_commit = repo.get(branch.target)
        last_commit_at = datetime.fromtimestamp(last_commit.commit_time, tz=UTC)
    except Exception:
        last_commit_at = datetime.now(UTC)

    # Determine parent and divergence point
    is_default = branch_name == default_branch_name

    if is_default:
        # Default branch has no parent
        parent_branch = None
        divergence_point_sha = None
        created_at = None
    else:
        # Non-default branch: find parent and divergence point
        parent_branch = default_branch_name
        divergence_point_sha = _find_divergence_point(
            repo, branch_name, default_branch_name
        )
        created_at = _get_branch_creation_time(repo, branch_name, divergence_point_sha)

    return BranchInfo(
        name=branch_name,
        head_sha=head_sha,
        divergence_point_sha=divergence_point_sha,
        parent_branch=parent_branch,
        created_at=created_at,
        last_commit_at=last_commit_at,
        is_default=is_default,
    )


def _find_divergence_point(
    repo: pygit2.Repository, branch_name: str, parent_branch: str
) -> str | None:
    """Find where branch diverged from parent using git merge-base."""
    try:
        # Get branch references
        branch = None
        if branch_name in repo.branches.local:
            branch = repo.branches[branch_name]
        elif f"origin/{branch_name}" in repo.branches.remote:
            branch = repo.branches[f"origin/{branch_name}"]

        parent = None
        if parent_branch in repo.branches.local:
            parent = repo.branches[parent_branch]
        elif f"origin/{parent_branch}" in repo.branches.remote:
            parent = repo.branches[f"origin/{parent_branch}"]

        if not branch or not parent:
            return None

        # Calculate merge base (divergence point)
        merge_base_oid = repo.merge_base(branch.target, parent.target)

        if merge_base_oid:
            return str(merge_base_oid)

        return None

    except Exception as e:
        logger.debug(f"Error finding divergence point for {branch_name}: {e}")
        return None


def _get_branch_creation_time(
    repo: pygit2.Repository, branch_name: str, divergence_sha: str | None
) -> datetime | None:
    """Estimate branch creation time from first commit after divergence."""
    if not divergence_sha:
        return None

    try:
        # Get branch reference
        branch = None
        if branch_name in repo.branches.local:
            branch = repo.branches[branch_name]
        elif f"origin/{branch_name}" in repo.branches.remote:
            branch = repo.branches[f"origin/{branch_name}"]

        if not branch:
            return None

        # Walk from branch head back to divergence point
        first_branch_commit = None

        for commit in repo.walk(branch.target, pygit2.GIT_SORT_TOPOLOGICAL):
            commit_sha = str(commit.id)

            if commit_sha == divergence_sha:
                break

            first_branch_commit = commit

        if first_branch_commit:
            return datetime.fromtimestamp(first_branch_commit.commit_time, tz=UTC)

        return None

    except Exception as e:
        logger.debug(f"Error estimating creation time for {branch_name}: {e}")
        return None
