"""
Extract phase: Extract commit data from repository using pygit2.

This phase collects commit metadata and calculates SLOC metrics.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pygit2

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from analytics.checkpoint import ExtractionCheckpoint
    from analytics.storage.chunk_storage import ChunkStorage

logger = logging.getLogger(__name__)


def _convert_rust_commits(rust_commits: list[dict]) -> list[dict]:
    """
    Convert Rust commit output to Python types expected by PyArrow.

    Rust returns ISO strings for timestamps, but PyArrow schema expects
    Python datetime/date objects.
    """
    converted = []
    for commit in rust_commits:
        c = dict(commit)  # Make a copy

        # Convert timestamp strings to datetime objects
        # Guard against empty strings from Rust defaults
        committed_at = c.get("committed_at")
        if isinstance(committed_at, str) and committed_at:
            c["committed_at"] = datetime.fromisoformat(
                committed_at.replace("Z", "+00:00")
            )

        collected_at = c.get("collected_at")
        if isinstance(collected_at, str) and collected_at:
            c["collected_at"] = datetime.fromisoformat(
                collected_at.replace("Z", "+00:00")
            )

        # Convert date string to date object
        commit_date = c.get("commit_date")
        if isinstance(commit_date, str) and commit_date:
            c["commit_date"] = date.fromisoformat(commit_date)

        converted.append(c)

    return converted


def _convert_rust_file_changes(rust_file_changes: list[dict]) -> list[dict]:
    """
    Convert Rust file change output to Python types expected by PyArrow.

    Rust returns date as string, but PyArrow schema expects date object.
    """
    converted = []
    for fc in rust_file_changes:
        f = dict(fc)  # Make a copy

        # Convert date string to date object
        # Guard against empty strings from Rust defaults
        commit_date = f.get("commit_date")
        if isinstance(commit_date, str) and commit_date:
            f["commit_date"] = date.fromisoformat(commit_date)

        converted.append(f)

    return converted


def get_parallel_workers() -> int:
    """
    Get number of parallel workers for commit processing.

    Returns 7 by default (for 8 vCPU workers, leaving 1 for system overhead).
    Can be overridden via ANALYTICS_PARALLEL_WORKERS environment variable.
    """
    try:
        env_value = os.environ.get("ANALYTICS_PARALLEL_WORKERS")
        if env_value is not None:
            workers = max(1, int(env_value))
            logger.info(
                f"Using {workers} workers from ANALYTICS_PARALLEL_WORKERS env var"
            )
            return workers
    except (ValueError, TypeError):
        pass

    logger.info("Using 7 workers (default)")
    return 7


def create_chunk_callback(
    s3_client: "Any",
    bucket: str,
    codebase_id: str,
    initial_commit_chunk_count: int = 0,
    initial_file_change_chunk_count: int = 0,
    initial_processed_shas: set[str] | None = None,
) -> tuple["Callable[[list[str], list[dict], list[dict]], dict]", "ChunkStorage"]:
    """Create a chunk callback for incremental S3 writes during extraction.

    This replaces the in-memory accumulation approach that caused OOM on large repos.
    Instead of accumulating all records in Python lists, each batch is written
    directly to S3 as a Parquet chunk.

    Args:
        s3_client: boto3 S3 client
        bucket: S3 bucket name
        codebase_id: Codebase identifier
        initial_commit_chunk_count: Resume from this commit chunk index
        initial_file_change_chunk_count: Resume from this file change chunk index
        initial_processed_shas: Set of already-processed SHAs from checkpoint

    Returns:
        Tuple of (callback_function, ChunkStorage instance)
        The callback returns a dict with current chunk state for checkpointing.
    """
    from analytics.storage.chunk_storage import ChunkStorage

    storage = ChunkStorage(s3_client, bucket, codebase_id)

    # Mutable state for tracking across batches
    commit_chunk_index = initial_commit_chunk_count
    file_change_chunk_index = initial_file_change_chunk_count
    processed_shas: set[str] = (
        set(initial_processed_shas) if initial_processed_shas else set()
    )

    def chunk_callback(
        batch_shas: list[str],
        batch_commits: list[dict],
        batch_file_changes: list[dict],
    ) -> dict:
        """Write batch to S3 chunks instead of accumulating in memory.

        Args:
            batch_shas: List of commit SHAs processed in this batch
            batch_commits: List of commit records (already converted to Python types)
            batch_file_changes: List of file change records

        Returns:
            Dict with current chunk state for checkpointing:
            - commit_chunk_count: Current commit chunk index
            - file_change_chunk_count: Current file change chunk index
            - processed_commit_shas: Set of all processed commit SHAs
        """
        nonlocal commit_chunk_index, file_change_chunk_index

        # Write commits chunk if any
        if batch_commits:
            storage.write_commit_chunk(batch_commits, commit_chunk_index)
            commit_chunk_index += 1

        # Write file changes chunk if any
        if batch_file_changes:
            storage.write_file_change_chunk(batch_file_changes, file_change_chunk_index)
            file_change_chunk_index += 1

        # Update processed SHAs for any batch with commits
        if batch_shas:
            processed_shas.update(batch_shas)

        # Return current state for checkpoint callback to use
        return {
            "commit_chunk_count": commit_chunk_index,
            "file_change_chunk_count": file_change_chunk_index,
            "processed_commit_shas": processed_shas.copy(),
        }

    return chunk_callback, storage


@dataclass
class ExtractResult:
    """Result of commit extraction."""

    success: bool
    commits: list[dict]
    total_commits: int
    error: str | None = None
    file_changes: list[dict] = field(default_factory=list)


def extract_commits(
    repo: pygit2.Repository,
    codebase_id: str,
    branch_names: list[str] | None = None,
    include_patches: bool = True,
    since_sha: str | None = None,
    include_file_changes: bool = False,
    checkpoint: "ExtractionCheckpoint | None" = None,
    checkpoint_callback: "Callable[[dict], None] | None" = None,
    checkpoint_interval_secs: float = 30.0,
    batch_callback: "Callable[[list[str], list[dict], list[dict]], None] | None" = None,
) -> ExtractResult:
    """
    Extract commits from repository.

    Args:
        repo: pygit2.Repository instance
        codebase_id: Codebase UUID
        branch_names: List of branches to extract from (None = all)
        include_patches: Whether to include patch data for SLOC
        since_sha: Only extract commits after this SHA (for incremental updates)
        include_file_changes: Whether to extract file-level changes (for cold storage)
        checkpoint: Optional checkpoint to resume from
        checkpoint_callback: Optional callback for checkpoint data (called at intervals)
        checkpoint_interval_secs: How often to call checkpoint callback (default 30s)
        batch_callback: v2.0 callback for writing batches to S3 chunks. When provided,
            records are written to S3 instead of accumulated in memory, preventing OOM
            on large repositories. Signature: (batch_shas, batch_commits, batch_file_changes)

    Returns:
        ExtractResult with commit data and optionally file changes.
        When batch_callback is provided (v2.0 mode), commits/file_changes are empty
        since records are written to S3 chunks via the callback.
    """
    logger.info(f"Extracting commits for codebase {codebase_id}")

    try:
        # Get branches to process
        if branch_names is None:
            branch_names = _get_all_branch_names(repo)

        logger.info(f"Processing {len(branch_names)} branches: {branch_names[:5]}...")

        # OPTIMIZED: Single pass through commit graph collects both:
        # 1. Commits in topological order (for tree size calculation)
        # 2. Inverted index of commit -> branches (for O(1) lookup)
        # This replaces the old two-pass approach that walked the graph twice.
        commits_topo, commit_to_branches = _collect_commits_and_branches(
            repo, branch_names
        )
        all_sha_set = set(commit_to_branches.keys())

        total_before_filter = len(all_sha_set)
        logger.info(f"Found {total_before_filter} unique commits")

        # Filter by since_sha for incremental updates
        if since_sha:
            all_sha_set = _filter_commits_since(repo, all_sha_set, since_sha)
            # Filter commits_topo and commit_to_branches to only include new commits
            commits_topo = [c for c in commits_topo if str(c.id) in all_sha_set]
            commit_to_branches = {
                sha: branches
                for sha, branches in commit_to_branches.items()
                if sha in all_sha_set
            }
            logger.info(
                f"Incremental: {len(all_sha_set)} new commits (filtered {total_before_filter - len(all_sha_set)} old)"
            )

        # Pre-calculate tree sizes using incremental method (much faster)
        # Pass pre-sorted commits to avoid duplicate graph walk
        logger.info("Pre-calculating tree sizes (incremental)...")

        # Use checkpoint's cached tree sizes if resuming
        initial_results = None
        if checkpoint and checkpoint.tree_size_cache:
            initial_results = checkpoint.tree_size_cache
            logger.info(
                f"Resuming with {len(initial_results)} cached tree sizes "
                f"from checkpoint (commit {checkpoint.last_processed_index + 1}/{checkpoint.commits_total})"
            )

        tree_size_cache = _calculate_tree_sizes_incremental(
            repo,
            commit_shas=None,
            commits_topo=commits_topo,
            initial_results=initial_results,
            checkpoint_callback=checkpoint_callback,
            checkpoint_interval_secs=checkpoint_interval_secs,
            codebase_id=codebase_id,
        )
        logger.info(f"Calculated tree sizes for {len(tree_size_cache)} commits")

        # Process commits in parallel
        collected_at = datetime.now(UTC)
        all_commits = []
        all_file_changes = []

        # Get parallelization settings
        workers = get_parallel_workers()
        commit_shas = list(all_sha_set)
        total = len(commit_shas)

        logger.info(f"Processing {total} commits with {workers} parallel workers")

        # Get checkpoint data for commit processing resume
        skip_shas: set[str] | None = None
        initial_commits: list[dict] = []
        initial_file_changes: list[dict] = []
        if checkpoint and checkpoint.processed_commit_shas:
            skip_shas = checkpoint.processed_commit_shas
            # v2.0: Records are stored in S3 chunks, not in checkpoint
            # On resume, we skip already-processed commits and continue writing new chunks
            # The chunks will be merged at the end of extraction
            logger.info(
                f"Resuming commit processing with {len(skip_shas)} already-processed commits"
            )

        # Create commit processing checkpoint callback if main callback provided
        commit_checkpoint_callback = None
        if checkpoint_callback:

            def commit_checkpoint_callback(data: dict) -> None:
                """Merge commit processing checkpoint with tree size checkpoint."""
                # Build combined checkpoint data (v2.0: no commit_records/file_change_records)
                combined_data = {
                    "codebase_id": codebase_id,
                    "commits_total": len(commits_topo),
                    "commits_processed": len(commits_topo),  # Tree sizes done
                    "last_processed_index": len(commits_topo) - 1,
                    "last_processed_sha": str(commits_topo[-1].id)
                    if commits_topo
                    else "",
                    "tree_size_cache": tree_size_cache,
                    # v2.0: Commit processing uses chunk counts, not in-memory records
                    "processed_commit_shas": data["processed_commit_shas"],
                    "commit_chunk_count": data.get("commit_chunk_count", 0),
                    "file_change_chunk_count": data.get("file_change_chunk_count", 0),
                }
                checkpoint_callback(combined_data)

        # Process commits in parallel using thread pool
        new_commits, new_file_changes = _process_commits_parallel(
            repo_path=repo.path,
            commit_shas=commit_shas,
            codebase_id=codebase_id,
            commit_to_branches=commit_to_branches,
            collected_at=collected_at,
            include_patches=include_patches,
            include_file_changes=include_file_changes,
            tree_size_cache=tree_size_cache,
            workers=workers,
            skip_shas=skip_shas,
            checkpoint_callback=commit_checkpoint_callback,
            initial_commits=initial_commits,
            initial_file_changes=initial_file_changes,
            batch_callback=batch_callback,
        )

        # Combine with initial results from checkpoint
        all_commits = initial_commits + new_commits
        all_file_changes = initial_file_changes + new_file_changes

        logger.info(f"Successfully extracted {len(all_commits)} commit records")
        if include_file_changes:
            logger.info(f"Extracted {len(all_file_changes)} file change records")

        return ExtractResult(
            success=True,
            commits=all_commits,
            total_commits=len(all_sha_set),
            file_changes=all_file_changes if include_file_changes else [],
        )

    except Exception as e:
        error_msg = f"Failed to extract commits: {e}"
        logger.error(error_msg)
        return ExtractResult(
            success=False, commits=[], total_commits=0, error=error_msg
        )


def _process_commits_parallel(
    repo_path: str,
    commit_shas: list[str],
    codebase_id: str,
    commit_to_branches: dict[str, list[str]],
    collected_at: datetime,
    include_patches: bool,
    include_file_changes: bool,
    tree_size_cache: dict[str, tuple[int, int]],
    workers: int,
    skip_shas: set[str] | None = None,
    checkpoint_callback: "Callable[[dict], None] | None" = None,
    initial_commits: list[dict] | None = None,
    initial_file_changes: list[dict] | None = None,
    batch_callback: "Callable[[list[str], list[dict], list[dict]], None] | None" = None,
) -> tuple[list[dict], list[dict]]:
    """
    Process commits in parallel using native Rust extension or Python fallback.

    Uses native Rust extension when available (much faster for large repos),
    falls back to Python ThreadPoolExecutor if not installed.

    Supports checkpointing for fault tolerance on large repositories.

    v2.0: If batch_callback is provided, each batch is passed to the callback
    (e.g., for writing to S3 chunks) instead of accumulating in memory.

    Args:
        repo_path: Path to the git repository
        commit_shas: List of commit SHAs to process
        codebase_id: Codebase UUID
        commit_to_branches: Mapping from commit SHA to branches (read-only, shared)
        collected_at: Timestamp for collection
        include_patches: Whether to include patch data
        include_file_changes: Whether to extract file-level changes
        tree_size_cache: Pre-computed tree sizes (read-only, shared)
        workers: Number of parallel workers
        skip_shas: Set of SHAs to skip (already processed from checkpoint)
        checkpoint_callback: Callback for checkpointing after each batch
        initial_commits: Commits from checkpoint to include in checkpoint saves
        initial_file_changes: File changes from checkpoint to include in saves

    Returns:
        Tuple of (all_commits, all_file_changes)
    """
    total = len(commit_shas)

    # Try native Rust extension first (much faster)
    try:
        from analytics_native import CommitInput
        from analytics_native import process_commits_parallel as native_process

        logger.info(f"Using native Rust commit processing for {total} commits")

        # Build CommitInput list with pre-computed tree sizes
        commit_inputs = []
        for sha in commit_shas:
            branches = commit_to_branches.get(sha, [])
            tree_size = tree_size_cache.get(sha, (0, 0))
            # Defensive conversion: ensure tree_bytes and tree_lines are integers
            # This handles edge cases where JSON deserialization might produce
            # unexpected types (e.g., strings from malformed checkpoints)
            try:
                tree_bytes = int(tree_size[0])
                tree_lines = int(tree_size[1])
            except (TypeError, ValueError, IndexError) as e:
                logger.warning(
                    f"Invalid tree_size for {sha[:8]}: {tree_size!r} ({type(tree_size).__name__}), "
                    f"using defaults. Error: {e}"
                )
                tree_bytes, tree_lines = 0, 0
            commit_inputs.append(CommitInput(sha, branches, tree_bytes, tree_lines))

        # Create Rust callback wrapper if checkpoint callback or batch_callback provided
        rust_callback = None
        if checkpoint_callback or batch_callback:
            import time

            # Track accumulated results across batches for checkpoint
            # Initialize with prior checkpoint data to preserve across restarts
            accumulated_shas: set[str] = set(skip_shas) if skip_shas else set()
            # Only accumulate in memory if NOT using batch_callback (v1.x mode)
            accumulated_commits: list[dict] = (
                list(initial_commits or []) if not batch_callback else []
            )
            accumulated_file_changes: list[dict] = (
                list(initial_file_changes or []) if not batch_callback else []
            )
            commit_start_time = time.time()
            initial_count = len(accumulated_shas)

            def rust_callback(
                batch_shas: list[str],
                batch_commits: list[dict],
                batch_file_changes: list[dict],
            ) -> None:
                """Transform Rust batch callback to checkpoint format or S3 chunk writes."""
                nonlocal commit_start_time, initial_count

                # Convert Rust output to Python types
                converted_commits = _convert_rust_commits(batch_commits)
                converted_file_changes = _convert_rust_file_changes(batch_file_changes)

                # v2.0: If batch_callback provided, write to S3 chunks instead of memory
                # batch_callback returns chunk state for checkpointing
                chunk_state = None
                if batch_callback:
                    chunk_state = batch_callback(
                        batch_shas, converted_commits, converted_file_changes
                    )
                else:
                    # v1.x mode: Accumulate in memory
                    accumulated_commits.extend(converted_commits)
                    accumulated_file_changes.extend(converted_file_changes)

                # Always track processed SHAs for checkpoint/progress
                accumulated_shas.update(batch_shas)

                # Log progress
                processed = len(accumulated_shas)
                new_processed = processed - initial_count
                pct = (processed / total) * 100 if total > 0 else 0
                elapsed = time.time() - commit_start_time
                rate = new_processed / elapsed if elapsed > 0 else 0
                remaining = total - processed
                eta = remaining / rate if rate > 0 else 0
                logger.info(
                    f"Commit processing progress: {processed}/{total} ({pct:.1f}%) | "
                    f"{rate:.0f} commits/sec | ETA: {eta:.0f}s"
                )

                # Call checkpoint callback if provided (for fault tolerance)
                if checkpoint_callback:
                    # Build checkpoint data with chunk state from batch_callback (v2.0)
                    # or use accumulated state for v1.x mode
                    if chunk_state:
                        processed_shas = chunk_state.get(
                            "processed_commit_shas", accumulated_shas.copy()
                        )
                        commit_chunks = chunk_state.get("commit_chunk_count", 0)
                        file_chunks = chunk_state.get("file_change_chunk_count", 0)
                    else:
                        processed_shas = accumulated_shas.copy()
                        commit_chunks = 0
                        file_chunks = 0

                    checkpoint_callback(
                        {
                            "codebase_id": codebase_id,
                            "processed_commit_shas": processed_shas,
                            "commit_chunk_count": commit_chunks,
                            "file_change_chunk_count": file_chunks,
                        }
                    )

        # Call native Rust implementation with checkpoint support
        rust_commits, rust_file_changes = native_process(
            repo_path,
            commit_inputs,
            codebase_id,
            collected_at.timestamp(),
            include_file_changes,
            set(skip_shas) if skip_shas else None,
            rust_callback,
            workers,
        )

        # v2.0: When batch_callback is provided, all records are written to S3 chunks
        # via the callback. Return empty lists as records aren't accumulated in memory.
        if batch_callback:
            logger.info(
                "Native commit processing complete (v2.0 chunks mode - records in S3)"
            )
            return [], []

        # v1.x mode: Convert Rust output (ISO strings) to Python datetime/date objects
        # PyArrow schema expects datetime objects, not strings
        all_commits = _convert_rust_commits(rust_commits)
        all_file_changes = _convert_rust_file_changes(rust_file_changes)

        logger.info(f"Native commit processing complete: {len(all_commits)} records")
        return all_commits, all_file_changes

    except ImportError:
        logger.info("Native extension not available, using Python implementation")
        return _process_commits_parallel_python(
            repo_path,
            commit_shas,
            codebase_id,
            commit_to_branches,
            collected_at,
            include_patches,
            include_file_changes,
            tree_size_cache,
            workers,
            skip_shas=skip_shas,
        )


def _process_commits_parallel_python(
    repo_path: str,
    commit_shas: list[str],
    codebase_id: str,
    commit_to_branches: dict[str, list[str]],
    collected_at: datetime,
    include_patches: bool,
    include_file_changes: bool,
    tree_size_cache: dict[str, tuple[int, int]],
    workers: int,
    skip_shas: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Python fallback for commit processing using ThreadPoolExecutor.

    Each worker thread creates its own pygit2.Repository instance for thread safety.
    Results are collected and flattened in the original order.

    Args:
        skip_shas: Set of SHAs to skip (already processed from checkpoint)
    """
    # Filter out already-processed commits from checkpoint
    if skip_shas:
        commit_shas = [sha for sha in commit_shas if sha not in skip_shas]
        logger.info(f"Skipping {len(skip_shas)} already-processed commits")

    total = len(commit_shas)

    def process_single_commit(commit_sha: str) -> tuple[list[dict], list[dict]]:
        """Process a single commit in a worker thread."""
        # Each thread creates its own Repository instance for thread safety
        thread_repo = pygit2.Repository(repo_path)

        try:
            commit_branches = commit_to_branches.get(commit_sha, [])

            commit_data, diff = _extract_commit_data_with_diff(
                thread_repo,
                commit_sha,
                codebase_id,
                commit_branches,
                collected_at,
                include_patches,
                tree_size_cache,
            )

            file_changes = []
            if include_file_changes and diff and commit_data:
                commit_time = datetime.fromtimestamp(
                    thread_repo.get(commit_sha).commit_time, tz=UTC
                )
                file_changes = _extract_file_changes(
                    diff, commit_sha, codebase_id, commit_time.date()
                )

            return commit_data, file_changes

        except Exception as e:
            logger.warning(f"Error processing commit {commit_sha[:8]}: {e}")
            return [], []

    # Process in batches for progress reporting
    all_commits = []
    all_file_changes = []
    batch_size = 1000
    processed = 0

    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch = commit_shas[batch_start:batch_end]

        # Process batch in parallel
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(process_single_commit, batch))

        # Collect results
        for commit_data, file_changes in results:
            all_commits.extend(commit_data)
            all_file_changes.extend(file_changes)

        processed += len(batch)
        if processed % 1000 == 0 or processed == total:
            logger.info(f"Processing commits: {processed}/{total}")

    return all_commits, all_file_changes


