//! Delta calculation between parent and child commits.
//!
//! This module calculates the difference in tree size between two commits
//! by walking the diff between their trees.

use git2::{Commit, Delta, DiffDelta, DiffOptions, Repository};

use crate::cache::SharedBlobCache;
use crate::filters::FileFilter;
use crate::tree_size::count_lines;

/// Result of a delta calculation.
#[derive(Debug, Clone, Copy, Default)]
pub struct DeltaResult {
    /// Change in bytes (positive = added, negative = removed).
    pub added_bytes: i64,
    /// Change in lines (positive = added, negative = removed).
    pub added_lines: i64,
}

/// Calculate the delta between a parent commit and child commit.
///
/// The delta represents the change in total tree size:
/// `tree_size(child) = tree_size(parent) + delta`
pub fn calculate_delta(
    repo: &Repository,
    parent: &Commit,
    child: &Commit,
    filter: &FileFilter,
    cache: &SharedBlobCache,
) -> Result<DeltaResult, git2::Error> {
    let parent_tree = parent.tree()?;
    let child_tree = child.tree()?;

    let mut opts = DiffOptions::new();
    opts.ignore_submodules(true);

    let diff = repo.diff_tree_to_tree(Some(&parent_tree), Some(&child_tree), Some(&mut opts))?;

    let mut added_bytes: i64 = 0;
    let mut added_lines: i64 = 0;

    // Process each delta in the diff
    diff.foreach(
        &mut |delta: DiffDelta, _progress| {
            process_delta(
                repo,
                &delta,
                filter,
                cache,
                &mut added_bytes,
                &mut added_lines,
            );
            true
        },
        None, // binary callback
        None, // hunk callback
        None, // line callback
    )?;

    Ok(DeltaResult {
        added_bytes,
        added_lines,
    })
}

