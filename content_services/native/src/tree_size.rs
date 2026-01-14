//! Tree size calculation for git repositories.
//!
//! This module implements the tree walking logic that calculates
//! total bytes and lines of code for a given commit's tree.

use git2::{ObjectType, Repository, Tree, TreeWalkMode, TreeWalkResult};

use crate::filters::FileFilter;

/// Result of a tree size calculation.
#[derive(Debug, Clone, Copy, Default)]
pub struct TreeSizeResult {
    /// Total bytes of analyzable files.
    pub bytes: u64,
    /// Total lines of analyzable files.
    pub lines: u64,
}

/// Walk a git tree and calculate total bytes and lines of analyzable files.
///
/// This function recursively walks the tree, filters files using the
/// provided filter, and sums up bytes and lines for all analyzable files.
pub fn walk_tree(repo: &Repository, tree: &Tree, filter: &FileFilter) -> TreeSizeResult {
    let mut total_bytes: u64 = 0;
    let mut total_lines: u64 = 0;

    // Walk the tree recursively
    let _ = tree.walk(TreeWalkMode::PreOrder, |dir, entry| {
        // Only process blobs (files)
        if entry.kind() != Some(ObjectType::Blob) {
            return TreeWalkResult::Ok;
        }

        // Build full path
        let name = match entry.name() {
            Some(n) => n,
            None => return TreeWalkResult::Ok,
        };
        let path = if dir.is_empty() {
            name.to_string()
        } else {
            format!("{}{}", dir, name)
        };

        // Check if path is analyzable
        if !filter.is_analyzable(&path) {
            return TreeWalkResult::Ok;
        }

        // Get the blob
        let blob = match repo.find_blob(entry.id()) {
            Ok(b) => b,
            Err(_) => return TreeWalkResult::Ok,
        };

        // Skip binary files
        if blob.is_binary() {
            return TreeWalkResult::Ok;
        }

        let content = blob.content();

        // Skip hex content
        if filter.is_hex_content(content) {
            return TreeWalkResult::Ok;
        }

        // Accumulate bytes and lines
        total_bytes += content.len() as u64;
        total_lines += count_lines(content);

        TreeWalkResult::Ok
    });

    TreeSizeResult {
        bytes: total_bytes,
        lines: total_lines,
    }
}

/// Count lines in a byte slice.
///
/// This matches the Python line counting behavior:
/// - Count newlines
/// - If content is non-empty and doesn't end with newline, add 1
///
/// Examples:
/// - "line1\nline2\nline3\n" -> 3 lines
/// - "line1\nline2\nline3" -> 3 lines (no trailing newline)
/// - "" -> 0 lines
/// - "\n" -> 1 line
pub fn count_lines(content: &[u8]) -> u64 {
    if content.is_empty() {
        return 0;
    }

    // Count newlines using bytecount for SIMD acceleration
    let newline_count = bytecount::count(content, b'\n') as u64;

    // If file doesn't end with newline, add 1 for the last line
    if !content.ends_with(b"\n") {
        newline_count + 1
    } else {
        newline_count
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_count_lines_with_trailing_newline() {
        assert_eq!(count_lines(b"line1\nline2\nline3\n"), 3);
    }

    #[test]
    fn test_count_lines_without_trailing_newline() {
        assert_eq!(count_lines(b"line1\nline2\nline3"), 3);
    }

    #[test]
    fn test_count_lines_empty() {
        assert_eq!(count_lines(b""), 0);
    }

    #[test]
    fn test_count_lines_single_newline() {
        assert_eq!(count_lines(b"\n"), 1);
    }

    #[test]
    fn test_count_lines_single_line_no_newline() {
        assert_eq!(count_lines(b"hello"), 1);
    }

    #[test]
    fn test_count_lines_single_line_with_newline() {
        assert_eq!(count_lines(b"hello\n"), 1);
    }

    #[test]
    fn test_count_lines_multiple_empty_lines() {
        assert_eq!(count_lines(b"\n\n\n"), 3);
    }
}