def _get_all_branch_names(repo: pygit2.Repository) -> list[str]:
    """Get all branch names (local + remote, deduplicated)."""
    branch_names = set()

    # Add local branches
    for branch_name in repo.branches.local:
        branch_names.add(branch_name)

    # Add remote branches (strip "origin/" prefix)
    for remote_branch in repo.branches.remote:
        if "/" in remote_branch:
            branch_name = remote_branch.split("/", 1)[1]
            if branch_name.upper() != "HEAD":
                branch_names.add(branch_name)

    return sorted(branch_names)


def _collect_commits_and_branches(
    repo: pygit2.Repository,
    branch_names: list[str],
) -> tuple[list[pygit2.Commit], dict[str, list[str]]]:
    """
    Single pass through commit graph that collects:
    1. All commits in topological order (parents before children)
    2. Mapping from commit SHA to list of branches containing it

    This single-pass approach reduces commit graph traversal complexity
    and provides better cache locality.

    Additionally, the returned commit_to_branches dict enables O(1) branch lookup
    instead of O(branches) per commit.

    Returns:
        Tuple of (commits_topo, commit_to_branches)
        - commits_topo: List of commits in topological order
        - commit_to_branches: Dict mapping commit_sha -> list of branch names
    """
    from collections import defaultdict

    commits_topo: list[pygit2.Commit] = []
    seen_for_topo: set[str] = set()
    commit_to_branches: dict[str, list[str]] = defaultdict(list)

    for branch_idx, branch_name in enumerate(branch_names):
        logger.info(
            f"Walking branch {branch_idx + 1}/{len(branch_names)}: {branch_name} "
            f"({len(seen_for_topo)} unique commits so far)"
        )

        # Get branch reference (local first, then remote)
        branch_ref = None
        if branch_name in repo.branches.local:
            branch_ref = repo.branches[branch_name]
        elif f"origin/{branch_name}" in repo.branches.remote:
            branch_ref = repo.branches[f"origin/{branch_name}"]

        if not branch_ref:
            logger.warning(f"Branch not found: {branch_name}")
            continue

        try:
            head_oid = branch_ref.peel().id
            flags = pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE

            for branch_commit_count, commit in enumerate(
                repo.walk(head_oid, flags), start=1
            ):
                sha = str(commit.id)

                # Track branch membership (always, even if seen before)
                commit_to_branches[sha].append(branch_name)

                # Add to topological list only once
                if sha not in seen_for_topo:
                    seen_for_topo.add(sha)
                    commits_topo.append(commit)

                if branch_commit_count % 10000 == 0:
                    logger.info(
                        f"  ... walked {branch_commit_count} commits on {branch_name}"
                    )

        except Exception as e:
            logger.warning(f"Error walking branch {branch_name}: {e}")
            continue

    return commits_topo, dict(commit_to_branches)


