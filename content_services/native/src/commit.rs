//! Commit processing logic.
//!
//! Provides parallel commit processing with thread-local repository handles.
//! Ports Python's `_process_commits_parallel` and `_extract_commit_data_with_diff`.

use crate::error::AnalyticsError;
use crate::filters::FileFilter;
use chrono::{DateTime, TimeZone, Utc};
use git2::{DiffOptions, Oid, Repository};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet};
use rayon::prelude::*;
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

/// Macro to log to stderr with immediate flush for containerized environments.
macro_rules! log_flush {
    ($($arg:tt)*) => {{
        eprintln!($($arg)*);
        let _ = std::io::stderr().flush();
    }};
}

/// Input for commit processing, passed from Python.
#[pyclass]
#[derive(Clone, Debug)]
pub struct CommitInput {
    #[pyo3(get)]
    pub sha: String,
    #[pyo3(get)]
    pub branches: Vec<String>,
    #[pyo3(get)]
    pub tree_bytes: u64,
    #[pyo3(get)]
    pub tree_lines: u64,
}

#[pymethods]
impl CommitInput {
    #[new]
    pub fn new(sha: String, branches: Vec<String>, tree_bytes: u64, tree_lines: u64) -> Self {
        Self {
            sha,
            branches,
            tree_bytes,
            tree_lines,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "CommitInput(sha='{}', branches={:?}, tree_bytes={}, tree_lines={})",
            &self.sha[..8.min(self.sha.len())],
            self.branches,
            self.tree_bytes,
            self.tree_lines
        )
    }
}

/// Diff-based metrics for a commit, filtered to analyzable files only.
#[derive(Debug, Default)]
pub struct DiffMetrics {
    pub files_changed: i32,
    pub additions_lines: i32,
    pub deletions_lines: i32,
    pub addition_bytes: i64,
    pub deletion_bytes: i64,
}

impl DiffMetrics {
    /// Calculate derived metrics
    pub fn net_lines(&self) -> i32 {
        self.additions_lines - self.deletions_lines
    }

    pub fn churn_lines(&self) -> i32 {
        self.additions_lines + self.deletions_lines
    }

    pub fn patch_bytes(&self) -> i64 {
        self.addition_bytes + self.deletion_bytes
    }

    pub fn net_bytes(&self) -> i64 {
        self.addition_bytes - self.deletion_bytes
    }

    pub fn sloc(&self) -> i64 {
        self.patch_bytes() / 50
    }

    pub fn bytes_per_line(&self) -> f64 {
        let churn = self.churn_lines();
        if churn > 0 {
            self.patch_bytes() as f64 / churn as f64
        } else {
            0.0
        }
    }

    /// Categorize commit size based on churn lines
    pub fn size_category(&self) -> &'static str {
        let churn = self.churn_lines();
        if churn < 10 {
            "tiny"
        } else if churn < 50 {
            "small"
        } else if churn < 200 {
            "medium"
        } else {
            "large"
        }
    }

    /// Detect if commit is a refactor (roughly equal adds/deletes)
    pub fn is_refactor(&self) -> bool {
        self.additions_lines > 0
            && self.deletions_lines > 0
            && (self.net_lines().abs() as f64) < (self.churn_lines() as f64 * 0.1)
    }
}

