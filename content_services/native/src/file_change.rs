//! File change extraction from diffs.
//!
//! Ports Python's `_extract_file_changes` function to Rust.
//! Extracts file-level change records for cold storage.

use crate::commit::PyValue;
use crate::error::AnalyticsError;
use crate::filters::FileFilter;
use crate::language::get_language_from_path;
use git2::{Delta, Diff};
use std::collections::HashMap;
use std::path::Path;

/// Map git2 Delta status to change type string
fn delta_to_change_type(status: Delta) -> &'static str {
    match status {
        Delta::Added => "added",
        Delta::Deleted => "deleted",
        Delta::Modified => "modified",
        Delta::Renamed => "renamed",
        Delta::Copied => "copied",
        Delta::Typechange => "typechange",
        _ => "modified",
    }
}

/// Extract file extension from path
fn get_extension(path: &str) -> Option<String> {
    Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{}", e))
}

/// Extract file-level changes from a diff.
///
/// Only includes analyzable code files (matching inspector criteria).
pub fn extract_file_changes(
    diff: &Diff,
    commit_sha: &str,
    codebase_id: &str,
    commit_date: &str,
    filter: &FileFilter,
) -> Result<Vec<HashMap<String, PyValue>>, AnalyticsError> {
    let mut file_changes = Vec::new();

    for delta_idx in 0..diff.deltas().len() {
        let delta = diff.get_delta(delta_idx).unwrap();

        // Get file path (prefer new file, fall back to old for deletes)
        let new_path = delta.new_file().path().and_then(|p| p.to_str());
        let old_path = delta.old_file().path().and_then(|p| p.to_str());
        let file_path = new_path.or(old_path);

        let Some(path) = file_path else { continue };

        // Filter to analyzable files only
        if !filter.is_analyzable(path) {
            continue;
        }

        let change_type = delta_to_change_type(delta.status());
        let previous_path = if change_type == "renamed" {
            old_path.map(|s| s.to_string())
        } else {
            None
        };

        // Get line and byte stats from patch
        let mut additions_lines = 0i32;
        let mut deletions_lines = 0i32;
        let mut addition_bytes = 0i64;
        let mut deletion_bytes = 0i64;
        let mut has_patch_data = false;

        if let Ok(patch) = git2::Patch::from_diff(diff, delta_idx) {
            if let Some(patch) = patch {
                has_patch_data = true;

                let (_, adds, dels) = patch.line_stats().unwrap_or((0, 0, 0));
                additions_lines = adds as i32;
                deletions_lines = dels as i32;

                // Calculate bytes from patch content
                for hunk_idx in 0..patch.num_hunks() {
                    if let Ok((_, num_lines)) = patch.hunk(hunk_idx) {
                        for line_idx in 0..num_lines {
                            if let Ok(line) = patch.line_in_hunk(hunk_idx, line_idx) {
                                match line.origin() {
                                    '+' => {
                                        addition_bytes += line.content().len() as i64;
                                    }
                                    '-' => {
                                        deletion_bytes += line.content().len() as i64;
                                    }
                                    _ => {}
                                }
                            }
                        }
                    }
                }
            }
        }

        let changes_lines = additions_lines + deletions_lines;
        let file_sloc = (addition_bytes + deletion_bytes) / 50;
        let file_extension = get_extension(path);
        let file_language = get_language_from_path(path).map(|s| s.to_string());

        let mut record: HashMap<String, PyValue> = HashMap::new();
        record.insert("codebase_id".to_string(), PyValue::Str(codebase_id.to_string()));
        record.insert("commit_sha".to_string(), PyValue::Str(commit_sha.to_string()));
        record.insert("file_path".to_string(), PyValue::Str(path.to_string()));
        record.insert("commit_date".to_string(), PyValue::Str(commit_date.to_string()));
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

    Ok(file_changes)
}