def _filter_commits_since(
    repo: pygit2.Repository, all_shas: set[str], since_sha: str
) -> set[str]:
    """Filter commits to only those after since_sha.

    Uses git ancestry to determine which commits are new.
    If since_sha is not found, returns all commits (fallback to full rebuild).

    Args:
        repo: pygit2.Repository instance
        all_shas: Set of all commit SHAs to filter
        since_sha: Only include commits after this SHA

    Returns:
        Set of commit SHAs that are newer than since_sha
    """
    try:
        # Try to find the since_sha commit
        since_commit = repo.get(since_sha)
        if not since_commit:
            logger.warning(
                f"since_sha {since_sha[:8]} not found, extracting all commits"
            )
            return all_shas

        # Get all commits reachable from since_sha (these are the "old" commits)
        old_shas = set()
        try:
            for commit in repo.walk(since_commit.id, pygit2.GIT_SORT_TOPOLOGICAL):
                old_shas.add(str(commit.id))
        except Exception as e:
            logger.warning(f"Error walking from since_sha: {e}")
            return all_shas

        # New commits = all commits - old commits (including since_sha itself)
        new_shas = all_shas - old_shas

        logger.debug(
            f"Filtered: {len(new_shas)} new commits, {len(old_shas)} old commits"
        )
        return new_shas

    except Exception as e:
        logger.warning(f"Error filtering commits since {since_sha[:8]}: {e}")
        return all_shas  # Fallback to all


