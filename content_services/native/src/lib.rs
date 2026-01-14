//! Native Rust extension for analytics tree size calculation and commit processing.
//!
//! This module provides PyO3 bindings to expose Rust implementations
//! of tree size calculation and commit processing to Python.
//!
//! ## Usage from Python
//!
//! ```python
//! from analytics_native import (
//!     calculate_tree_sizes,
//!     calculate_tree_sizes_incremental,
//!     is_path_analyzable,
//!     CommitWithParent,
//!     CommitInput,
//!     process_commits_parallel,
//!     get_language_from_path,
//! )
//!
//! # Calculate tree sizes for multiple commits in parallel (full tree walks)
//! results = calculate_tree_sizes("/path/to/repo", ["sha1", "sha2", ...], num_workers=4)
//! # Returns: {"sha1": (bytes, lines), "sha2": (bytes, lines), ...}
//!
//! # Calculate tree sizes incrementally using parent deltas (much faster)
//! commits = [
//!     CommitWithParent("sha1", None),  # Root commit
//!     CommitWithParent("sha2", "sha1"),  # Child of sha1
//!     CommitWithParent("sha3", "sha2"),  # Child of sha2
//! ]
//! results = calculate_tree_sizes_incremental("/path/to/repo", commits, num_workers=4)
//!
//! # Process commits in parallel (Phase 3)
//! commit_inputs = [
//!     CommitInput("sha1", ["main"], tree_bytes, tree_lines),
//!     CommitInput("sha2", ["main", "develop"], tree_bytes, tree_lines),
//! ]
//! commit_records, file_changes = process_commits_parallel(
//!     "/path/to/repo", commit_inputs, "codebase-id",
//!     collected_at_ts, include_file_changes=False, num_workers=4
//! )
//!
//! # Check if a path is analyzable
//! is_analyzable = is_path_analyzable("src/main.py")  # True
//! is_analyzable = is_path_analyzable(".git/config")  # False
//!
//! # Get language from file path
//! lang = get_language_from_path("main.py")  # "Python"
//! ```

use pyo3::prelude::*;
use rayon::prelude::*;
use std::collections::HashMap;
use std::io::Write;
use std::sync::OnceLock;

/// Macro to log to stderr with immediate flush for containerized environments.
macro_rules! log_flush {
    ($($arg:tt)*) => {{
        eprintln!($($arg)*);
        let _ = std::io::stderr().flush();
    }};
}

mod cache;
mod commit;
mod delta;
mod error;
mod file_change;
mod filters;
mod language;
mod tree_size;

use cache::new_shared_cache_with_capacity;
use commit::CommitInput;
use delta::calculate_delta;
use error::AnalyticsError;
use filters::FileFilter;

/// Global rayon thread pool configuration.
/// We only set this once per process.
static THREAD_POOL_CONFIGURED: OnceLock<()> = OnceLock::new();

/// Configure the rayon thread pool with the given number of workers.
fn configure_thread_pool(num_workers: usize) {
    THREAD_POOL_CONFIGURED.get_or_init(|| {
        let _ = rayon::ThreadPoolBuilder::new()
            .num_threads(num_workers)
            .build_global();
    });
}

/// Calculate tree sizes for multiple commits in parallel.
///
/// This function opens the repository once per thread and processes
/// commits in parallel using rayon.
///
/// # Arguments
///
/// * `repo_path` - Path to the git repository
/// * `commit_shas` - List of commit SHAs to process
/// * `num_workers` - Optional number of parallel workers (default: 4)
///
/// # Returns
///
/// A dictionary mapping each SHA to a tuple of (bytes, lines).
///
/// # Errors
///
/// Returns a Python RuntimeError if:
/// - The repository cannot be opened
/// - A commit SHA is invalid
/// - A commit cannot be found
#[pyfunction]
#[pyo3(signature = (repo_path, commit_shas, num_workers=None))]
fn calculate_tree_sizes(
    repo_path: &str,
    commit_shas: Vec<String>,
    num_workers: Option<usize>,
) -> PyResult<HashMap<String, (u64, u64)>> {
    // Configure thread pool
    let workers = num_workers.unwrap_or(4);
    configure_thread_pool(workers);

    let filter = FileFilter::new();

    // Process commits in parallel
    let results: Result<HashMap<String, (u64, u64)>, AnalyticsError> = commit_shas
        .par_iter()
        .map(|sha| {
            // Each thread opens its own repo handle (thread-safe)
            let repo = git2::Repository::open(repo_path)?;

            let oid = git2::Oid::from_str(sha).map_err(|e| AnalyticsError::InvalidSha {
                sha: sha.clone(),
                message: e.to_string(),
            })?;

            let commit = repo
                .find_commit(oid)
                .map_err(|_| AnalyticsError::CommitNotFound(sha.clone()))?;

            let tree = commit
                .tree()
                .map_err(|_| AnalyticsError::TreeNotFound(sha.clone()))?;

            let result = tree_size::walk_tree(&repo, &tree, &filter);
            Ok((sha.clone(), (result.bytes, result.lines)))
        })
        .collect();

    results.map_err(|e| e.into())
}