/// Process a single commit and return commit records (one per branch).
fn process_single_commit(
    repo: &Repository,
    commit_input: &CommitInput,
    codebase_id: &str,
    collected_at_ts: f64,
    include_file_changes: bool,
    filter: &FileFilter,
) -> Result<(Vec<HashMap<String, PyValue>>, Vec<HashMap<String, PyValue>>), AnalyticsError> {
    let oid = Oid::from_str(&commit_input.sha)?;
    let commit = repo.find_commit(oid)?;

    // Get commit metadata
    let author = commit.author();
    let committer = commit.committer();
    let message = commit.message().unwrap_or("");
    let message_truncated: String = message.chars().take(1000).collect();
    let message_length = message.len() as i32;

    // Get timestamp - use Unix epoch (1970-01-01) as fallback for invalid timestamps
    // to avoid empty strings that fail Parquet schema validation
    let commit_time = commit.time();
    let commit_ts = commit_time.seconds();
    let epoch = Utc.timestamp_opt(0, 0).unwrap();
    let commit_dt = Utc.timestamp_opt(commit_ts, 0).single().unwrap_or(epoch);

    // Format timestamps
    let committed_at = commit_dt.to_rfc3339();
    let commit_date = commit_dt.format("%Y-%m-%d").to_string();
    let commit_year: i64 = commit_dt.format("%Y").to_string().parse().unwrap_or(1970);
    let commit_month: i64 = commit_dt.format("%m").to_string().parse().unwrap_or(1);
    let commit_day: i64 = commit_dt.format("%d").to_string().parse().unwrap_or(1);

    let collected_at = DateTime::from_timestamp(collected_at_ts as i64, 0)
        .unwrap_or(epoch)
        .to_rfc3339();

    // Parent info
    let parent_count = commit.parent_count() as i32;
    let is_merge_commit = parent_count > 1;

    // Calculate diff metrics and optionally extract file changes (filtered to analyzable files)
    let (diff_metrics, file_changes) = calculate_diff_metrics(
        repo,
        &commit,
        filter,
        include_file_changes,
        &commit_input.sha,
        codebase_id,
    )?;

    // Tree metrics from cache
    let tree_bytes = commit_input.tree_bytes as i64;
    let tree_lines = commit_input.tree_lines as i64;
    let tree_sloc = tree_bytes / 50;

    // Build base record
    let mut base_record: HashMap<String, PyValue> = HashMap::new();
    base_record.insert("commit_sha".to_string(), PyValue::Str(commit_input.sha.clone()));
    base_record.insert("codebase_id".to_string(), PyValue::Str(codebase_id.to_string()));
    base_record.insert("committed_at".to_string(), PyValue::Str(committed_at));
    base_record.insert("collected_at".to_string(), PyValue::Str(collected_at));
    base_record.insert("commit_date".to_string(), PyValue::Str(commit_date.clone()));
    base_record.insert("commit_year".to_string(), PyValue::Int(commit_year));
    base_record.insert("commit_month".to_string(), PyValue::Int(commit_month));
    base_record.insert("commit_day".to_string(), PyValue::Int(commit_day));
    base_record.insert("author_email".to_string(), PyValue::Str(author.email().unwrap_or("").to_string()));
    base_record.insert("author_name".to_string(), PyValue::Str(author.name().unwrap_or("").to_string()));
    base_record.insert("committer_email".to_string(), PyValue::Str(committer.email().unwrap_or("").to_string()));
    base_record.insert("committer_name".to_string(), PyValue::Str(committer.name().unwrap_or("").to_string()));
    base_record.insert("message".to_string(), PyValue::Str(message_truncated));
    base_record.insert("message_length".to_string(), PyValue::Int(message_length as i64));
    base_record.insert("parent_count".to_string(), PyValue::Int(parent_count as i64));
    base_record.insert("is_merge_commit".to_string(), PyValue::Bool(is_merge_commit));
    base_record.insert("files_changed".to_string(), PyValue::Int(diff_metrics.files_changed as i64));
    base_record.insert("additions_lines".to_string(), PyValue::Int(diff_metrics.additions_lines as i64));
    base_record.insert("deletions_lines".to_string(), PyValue::Int(diff_metrics.deletions_lines as i64));
    base_record.insert("net_lines".to_string(), PyValue::Int(diff_metrics.net_lines() as i64));
    base_record.insert("churn_lines".to_string(), PyValue::Int(diff_metrics.churn_lines() as i64));
    base_record.insert("addition_bytes".to_string(), PyValue::Int(diff_metrics.addition_bytes));
    base_record.insert("deletion_bytes".to_string(), PyValue::Int(diff_metrics.deletion_bytes));
    base_record.insert("patch_bytes".to_string(), PyValue::Int(diff_metrics.patch_bytes()));
    base_record.insert("net_bytes".to_string(), PyValue::Int(diff_metrics.net_bytes()));
    base_record.insert("sloc".to_string(), PyValue::Int(diff_metrics.sloc()));
    base_record.insert("bytes_per_line".to_string(), PyValue::Float(diff_metrics.bytes_per_line()));
    base_record.insert("commit_size_category".to_string(), PyValue::Str(diff_metrics.size_category().to_string()));
    base_record.insert("is_refactor".to_string(), PyValue::Bool(diff_metrics.is_refactor()));
    base_record.insert("tree_bytes".to_string(), PyValue::Int(tree_bytes));
    base_record.insert("tree_lines".to_string(), PyValue::Int(tree_lines));
    base_record.insert("tree_sloc".to_string(), PyValue::Int(tree_sloc));
    base_record.insert("collection_version".to_string(), PyValue::Str("2.0".to_string()));

    // Create one record per branch
    let commit_records: Vec<HashMap<String, PyValue>> = commit_input
        .branches
        .iter()
        .map(|branch| {
            let mut record = base_record.clone();
            record.insert("branch_name".to_string(), PyValue::Str(branch.clone()));
            record
        })
        .collect();

    Ok((commit_records, file_changes))
}