# Map pygit2 status chars to our change types
STATUS_CHAR_TO_CHANGE_TYPE = {
    "A": "added",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "C": "copied",
    "T": "typechange",
}


def _extract_file_changes(
    diff: pygit2.Diff,
    commit_sha: str,
    codebase_id: str,
    commit_date: date | None,
) -> list[dict]:
    """Extract file-level changes from a diff.

    Only includes ANALYZABLE code files (matching inspector criteria).
    Docs, config, and other non-code files are excluded.

    Args:
        diff: pygit2.Diff object
        commit_sha: SHA of the commit
        codebase_id: Codebase UUID
        commit_date: Date of the commit (date object)

    Returns:
        List of file change dictionaries matching FILE_CHANGES_SCHEMA
    """
    from analytics.utils.file_filter import is_analyzable_path

    file_changes = []

    if not diff:
        return file_changes

    try:
        for patch in diff:
            delta = patch.delta

            # Determine file path (new path for adds/modifies, old for deletes)
            new_path = delta.new_file.path if delta.new_file.path else ""
            old_path = delta.old_file.path if delta.old_file.path else ""
            file_path = new_path or old_path

            if not file_path:
                continue

            # Skip non-analyzable files (docs, config, binary, etc.)
            if not is_analyzable_path(file_path):
                continue

            # Get change type from status char
            try:
                status_char = delta.status_char()
                change_type = STATUS_CHAR_TO_CHANGE_TYPE.get(status_char, "modified")
            except Exception:
                change_type = "modified"

            # Get line stats
            try:
                _, additions, deletions = patch.line_stats
            except Exception:
                additions = 0
                deletions = 0

            # Calculate bytes from patch text
            addition_bytes = 0
            deletion_bytes = 0
            has_patch_data = False

            try:
                patch_text = patch.text
                if patch_text:
                    has_patch_data = True
                    for line in patch_text.split("\n"):
                        if line.startswith("+") and not line.startswith("+++"):
                            addition_bytes += len(
                                line[1:].encode("utf-8", errors="replace")
                            )
                        elif line.startswith("-") and not line.startswith("---"):
                            deletion_bytes += len(
                                line[1:].encode("utf-8", errors="replace")
                            )
            except Exception as e:
                logger.debug(f"Could not parse patch text for {file_path}: {e}")

            # Extract file extension
            file_extension = None
            if "." in file_path:
                file_extension = "." + file_path.rsplit(".", 1)[-1]

            # Calculate SLOC for this file (bytes / 50)
            file_sloc = (addition_bytes + deletion_bytes) // 50

            # Detect language from file path (A11)
            from analytics.aggregation.language import _get_language_from_path

            file_language = _get_language_from_path(file_path)

            file_change = {
                "codebase_id": codebase_id,
                "commit_sha": commit_sha,
                "file_path": file_path,
                "commit_date": commit_date,
                "commit_year_month": commit_date.strftime("%Y-%m"),
                "change_type": change_type,
                "previous_path": old_path if change_type == "renamed" else None,
                "additions_lines": additions,
                "deletions_lines": deletions,
                "changes_lines": additions + deletions,
                "addition_bytes": addition_bytes,
                "deletion_bytes": deletion_bytes,
                "file_sloc": file_sloc,
                "file_extension": file_extension,
                "file_language": file_language,
                "has_patch_data": has_patch_data,
                "patch_blob_key": None,  # Reserved for future patch storage
            }

            file_changes.append(file_change)

    except Exception as e:
        logger.warning(f"Error extracting file changes for {commit_sha[:8]}: {e}")

    return file_changes