/// Check if a path is analyzable (would be counted for SLOC).
///
/// This matches the Python `is_analyzable_path()` function.
/// It checks:
/// - Not in a blacklisted directory (.git, driver_docs)
/// - Not a blacklisted extension (.svg, .exe, .dll, etc.)
/// - Not a blacklisted filename (.DS_Store, .driverignore)
///
/// Note: This does not check for binary content or hex content,
/// which requires access to the file contents.
///
/// # Arguments
///
/// * `path` - File path to check (e.g., "src/main.py")
///
/// # Returns
///
/// True if the path appears to be analyzable.
#[pyfunction]
fn is_path_analyzable(path: &str) -> bool {
    let filter = FileFilter::new();
    filter.is_analyzable(path)
}

/// Represents a commit with its parent for incremental processing.
///
/// Used by `calculate_tree_sizes_incremental` to track parent-child
/// relationships for delta calculation.
#[pyclass]
#[derive(Clone)]
pub struct CommitWithParent {
    /// The commit SHA.
    #[pyo3(get)]
    pub sha: String,
    /// The parent commit SHA (None for root commits).
    #[pyo3(get)]
    pub parent_sha: Option<String>,
}

#[pymethods]
impl CommitWithParent {
    /// Create a new CommitWithParent.
    ///
    /// # Arguments
    ///
    /// * `sha` - The commit SHA
    /// * `parent_sha` - The parent commit SHA (None for root commits)
    #[new]
    fn new(sha: String, parent_sha: Option<String>) -> Self {
        CommitWithParent { sha, parent_sha }
    }

    fn __repr__(&self) -> String {
        match &self.parent_sha {
            Some(parent) => format!("CommitWithParent({}, {})", self.sha, parent),
            None => format!("CommitWithParent({}, None)", self.sha),
        }
    }
}