/// Calculate diff metrics for a commit, filtering to analyzable files.
/// Also extracts file changes if requested (to avoid lifetime issues with returning the diff).
fn calculate_diff_metrics(
    repo: &Repository,
    commit: &git2::Commit,
    filter: &FileFilter,
    include_file_changes: bool,
    commit_sha: &str,
    codebase_id: &str,
) -> Result<(DiffMetrics, Vec<HashMap<String, PyValue>>), AnalyticsError> {
    let mut metrics = DiffMetrics::default();

    // Get commit date for file changes
    let commit_time = commit.time();
    let commit_ts = commit_time.seconds();
    let commit_dt = Utc.timestamp_opt(commit_ts, 0).single();
    let commit_date = commit_dt
        .map(|dt| dt.format("%Y-%m-%d").to_string())
        .unwrap_or_default();
    let commit_year_month = commit_dt
        .map(|dt| dt.format("%Y-%m").to_string())
        .unwrap_or_else(|| "unknown".to_string());

    // Get diff from parent
    let diff = if commit.parent_count() > 0 {
        let parent = commit.parent(0)?;
        let parent_tree = parent.tree()?;
        let commit_tree = commit.tree()?;

        let mut opts = DiffOptions::new();
        opts.ignore_submodules(true);

        Some(repo.diff_tree_to_tree(Some(&parent_tree), Some(&commit_tree), Some(&mut opts))?)
    } else {
        // Root commit - diff against empty tree
        let commit_tree = commit.tree()?;
        let mut opts = DiffOptions::new();
        opts.ignore_submodules(true);

        Some(repo.diff_tree_to_tree(None, Some(&commit_tree), Some(&mut opts))?)
    };

    let mut file_changes: Vec<HashMap<String, PyValue>> = Vec::new();

    if let Some(ref diff) = diff {
        // Process each delta (file change)
        for delta_idx in 0..diff.deltas().len() {
            let delta = diff.get_delta(delta_idx).unwrap();

            // Get file path (prefer new file, fall back to old)
            let new_path = delta.new_file().path().and_then(|p| p.to_str());
            let old_path = delta.old_file().path().and_then(|p| p.to_str());
            let file_path = new_path.or(old_path);

            let Some(path) = file_path else { continue };

            // Filter to analyzable files only
            if !filter.is_analyzable(path) {
                continue;
            }

            metrics.files_changed += 1;

            // Get patch for this file to calculate line stats
            let mut additions_lines = 0i32;
            let mut deletions_lines = 0i32;
            let mut addition_bytes = 0i64;
            let mut deletion_bytes = 0i64;
            let mut has_patch_data = false;

            if let Ok(patch) = git2::Patch::from_diff(diff, delta_idx) {
                if let Some(patch) = patch {
                    has_patch_data = true;
                    let (_, additions, deletions) = patch.line_stats().unwrap_or((0, 0, 0));
                    additions_lines = additions as i32;
                    deletions_lines = deletions as i32;
                    metrics.additions_lines += additions_lines;
                    metrics.deletions_lines += deletions_lines;

                    // Calculate bytes from patch content
                    for hunk_idx in 0..patch.num_hunks() {
                        if let Ok((_hunk, num_lines)) = patch.hunk(hunk_idx) {
                            for line_idx in 0..num_lines {
                                if let Ok(line) = patch.line_in_hunk(hunk_idx, line_idx) {
                                    match line.origin() {
                                        '+' => {
                                            // Strip trailing newline to match Python behavior
                                            let content = line.content();
                                            let content = content.strip_suffix(b"\n").unwrap_or(content);
                                            let bytes = content.len() as i64;
                                            addition_bytes += bytes;
                                            metrics.addition_bytes += bytes;
                                        }
                                        '-' => {
                                            // Strip trailing newline to match Python behavior
                                            let content = line.content();
                                            let content = content.strip_suffix(b"\n").unwrap_or(content);
                                            let bytes = content.len() as i64;
                                            deletion_bytes += bytes;
                                            metrics.deletion_bytes += bytes;
                                        }
                                        _ => {}
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Extract file change record if requested
            if include_file_changes {
                let change_type = match delta.status() {
                    git2::Delta::Added => "added",
                    git2::Delta::Deleted => "deleted",
                    git2::Delta::Modified => "modified",
                    git2::Delta::Renamed => "renamed",
                    git2::Delta::Copied => "copied",
                    git2::Delta::Typechange => "typechange",
                    _ => "modified",
                };

                let previous_path = if change_type == "renamed" {
                    old_path.map(|s| s.to_string())
                } else {
                    None
                };

                let changes_lines = additions_lines + deletions_lines;
                let file_sloc = (addition_bytes + deletion_bytes) / 50;
                let file_extension = std::path::Path::new(path)
                    .extension()
                    .and_then(|e| e.to_str())
                    .map(|e| format!(".{}", e));
                let file_language = crate::language::get_language_from_path(path)
                    .map(|s| s.to_string());

                let mut record: HashMap<String, PyValue> = HashMap::new();
                record.insert("codebase_id".to_string(), PyValue::Str(codebase_id.to_string()));
                record.insert("commit_sha".to_string(), PyValue::Str(commit_sha.to_string()));
                record.insert("file_path".to_string(), PyValue::Str(path.to_string()));
                record.insert("commit_date".to_string(), PyValue::Str(commit_date.clone()));
                record.insert("commit_year_month".to_string(), PyValue::Str(commit_year_month.clone()));
                record.insert("change_type".to_string(), PyValue::Str(change_type.to_string()));
                record.insert(
                    "previous_path".to_string(),
                    previous_path.map(PyValue::Str).unwrap_or(PyValue::None),
                );
                record.insert("additions_lines".to_string(), PyValue::Int(additions_lines as i64));
                record.insert("deletions_lines".to_string(), PyValue::Int(deletions_lines as i64));
                record.insert("changes_lines".to_string(), PyValue::Int(changes_lines as i64));
                record.insert("addition_bytes".to_string(), PyValue::Int(addition_bytes));
                record.insert("deletion_bytes".to_string(), PyValue::Int(deletion_bytes));
                record.insert("file_sloc".to_string(), PyValue::Int(file_sloc));
                record.insert(
                    "file_extension".to_string(),
                    file_extension.map(PyValue::Str).unwrap_or(PyValue::None),
                );
                record.insert(
                    "file_language".to_string(),
                    file_language.map(PyValue::Str).unwrap_or(PyValue::None),
                );
                record.insert("has_patch_data".to_string(), PyValue::Bool(has_patch_data));
                record.insert("patch_blob_key".to_string(), PyValue::None);

                file_changes.push(record);
            }
        }
    }

    Ok((metrics, file_changes))
}

/// Enum to represent Python values for serialization
#[derive(Clone, Debug)]
pub enum PyValue {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
    None,
}

impl IntoPy<PyObject> for PyValue {
    fn into_py(self, py: Python<'_>) -> PyObject {
        match self {
            PyValue::Str(s) => s.into_py(py),
            PyValue::Int(i) => i.into_py(py),
            PyValue::Float(f) => f.into_py(py),
            PyValue::Bool(b) => b.into_py(py),
            PyValue::None => py.None(),
        }
    }
}

/// Convert HashMap<String, PyValue> to Python dict
fn hashmap_to_pydict(py: Python<'_>, map: HashMap<String, PyValue>) -> PyObject {
    let dict = PyDict::new(py);
    for (key, value) in map {
        dict.set_item(key, value.into_py(py)).unwrap();
    }
    dict.into()
}

/// Default batch size for commit processing with checkpointing
const BATCH_SIZE: usize = 10000;

/// Process commits in parallel and return (commit_records, file_changes).
///
/// Uses thread-local repository handles to avoid opening a new repo per commit.
/// Supports checkpointing for fault tolerance on large repositories.
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
pub fn process_commits_parallel(
    py: Python<'_>,
    repo_path: &str,
    commits: Vec<CommitInput>,
    codebase_id: &str,
    collected_at_ts: f64,
    include_file_changes: bool,
    skip_shas: Option<&PySet>,
    checkpoint_callback: Option<PyObject>,
    num_workers: Option<usize>,
) -> PyResult<(Vec<PyObject>, Vec<PyObject>)> {
    // Build skip set from Python set
    let skip_set: HashSet<String> = if let Some(py_set) = skip_shas {
        py_set
            .iter()
            .filter_map(|item| item.extract::<String>().ok())
            .collect()
    } else {
        HashSet::new()
    };

    // Filter out already-processed commits
    let commits_to_process: Vec<CommitInput> = if skip_set.is_empty() {
        commits
    } else {
        let before = commits.len();
        let filtered: Vec<CommitInput> = commits
            .into_iter()
            .filter(|c| !skip_set.contains(&c.sha))
            .collect();
        let skipped = before - filtered.len();
        if skipped > 0 {
            log_flush!(
                "[rust-commits] Resuming from checkpoint: skipping {} already-processed commits",
                skipped
            );
        }
        filtered
    };

    let total = commits_to_process.len();
    if total == 0 {
        log_flush!("[rust-commits] No commits to process (all already in checkpoint)");
        return Ok((Vec::new(), Vec::new()));
    }

    let workers = num_workers.unwrap_or(4);

    log_flush!(
        "[rust-commits] Starting commit processing for {} commits with {} workers",
        total, workers
    );

    // Configure rayon thread pool
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(workers)
        .build()
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;

    // Shared filter (read-only, safe to share)
    let filter = Arc::new(FileFilter::new());
    let repo_path_string = repo_path.to_string();
    let codebase_id_string = codebase_id.to_string();

    let start_time = std::time::Instant::now();

    // Accumulate all results
    let mut all_commits: Vec<PyObject> = Vec::new();
    let mut all_file_changes: Vec<PyObject> = Vec::new();
    let mut total_errors = 0;
    let mut total_processed = 0;

    // Process in batches for checkpointing
    let batches: Vec<Vec<CommitInput>> = commits_to_process
        .chunks(BATCH_SIZE)
        .map(|chunk| chunk.to_vec())
        .collect();
    let num_batches = batches.len();

    log_flush!(
        "[rust-commits] Processing {} commits in {} batches of up to {} commits each",
        total, num_batches, BATCH_SIZE
    );

    for (batch_idx, batch) in batches.into_iter().enumerate() {
        let batch_size = batch.len();
        let batch_shas: Vec<String> = batch.iter().map(|c| c.sha.clone()).collect();

        // Atomic counter for progress tracking within batch
        let processed_count = Arc::new(AtomicUsize::new(0));
        let log_interval = 5000;

        // Clone for closure
        let repo_path_clone = repo_path_string.clone();
        let codebase_id_clone = codebase_id_string.clone();
        let filter_clone = Arc::clone(&filter);

        // Process this batch in parallel
        let results: Vec<Result<(Vec<HashMap<String, PyValue>>, Vec<HashMap<String, PyValue>>), AnalyticsError>> = pool.install(|| {
            let processed_count = Arc::clone(&processed_count);
            batch
                .par_iter()
                .map(move |commit_input| {
                    let processed_count = Arc::clone(&processed_count);

                    // Thread-local repository
                    thread_local! {
                        static REPO: RefCell<Option<Repository>> = RefCell::new(None);
                    }

                    let result = REPO.with(|repo_cell| {
                        let mut repo_opt = repo_cell.borrow_mut();
                        if repo_opt.is_none() {
                            *repo_opt = Some(Repository::open(&repo_path_clone)?);
                        }
                        let repo = repo_opt.as_ref().unwrap();

                        process_single_commit(
                            repo,
                            commit_input,
                            &codebase_id_clone,
                            collected_at_ts,
                            include_file_changes,
                            &filter_clone,
                        )
                    });

                    // Progress tracking with periodic logging
                    let count = processed_count.fetch_add(1, Ordering::Relaxed) + 1;
                    if count % log_interval == 0 || count == batch_size {
                        let elapsed = start_time.elapsed().as_secs_f64();
                        let global_count = total_processed + count;
                        let rate = global_count as f64 / elapsed;
                        let pct = (global_count as f64 / total as f64) * 100.0;
                        log_flush!(
                            "[rust-commits] Progress: {}/{} commits ({:.1}%) - {:.0} commits/sec",
                            global_count, total, pct, rate
                        );
                    }

                    result
                })
                .collect()
        });

        // Process batch results - track successful SHAs separately from attempted
        let mut batch_commits: Vec<PyObject> = Vec::new();
        let mut batch_file_changes: Vec<PyObject> = Vec::new();
        let mut successful_shas: Vec<String> = Vec::new();
        let mut batch_errors = 0;

        for (idx, result) in results.into_iter().enumerate() {
            match result {
                Ok((commit_records, file_changes)) => {
                    // Track this SHA as successfully processed
                    successful_shas.push(batch_shas[idx].clone());
                    for record in commit_records {
                        batch_commits.push(hashmap_to_pydict(py, record));
                    }
                    for fc in file_changes {
                        batch_file_changes.push(hashmap_to_pydict(py, fc));
                    }
                }
                Err(e) => {
                    batch_errors += 1;
                    if batch_errors <= 5 {
                        log_flush!("[rust-commits] Error processing commit {}: {}", batch_shas[idx], e);
                    }
                }
            }
        }

        total_processed += batch_size;
        total_errors += batch_errors;

        // Call checkpoint callback after each batch with only successful SHAs
        // (failed commits should be retried on resume, not permanently skipped)
        if let Some(ref callback) = checkpoint_callback {
            let successful_shas_py: Vec<&str> = successful_shas.iter().map(|s| s.as_str()).collect();
            log_flush!(
                "[rust-commits] Batch {}/{} complete: {} commits ({} successful), {} records, {} file changes - calling checkpoint",
                batch_idx + 1, num_batches, batch_size, successful_shas.len(), batch_commits.len(), batch_file_changes.len()
            );

            // Call Python callback with (successful_shas, commit_records, file_changes)
            callback.call1(
                py,
                (
                    successful_shas_py,
                    batch_commits.clone(),
                    batch_file_changes.clone(),
                ),
            )?;
        }

        // Accumulate results
        all_commits.extend(batch_commits);
        all_file_changes.extend(batch_file_changes);
    }

    // Final progress reporting
    let elapsed = start_time.elapsed().as_secs_f64();
    let rate = total as f64 / elapsed;
    log_flush!(
        "[rust-commits] Processed {} commits in {:.2}s ({:.0} commits/sec)",
        total, elapsed, rate
    );

    if total_errors > 0 {
        log_flush!("[rust-commits] Total errors: {}", total_errors);
    }

    log_flush!(
        "[rust-commits] Complete: {} commit records, {} file changes",
        all_commits.len(),
        all_file_changes.len()
    );

    Ok((all_commits, all_file_changes))
}