def _extract_commit_data_with_diff(
    repo: pygit2.Repository,
    commit_sha: str,
    codebase_id: str,
    branches: list[str],
    collected_at: datetime,
    include_patches: bool,
    tree_size_cache: dict[str, tuple[int, int]] | None = None,
) -> tuple[list[dict], pygit2.Diff | None]:
    """
    Extract data for a single commit and return the diff.

    Metrics are filtered to only include ANALYZABLE code files (matching
    inspector criteria). This ensures additions/deletions/churn metrics
    are comparable to current_sloc from tree walks.

    Args:
        tree_size_cache: Pre-computed tree sizes from incremental calculation

    Returns:
        Tuple of (commit records, diff object)
        - One commit record per branch the commit belongs to
        - The diff object for file-level extraction
    """
    from analytics.utils.file_filter import is_analyzable_path

    try:
        commit = repo.get(commit_sha)
        if not commit:
            return [], None

        # Get diff from parent
        diff = _get_commit_diff(repo, commit)

        # Calculate line/byte metrics from diff
        # FILTERED to only include analyzable code files (matching inspector criteria)
        files_changed = 0
        total_additions = 0
        total_deletions = 0
        total_addition_bytes = 0
        total_deletion_bytes = 0

        if diff:
            # Iterate over individual patches and filter to analyzable files
            # (cannot use diff.stats as it includes all files)
            for patch in diff:
                delta = patch.delta
                file_path = delta.new_file.path or delta.old_file.path

                # Skip non-analyzable files (docs, config, binary, etc.)
                if not file_path or not is_analyzable_path(file_path):
                    continue

                # Count this file
                files_changed += 1

                # Get line stats for this file
                try:
                    _, additions, deletions = patch.line_stats
                    total_additions += additions
                    total_deletions += deletions
                except Exception:
                    pass

                # Get bytes from patch content for this file
                try:
                    patch_text = patch.text
                    if patch_text:
                        for line in patch_text.split("\n"):
                            if line.startswith("+") and not line.startswith("+++"):
                                total_addition_bytes += len(
                                    line[1:].encode("utf-8", errors="replace")
                                )
                            elif line.startswith("-") and not line.startswith("---"):
                                total_deletion_bytes += len(
                                    line[1:].encode("utf-8", errors="replace")
                                )
                except Exception:
                    pass

        # Calculate derived metrics
        net_lines = total_additions - total_deletions
        churn_lines = total_additions + total_deletions
        patch_bytes = total_addition_bytes + total_deletion_bytes
        net_bytes = total_addition_bytes - total_deletion_bytes
        sloc = patch_bytes // 50
        bytes_per_line = patch_bytes / churn_lines if churn_lines > 0 else 0.0

        # Categorize commit size
        if churn_lines < 10:
            size_category = "tiny"
        elif churn_lines < 50:
            size_category = "small"
        elif churn_lines < 200:
            size_category = "medium"
        else:
            size_category = "large"

        # Get commit timestamp
        commit_time = datetime.fromtimestamp(commit.commit_time, tz=UTC)

        # Calculate actual codebase size at this commit
        # Uses cached incremental values if available, otherwise full tree walk
        tree_bytes, tree_lines = _get_tree_size_at_commit(repo, commit, tree_size_cache)
        tree_sloc = tree_bytes // 50  # Same conversion factor as Driver

        # Create base commit record
        base_record = {
            "commit_sha": commit_sha,
            "codebase_id": codebase_id,
            "committed_at": commit_time,
            "collected_at": collected_at,
            "commit_date": commit_time.date(),
            "commit_year": commit_time.year,
            "commit_month": commit_time.month,
            "commit_day": commit_time.day,
            "author_email": commit.author.email,
            "author_name": commit.author.name,
            "committer_email": commit.committer.email,
            "committer_name": commit.committer.name,
            "message": commit.message[:1000] if commit.message else "",
            "message_length": len(commit.message) if commit.message else 0,
            "parent_count": len(commit.parents),
            "is_merge_commit": len(commit.parents) > 1,
            "files_changed": files_changed,
            "additions_lines": total_additions,
            "deletions_lines": total_deletions,
            "net_lines": net_lines,
            "churn_lines": churn_lines,
            "addition_bytes": total_addition_bytes,
            "deletion_bytes": total_deletion_bytes,
            "patch_bytes": patch_bytes,
            "net_bytes": net_bytes,
            "sloc": sloc,
            "bytes_per_line": bytes_per_line,
            "commit_size_category": size_category,
            "is_refactor": total_additions > 0
            and total_deletions > 0
            and abs(net_lines) < churn_lines * 0.1,
            # Tree-based metrics (actual codebase size at this commit)
            "tree_bytes": tree_bytes,
            "tree_lines": tree_lines,
            "tree_sloc": tree_sloc,
            "collection_version": "2.0",
        }

        # Create one record per branch
        records = []
        for branch in branches:
            record = base_record.copy()
            record["branch_name"] = branch
            records.append(record)

        return records, diff

    except Exception as e:
        logger.warning(f"Error extracting commit {commit_sha[:8]}: {e}")
        return [], None