/// Calculate tree sizes incrementally using parent deltas.
///
/// This is much faster than full tree walks because it only examines
/// the files that changed between commits, not the entire tree.
///
/// # Arguments
///
/// * `repo_path` - Path to the git repository
/// * `commits` - List of CommitWithParent in topological order (oldest first)
/// * `initial_results` - Optional: pre-populated results for resume from checkpoint
/// * `checkpoint_callback` - Optional: Python callable for periodic checkpoints.
///                           Called with (index, results_dict) at intervals.
/// * `checkpoint_interval_secs` - Checkpoint frequency in seconds (default: 30.0)
/// * `num_workers` - Optional number of parallel workers (currently unused,
///                   incremental processing is sequential due to parent dependency)
///
/// # Returns
///
/// A dictionary mapping each SHA to a tuple of (bytes, lines).
///
/// # Errors
///
/// Returns a Python RuntimeError if:
/// - The repository cannot be opened
/// - A commit SHA is invalid
/// - A commit cannot be found
/// - A parent commit is not yet processed (wrong topological order)
#[pyfunction]
#[pyo3(signature = (repo_path, commits, initial_results=None, checkpoint_callback=None, checkpoint_interval_secs=None, num_workers=None))]
fn calculate_tree_sizes_incremental(
    py: Python<'_>,
    repo_path: &str,
    commits: Vec<CommitWithParent>,
    initial_results: Option<HashMap<String, (u64, u64)>>,
    checkpoint_callback: Option<PyObject>,
    checkpoint_interval_secs: Option<f64>,
    num_workers: Option<usize>,
) -> PyResult<HashMap<String, (u64, u64)>> {
    // Configure thread pool (for any parallel operations within delta calc)
    let workers = num_workers.unwrap_or(4);
    configure_thread_pool(workers);

    let filter = FileFilter::new();
    // Estimate ~10 unique blobs per commit on average
    let cache = new_shared_cache_with_capacity(commits.len() * 10);

    // Initialize from checkpoint if resuming
    let mut results: HashMap<String, (u64, u64)> = initial_results
        .unwrap_or_else(|| HashMap::with_capacity(commits.len()));
    let start_index = results.len();

    let total = commits.len();
    let start_time = std::time::Instant::now();

    // Checkpoint timing
    let checkpoint_interval = checkpoint_interval_secs.unwrap_or(30.0);
    let mut last_checkpoint = std::time::Instant::now();

    if start_index > 0 {
        log_flush!(
            "[rust-incremental] Resuming from checkpoint: {}/{} commits already processed",
            start_index, total
        );
    } else {
        log_flush!(
            "[rust-incremental] Starting incremental tree size calculation for {} commits",
            total
        );
    }

    // Open repo once (we'll reuse it for all commits)
    let repo = git2::Repository::open(repo_path).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to open repo: {}", e))
    })?;

    let mut root_commits = 0;
    let mut delta_commits = 0;

    // Process commits in topological order (sequential due to parent dependency)
    for (idx, commit_info) in commits.iter().enumerate() {
        // Skip if already in results (from checkpoint or duplicate)
        if results.contains_key(&commit_info.sha) {
            continue;
        }

        let oid = git2::Oid::from_str(&commit_info.sha).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Invalid SHA {}: {}",
                commit_info.sha, e
            ))
        })?;

        let commit = repo.find_commit(oid).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Commit not found {}: {}",
                commit_info.sha, e
            ))
        })?;

        let size = match &commit_info.parent_sha {
            Some(parent_sha) => {
                // Try incremental: parent_size + delta
                // If parent not in results (e.g., incremental extraction with since_sha),
                // fall back to full tree walk
                if let Some(parent_size) = results.get(parent_sha) {
                    delta_commits += 1;

                    let parent_oid = git2::Oid::from_str(parent_sha).map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!(
                            "Invalid parent SHA {}: {}",
                            parent_sha, e
                        ))
                    })?;

                    let parent_commit = repo.find_commit(parent_oid).map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!(
                            "Parent commit not found {}: {}",
                            parent_sha, e
                        ))
                    })?;

                    let delta_result =
                        calculate_delta(&repo, &parent_commit, &commit, &filter, &cache).map_err(
                            |e| {
                                pyo3::exceptions::PyRuntimeError::new_err(format!(
                                    "Delta calculation failed for {}: {}",
                                    commit_info.sha, e
                                ))
                            },
                        )?;

                    // Apply delta to parent size
                    let new_bytes = (parent_size.0 as i64 + delta_result.added_bytes).max(0) as u64;
                    let new_lines = (parent_size.1 as i64 + delta_result.added_lines).max(0) as u64;
                    (new_bytes, new_lines)
                } else {
                    // Parent not in results (incremental extraction) - fall back to full tree walk
                    root_commits += 1;
                    let tree = commit.tree().map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!(
                            "Tree not found for {}: {}",
                            commit_info.sha, e
                        ))
                    })?;

                    let result = tree_size::walk_tree(&repo, &tree, &filter);
                    (result.bytes, result.lines)
                }
            }
            None => {
                // Root commit: full tree walk
                root_commits += 1;
                let tree = commit.tree().map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Tree not found for {}: {}",
                        commit_info.sha, e
                    ))
                })?;

                let result = tree_size::walk_tree(&repo, &tree, &filter);
                (result.bytes, result.lines)
            }
        };

        results.insert(commit_info.sha.clone(), size);

        // Checkpoint callback - call if enough time has elapsed
        if let Some(ref callback) = checkpoint_callback {
            if last_checkpoint.elapsed().as_secs_f64() >= checkpoint_interval {
                let results_clone = results.clone();
                callback.call1(py, (idx, results_clone))?;
                last_checkpoint = std::time::Instant::now();
            }
        }

        // Progress logging every 1000 commits or at key milestones
        let processed = idx + 1;
        if processed % 1000 == 0 || processed == total || processed == start_index + 1 || processed == 10 || processed == 100 {
            let elapsed = start_time.elapsed().as_secs_f64();
            let commits_this_run = processed.saturating_sub(start_index);
            let rate = if elapsed > 0.0 { commits_this_run as f64 / elapsed } else { 0.0 };
            let remaining = total - processed;
            let eta = if rate > 0.0 { remaining as f64 / rate } else { 0.0 };
            log_flush!(
                "[rust-incremental] Progress: {}/{} ({:.1}%) | {:.0} commits/sec | ETA: {:.0}s | tree_size: ({}, {})",
                processed, total,
                (processed as f64 / total as f64) * 100.0,
                rate,
                eta,
                size.0, size.1
            );
        }
    }

    let elapsed = start_time.elapsed().as_secs_f64();
    let (cache_bytes, cache_lines) = cache.stats();
    let commits_processed = total.saturating_sub(start_index);
    let rate = if elapsed > 0.0 { commits_processed as f64 / elapsed } else { 0.0 };
    log_flush!(
        "[rust-incremental] Complete: {} commits in {:.2}s ({:.0} commits/sec)",
        commits_processed, elapsed, rate
    );
    log_flush!(
        "[rust-incremental] Stats: {} root commits (full walk), {} delta commits | Cache: {} bytes, {} lines",
        root_commits, delta_commits, cache_bytes, cache_lines
    );

    Ok(results)
}