/// Process a single diff delta and update byte/line counts.
fn process_delta(
    repo: &Repository,
    delta: &DiffDelta,
    filter: &FileFilter,
    cache: &SharedBlobCache,
    added_bytes: &mut i64,
    added_lines: &mut i64,
) {
    match delta.status() {
        Delta::Added => {
            // New file: add its size
            if let Some(new_file) = delta.new_file().path() {
                let path = new_file.to_string_lossy();
                if filter.is_analyzable(&path) {
                    let (bytes, lines) = get_blob_size(repo, delta.new_file().id(), filter, cache);
                    *added_bytes += bytes as i64;
                    *added_lines += lines as i64;
                }
            }
        }
        Delta::Deleted => {
            // Removed file: subtract its size
            if let Some(old_file) = delta.old_file().path() {
                let path = old_file.to_string_lossy();
                if filter.is_analyzable(&path) {
                    let (bytes, lines) = get_blob_size(repo, delta.old_file().id(), filter, cache);
                    *added_bytes -= bytes as i64;
                    *added_lines -= lines as i64;
                }
            }
        }
        Delta::Modified => {
            // Modified file: compute difference
            let old_path = delta
                .old_file()
                .path()
                .map(|p| p.to_string_lossy().to_string());
            let new_path = delta
                .new_file()
                .path()
                .map(|p| p.to_string_lossy().to_string());

            let old_analyzable = old_path
                .as_ref()
                .map(|p| filter.is_analyzable(p))
                .unwrap_or(false);
            let new_analyzable = new_path
                .as_ref()
                .map(|p| filter.is_analyzable(p))
                .unwrap_or(false);

            if old_analyzable {
                let (bytes, lines) = get_blob_size(repo, delta.old_file().id(), filter, cache);
                *added_bytes -= bytes as i64;
                *added_lines -= lines as i64;
            }

            if new_analyzable {
                let (bytes, lines) = get_blob_size(repo, delta.new_file().id(), filter, cache);
                *added_bytes += bytes as i64;
                *added_lines += lines as i64;
            }
        }
        Delta::Renamed => {
            // Rename: file moved, tree size only changes if analyzability boundary crossed
            let old_path = delta
                .old_file()
                .path()
                .map(|p| p.to_string_lossy().to_string());
            let new_path = delta
                .new_file()
                .path()
                .map(|p| p.to_string_lossy().to_string());

            let old_analyzable = old_path
                .as_ref()
                .map(|p| filter.is_analyzable(p))
                .unwrap_or(false);
            let new_analyzable = new_path
                .as_ref()
                .map(|p| filter.is_analyzable(p))
                .unwrap_or(false);

            let old_oid = delta.old_file().id();
            let new_oid = delta.new_file().id();

            if old_oid != new_oid {
                // Content also changed during rename
                if old_analyzable {
                    let (bytes, lines) = get_blob_size(repo, old_oid, filter, cache);
                    *added_bytes -= bytes as i64;
                    *added_lines -= lines as i64;
                }
                if new_analyzable {
                    let (bytes, lines) = get_blob_size(repo, new_oid, filter, cache);
                    *added_bytes += bytes as i64;
                    *added_lines += lines as i64;
                }
            } else if old_analyzable != new_analyzable {
                // Same content, but path changed analyzability
                if new_analyzable {
                    // Moved INTO analyzable path
                    let (bytes, lines) = get_blob_size(repo, new_oid, filter, cache);
                    *added_bytes += bytes as i64;
                    *added_lines += lines as i64;
                } else {
                    // Moved OUT OF analyzable path
                    let (bytes, lines) = get_blob_size(repo, old_oid, filter, cache);
                    *added_bytes -= bytes as i64;
                    *added_lines -= lines as i64;
                }
            }
            // If both analyzable and same content, no change to totals (file just moved)
        }
        Delta::Copied => {
            // Copy: new file added to tree, always increases tree size if analyzable
            let new_path = delta
                .new_file()
                .path()
                .map(|p| p.to_string_lossy().to_string());

            let new_analyzable = new_path
                .as_ref()
                .map(|p| filter.is_analyzable(p))
                .unwrap_or(false);

            if new_analyzable {
                let (bytes, lines) = get_blob_size(repo, delta.new_file().id(), filter, cache);
                *added_bytes += bytes as i64;
                *added_lines += lines as i64;
            }
        }
        _ => {
            // Ignore other delta types (Unmodified, Typechange, etc.)
        }
    }
}

/// Get blob size from cache or compute it.
///
/// Returns (bytes, lines) for the blob. Returns (0, 0) for:
/// - Zero OID
/// - Binary files
/// - Hex content files
/// - Blobs that can't be read
fn get_blob_size(
    repo: &Repository,
    oid: git2::Oid,
    filter: &FileFilter,
    cache: &SharedBlobCache,
) -> (u64, u64) {
    if oid.is_zero() {
        return (0, 0);
    }

    // Check bytes cache first
    let bytes = cache.get_or_compute_bytes(oid, || {
        match repo.find_blob(oid) {
            Ok(blob) => {
                // Skip binary files
                if blob.is_binary() {
                    return 0;
                }

                let content = blob.content();

                // Skip hex content (firmware dumps, etc.)
                if filter.is_hex_content(content) {
                    return 0;
                }

                content.len() as u64
            }
            Err(_) => 0,
        }
    });

    // If bytes is 0, lines must also be 0
    if bytes == 0 {
        return (0, 0);
    }

    let lines = cache.get_or_compute_lines(oid, || {
        match repo.find_blob(oid) {
            Ok(blob) if !blob.is_binary() => {
                let content = blob.content();
                if filter.is_hex_content(content) {
                    0
                } else {
                    count_lines(content)
                }
            }
            _ => 0,
        }
    });

    (bytes, lines)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_delta_result_default() {
        let result = DeltaResult::default();
        assert_eq!(result.added_bytes, 0);
        assert_eq!(result.added_lines, 0);
    }
}