def _get_commit_diff(
    repo: pygit2.Repository, commit: pygit2.Commit
) -> pygit2.Diff | None:
    """Get diff for a commit compared to its parent."""
    try:
        if commit.parents:
            # Normal commit: diff against first parent
            parent = commit.parents[0]
            return repo.diff(parent.tree, commit.tree)
        else:
            # First commit: diff against empty tree
            tree_builder = repo.TreeBuilder()
            empty_tree_oid = tree_builder.write()
            empty_tree = repo.get(empty_tree_oid)
            return repo.diff(empty_tree, commit.tree)
    except Exception as e:
        logger.debug(f"Error getting diff for commit {commit.id}: {e}")
        return None


def _get_tree_size_at_commit(
    repo: pygit2.Repository,
    commit: pygit2.Commit,
    tree_size_cache: dict[str, tuple[int, int]] | None = None,
) -> tuple[int, int]:
    """
    Calculate the total size of ANALYZABLE code at a given commit.

    If tree_size_cache is provided and contains this commit, returns cached value.
    Otherwise falls back to full tree walk.

    Args:
        repo: pygit2.Repository instance
        commit: pygit2.Commit to analyze
        tree_size_cache: Optional pre-computed cache from incremental calculation

    Returns:
        Tuple of (total_bytes, total_lines) for analyzable code files in the tree
    """
    commit_sha = str(commit.id)

    # Use cache if available
    if tree_size_cache and commit_sha in tree_size_cache:
        return tree_size_cache[commit_sha]

    # Fall back to full tree walk
    total_bytes = 0
    total_lines = 0

    try:
        tree = commit.tree
        if not tree:
            return (0, 0)

        total_bytes, total_lines = _walk_tree_recursive(repo, tree, path_parts=())

    except Exception as e:
        logger.warning(f"Error calculating tree size for commit {commit.id}: {e}")
        return (0, 0)

    return (total_bytes, total_lines)


def _walk_tree_recursive(
    repo: pygit2.Repository, tree: pygit2.Tree, path_parts: tuple[str, ...] = ()
) -> tuple[int, int]:
    """
    Recursively walk a tree and sum up file sizes for ANALYZABLE files only.

    Files are filtered to match inspector's is_analyzable criteria:
    - Only recognized code languages
    - Excludes binary, hex, blacklisted files
    - Excludes documentation and config files

    Args:
        repo: pygit2.Repository instance
        tree: pygit2.Tree to walk
        path_parts: Current path as tuple of directory names (for blacklist checking)

    Returns:
        Tuple of (total_bytes, total_lines) for analyzable files only
    """
    from analytics.utils.file_filter import is_analyzable_file

    total_bytes = 0
    total_lines = 0

    for entry in tree:
        try:
            current_path = (*path_parts, entry.name)

            if entry.type_str == "blob":
                # It's a file - get the blob
                blob = repo.get(entry.id)
                if blob is None:
                    continue

                # Get file info for filtering
                filename = entry.name
                extension = Path(filename).suffix
                is_binary = blob.is_binary

                # Get content for hex detection (only for non-binary files)
                content = None if is_binary else blob.data

                # Check if file is analyzable (matches inspector criteria)
                if not is_analyzable_file(
                    path_parts=current_path,
                    filename=filename,
                    extension=extension,
                    is_binary=is_binary,
                    content=content,
                ):
                    continue

                # Count this file
                data = blob.data
                total_bytes += len(data)
                # Count lines: number of newlines + 1 for last line without newline
                if data:
                    total_lines += data.count(b"\n")
                    # Add 1 for the last line if it doesn't end with newline
                    if not data.endswith(b"\n"):
                        total_lines += 1

            elif entry.type_str == "tree":
                # It's a subdirectory - recurse with updated path
                subtree = repo.get(entry.id)
                if subtree:
                    sub_bytes, sub_lines = _walk_tree_recursive(
                        repo, subtree, path_parts=current_path
                    )
                    total_bytes += sub_bytes
                    total_lines += sub_lines

        except Exception as e:
            logger.debug(f"Error processing tree entry {entry.name}: {e}")
            continue

    return (total_bytes, total_lines)