/// Get language for a file path.
///
/// Tries extension first, then falls back to filename detection.
///
/// # Arguments
///
/// * `file_path` - Path to file (e.g., "src/main.py")
///
/// # Returns
///
/// Language name or None if not recognized.
#[pyfunction]
fn get_language_from_path(file_path: &str) -> Option<String> {
    language::get_language_from_path(file_path).map(|s| s.to_string())
}

/// Process commits in parallel and return (commit_records, file_changes).
///
/// This function processes commits using thread-local repository handles
/// to avoid the overhead of opening a new repo per commit.
///
/// Supports checkpointing for fault tolerance on large repositories.
/// Processes commits in batches of 10,000 and calls checkpoint_callback
/// after each batch completes.
///
/// # Arguments
///
/// * `repo_path` - Path to git repository
/// * `commits` - List of CommitInput with SHA, branches, and pre-computed tree sizes
/// * `codebase_id` - UUID of the codebase
/// * `collected_at_ts` - Collection timestamp as Unix float
/// * `include_file_changes` - Whether to extract file-level changes
/// * `skip_shas` - Optional set of SHAs to skip (already processed from checkpoint)
/// * `checkpoint_callback` - Optional Python callable for periodic checkpoints.
///                           Called with (batch_shas, commit_records, file_changes) after each batch.
/// * `num_workers` - Optional number of parallel workers (default: 4)
///
/// # Returns
///
/// Tuple of (commit_records, file_changes) where each is a list of dicts.
#[pyfunction]
#[pyo3(signature = (repo_path, commits, codebase_id, collected_at_ts, include_file_changes=false, skip_shas=None, checkpoint_callback=None, num_workers=None))]
fn process_commits_parallel(
    py: Python<'_>,
    repo_path: &str,
    commits: Vec<CommitInput>,
    codebase_id: &str,
    collected_at_ts: f64,
    include_file_changes: bool,
    skip_shas: Option<&pyo3::types::PySet>,
    checkpoint_callback: Option<PyObject>,
    num_workers: Option<usize>,
) -> PyResult<(Vec<PyObject>, Vec<PyObject>)> {
    commit::process_commits_parallel(
        py,
        repo_path,
        commits,
        codebase_id,
        collected_at_ts,
        include_file_changes,
        skip_shas,
        checkpoint_callback,
        num_workers,
    )
}

/// Python module definition.
#[pymodule]
fn analytics_native(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(calculate_tree_sizes, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_tree_sizes_incremental, m)?)?;
    m.add_function(wrap_pyfunction!(is_path_analyzable, m)?)?;
    m.add_function(wrap_pyfunction!(process_commits_parallel, m)?)?;
    m.add_function(wrap_pyfunction!(get_language_from_path, m)?)?;
    m.add_class::<CommitWithParent>()?;
    m.add_class::<CommitInput>()?;
    Ok(())
}