# =============================================================================
# Incremental Tree Size Calculation
# =============================================================================


def _get_commits_topological(
    repo: pygit2.Repository, commit_shas: set[str]
) -> list[pygit2.Commit]:
    """
    Sort commits topologically so parents are processed before children.

    This ordering is essential for incremental calculation since we need
    parent tree sizes to compute child tree sizes.
    """
    seen = set()
    result = []

    # Walk from all branch heads to collect commits in reverse topological order
    for branch in repo.branches.local:
        try:
            branch_ref = repo.branches[branch]
            head_oid = branch_ref.peel().id
            flags = pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE
            for commit in repo.walk(head_oid, flags):
                sha = str(commit.id)
                if sha in commit_shas and sha not in seen:
                    seen.add(sha)
                    result.append(commit)
        except Exception:
            continue

    # Also check remote branches
    for remote_branch in repo.branches.remote:
        try:
            branch_ref = repo.branches[remote_branch]
            head_oid = branch_ref.peel().id
            flags = pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE
            for commit in repo.walk(head_oid, flags):
                sha = str(commit.id)
                if sha in commit_shas and sha not in seen:
                    seen.add(sha)
                    result.append(commit)
        except Exception:
            continue

    return result


def _calculate_tree_sizes_incremental(
    repo: pygit2.Repository,
    commit_shas: set[str] | None = None,
    commits_topo: list[pygit2.Commit] | None = None,
    initial_results: dict[str, tuple[int, int]] | None = None,
    checkpoint_callback: "Callable[[dict], None] | None" = None,
    checkpoint_interval_secs: float = 30.0,
    codebase_id: str | None = None,
) -> dict[str, tuple[int, int]]:
    """
    Calculate tree sizes for all commits using incremental delta approach.

    Uses native Rust extension when available (much faster for large repos),
    falls back to Python incremental approach if not installed.

    The incremental approach calculates:
        tree_size(commit) = tree_size(parent) + delta(parent, commit)

    This is O(commits x avg_files_changed) instead of O(commits x tree_size),
    which provides massive speedups for large repositories.

    Args:
        repo: pygit2.Repository instance
        commit_shas: Set of commit SHAs to process (legacy, will sort internally)
        commits_topo: Pre-sorted commits in topological order (optimized path)
        initial_results: Pre-computed tree sizes to resume from (checkpoint)
        checkpoint_callback: Called at intervals with checkpoint data
        checkpoint_interval_secs: How often to call checkpoint callback (default 30s)
        codebase_id: Codebase ID for checkpoint metadata

    Note: Either commit_shas OR commits_topo should be provided, not both.
    If commits_topo is provided, it takes precedence and avoids the graph walk.

    Returns:
        Dict mapping commit_sha to (tree_bytes, tree_lines)
    """
    # Use pre-sorted commits if provided, otherwise sort internally
    if commits_topo is not None:
        commits = commits_topo
    elif commit_shas is not None:
        commits = _get_commits_topological(repo, commit_shas)
    else:
        # Neither provided - nothing to process
        return {}

    total = len(commits)

    # Try native Rust incremental extension (much faster for large repos)
    try:
        from analytics_native import (
            CommitWithParent,
        )
        from analytics_native import (
            calculate_tree_sizes_incremental as native_incremental,
        )

        workers = get_parallel_workers()
        logger.info(f"Using native Rust incremental extension for {total} commits")

        # Build commit list with parent info for incremental calculation
        commit_list = []
        for commit in commits:
            sha = str(commit.id)
            parent_sha = str(commit.parents[0].id) if commit.parents else None
            commit_list.append(CommitWithParent(sha, parent_sha))

        # Create wrapper callback to transform Rust callback data to Python format
        rust_callback = None
        if checkpoint_callback:
            import time

            callback_start_time = time.time()

            def rust_callback(index: int, results: dict[str, tuple[int, int]]) -> None:
                """Transform Rust callback data to checkpoint format."""
                nonlocal callback_start_time
                # Log progress
                processed = index + 1
                pct = (processed / total) * 100
                elapsed = time.time() - callback_start_time
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = total - processed
                eta = remaining / rate if rate > 0 else 0
                logger.info(
                    f"Tree size progress: {processed}/{total} ({pct:.1f}%) | "
                    f"{rate:.0f} commits/sec | ETA: {eta:.0f}s"
                )

                # Get the SHA of the commit at this index
                last_sha = commit_list[index].sha if index < len(commit_list) else ""
                checkpoint_data = {
                    "codebase_id": codebase_id,
                    "commits_total": total,
                    "commits_processed": index + 1,
                    "last_processed_index": index,
                    "last_processed_sha": last_sha,
                    "tree_size_cache": results,
                }
                checkpoint_callback(checkpoint_data)

        result = native_incremental(
            repo.path,
            commit_list,
            initial_results=initial_results,
            checkpoint_callback=rust_callback,
            checkpoint_interval_secs=checkpoint_interval_secs
            if checkpoint_callback
            else None,
            num_workers=workers,
        )
        logger.info(f"Native incremental calculation complete: {len(result)} commits")
        return result

    except ImportError:
        logger.info("Native extension not available, using Python implementation")
        return _calculate_tree_sizes_python(
            repo, commits, initial_results=initial_results
        )


def _calculate_tree_sizes_python(
    repo: pygit2.Repository,
    commits: list[pygit2.Commit],
    initial_results: dict[str, tuple[int, int]] | None = None,
) -> dict[str, tuple[int, int]]:
    """
    Python fallback for tree size calculation using incremental delta approach.

    Instead of walking the entire tree for each commit (~1000 files), we
    calculate the delta from the parent commit (~2-5 files changed).

    Args:
        initial_results: Pre-computed tree sizes from checkpoint to resume from
    """
    # Initialize with checkpoint data if available
    tree_sizes: dict[str, tuple[int, int]] = (
        dict(initial_results) if initial_results else {}
    )
    if initial_results:
        logger.info(
            f"Resuming tree size calculation with {len(initial_results)} cached entries"
        )
    blob_lines_cache: dict[str, int] = {}
    blob_bytes_cache: dict[str, int] = {}  # Cache blob sizes to avoid redundant lookups
    hex_cache: dict[str, bool] = {}  # Cache hex detection results by blob OID

    total = len(commits)

    for i, commit in enumerate(commits):
        sha = str(commit.id)

        if sha in tree_sizes:
            continue

        # Root commit or parent not in cache: full tree walk
        if len(commit.parents) == 0:
            tree_bytes, tree_lines = _walk_tree_recursive(repo, commit.tree, ())
            tree_sizes[sha] = (tree_bytes, tree_lines)
            continue

        parent = commit.parents[0]
        parent_sha = str(parent.id)

        if parent_sha not in tree_sizes:
            # Parent not processed yet - do full tree walk
            tree_bytes, tree_lines = _walk_tree_recursive(repo, commit.tree, ())
            tree_sizes[sha] = (tree_bytes, tree_lines)
            continue

        # Incremental: calculate delta from parent
        # Use max(0) to guard against negative values from delta calculation errors
        parent_bytes, parent_lines = tree_sizes[parent_sha]
        delta_bytes, delta_lines = _calculate_delta(
            repo, parent, commit, blob_lines_cache, blob_bytes_cache, hex_cache
        )
        tree_sizes[sha] = (
            max(0, parent_bytes + delta_bytes),
            max(0, parent_lines + delta_lines),
        )

        if (i + 1) % 5000 == 0 or (i + 1) == total:
            logger.info(f"Tree sizes: {i + 1}/{total} commits processed")

    logger.info(
        f"Incremental caches: {len(blob_lines_cache)} blob lines, "
        f"{len(blob_bytes_cache)} blob bytes, {len(hex_cache)} hex results"
    )
    return tree_sizes


def _calculate_delta(
    repo: pygit2.Repository,
    parent: pygit2.Commit,
    commit: pygit2.Commit,
    blob_lines_cache: dict[str, int],
    blob_bytes_cache: dict[str, int],
    hex_cache: dict[str, bool],
) -> tuple[int, int]:
    """
    Calculate the change in (bytes, lines) between parent and commit.

    Examines only the files that changed in the diff, not the entire tree.
    """

    delta_bytes = 0
    delta_lines = 0

    try:
        diff = repo.diff(parent.tree, commit.tree)
    except Exception:
        return (0, 0)

    for patch in diff:
        delta = patch.delta
        old_analyzable = _is_diff_file_analyzable(
            repo, delta.old_file, blob_lines_cache, hex_cache
        )
        new_analyzable = _is_diff_file_analyzable(
            repo, delta.new_file, blob_lines_cache, hex_cache
        )

        if old_analyzable and not new_analyzable:
            # File became non-analyzable (deleted or moved to blacklisted dir)
            old_bytes, old_lines = _get_blob_size(
                repo, delta.old_file, blob_lines_cache, blob_bytes_cache
            )
            delta_bytes -= old_bytes
            delta_lines -= old_lines

        elif not old_analyzable and new_analyzable:
            # File became analyzable (added or moved from blacklisted dir)
            new_bytes, new_lines = _get_blob_size(
                repo, delta.new_file, blob_lines_cache, blob_bytes_cache
            )
            delta_bytes += new_bytes
            delta_lines += new_lines

        elif old_analyzable and new_analyzable:
            # Both analyzable - calculate difference
            old_bytes, old_lines = _get_blob_size(
                repo, delta.old_file, blob_lines_cache, blob_bytes_cache
            )
            new_bytes, new_lines = _get_blob_size(
                repo, delta.new_file, blob_lines_cache, blob_bytes_cache
            )
            delta_bytes += new_bytes - old_bytes
            delta_lines += new_lines - old_lines

    return (delta_bytes, delta_lines)


def _is_diff_file_analyzable(
    repo: pygit2.Repository,
    diff_file: pygit2.DiffFile,
    blob_lines_cache: dict[str, int],
    hex_cache: dict[str, bool],
) -> bool:
    """Check if a diff file is analyzable, using hex_cache to avoid redundant checks."""
    from pathlib import Path

    from analytics.utils.file_filter import (
        can_skip_hex_check,
        is_blacklisted_extension,
        is_blacklisted_filename,
        is_blacklisted_path,
        is_hex_content,
    )

    if not diff_file.path:
        return False

    path = Path(diff_file.path)
    blob_oid = str(diff_file.id)

    # Zero OID means file doesn't exist in this version
    if blob_oid == "0" * 40:
        return False

    # Quick path-based checks first (no blob read needed)
    if is_blacklisted_path(path.parts):
        return False
    if is_blacklisted_extension(path.suffix):
        return False
    if is_blacklisted_filename(path.name):
        return False

    # Fast path: skip hex check for known-safe extensions
    skip_hex = can_skip_hex_check(path.suffix)

    try:
        blob = repo.get(diff_file.id)
        if blob is None:
            return False

        # Binary check
        if blob.is_binary:
            return False

        # Hex content check (skip for safe extensions, use cache for others)
        if not skip_hex:
            if blob_oid in hex_cache:
                if hex_cache[blob_oid]:
                    return False  # Cached as hex content
            else:
                # Check and cache the result
                is_hex = is_hex_content(blob.data)
                hex_cache[blob_oid] = is_hex
                if is_hex:
                    return False

        return True
    except Exception:
        return False


def _get_blob_size(
    repo: pygit2.Repository,
    diff_file: pygit2.DiffFile,
    blob_lines_cache: dict[str, int],
    blob_bytes_cache: dict[str, int],
) -> tuple[int, int]:
    """Get (bytes, lines) for a blob, using caches to avoid redundant git lookups."""
    blob_oid = str(diff_file.id)

    # Zero OID means file doesn't exist
    if blob_oid == "0" * 40:
        return (0, 0)

    # OPTIMIZATION: Check both caches BEFORE doing expensive git lookup
    if blob_oid in blob_bytes_cache and blob_oid in blob_lines_cache:
        return (blob_bytes_cache[blob_oid], blob_lines_cache[blob_oid])

    try:
        blob = repo.get(diff_file.id)
        if blob is None:
            return (0, 0)

        # Use blob.size property (reads from git object header, may be faster)
        blob_bytes = blob.size

        # Check if we only need to calculate lines (bytes might be cached)
        if blob_oid in blob_lines_cache:
            blob_bytes_cache[blob_oid] = blob_bytes
            return (blob_bytes, blob_lines_cache[blob_oid])

        # Calculate line count
        if blob.is_binary:
            blob_lines = 0
        else:
            data = blob.data
            if not data:
                blob_lines = 0
            else:
                blob_lines = data.count(b"\n")
                if not data.endswith(b"\n"):
                    blob_lines += 1

        # Cache both values
        blob_bytes_cache[blob_oid] = blob_bytes
        blob_lines_cache[blob_oid] = blob_lines
        return (blob_bytes, blob_lines)

    except Exception:
        return (0, 0)
