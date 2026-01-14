//! File filtering logic matching the Python implementation exactly.
//!
//! This module ports the filtering logic from:
//! `content_services/src/analytics/utils/file_filter.py`
//!
//! IMPORTANT: These constants must match the Python implementation exactly.
//! Run `pytest tests/analytics/unit/test_filter_constants.py -v -s` to verify.

use once_cell::sync::Lazy;
use std::collections::HashSet;
use std::path::Path;

/// Directories that should be excluded from analysis.
/// Files inside these directories are not counted for SLOC.
static BLACKLIST_DIRS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    HashSet::from([
        ".git",
        "driver_docs",
    ])
});

/// File extensions that should be excluded from analysis.
/// Note: Both `.bin` and `.BIN` are listed explicitly (no case folding).
static BLACKLIST_EXTENSIONS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    HashSet::from([
        ".BIN",
        ".DAT",
        ".a",
        ".axf",
        ".bin",
        ".cdylib",
        ".dat",
        ".dll",
        ".dylib",
        ".elf",
        ".exe",
        ".hex",
        ".o",
        ".so",
        ".svg",
    ])
});

/// Specific filenames that should be excluded from analysis.
static BLACKLIST_FILENAMES: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    HashSet::from([
        ".DS_Store",
        ".driverignore",
    ])
});

/// Hex content detection threshold.
/// Files with >99% hex characters (including whitespace) are excluded.
pub const HEX_THRESHOLD: f64 = 0.99;

/// File filter for determining if a file should be counted for SLOC.
pub struct FileFilter;

impl FileFilter {
    /// Create a new file filter.
    pub fn new() -> Self {
        FileFilter
    }

    /// Check if a path should be analyzed (counted for SLOC).
    ///
    /// This matches the Python `is_analyzable_path()` function.
    /// It checks:
    /// - Not in a blacklisted directory (.git, driver_docs)
    /// - Not a blacklisted extension (.svg, .exe, .dll, etc.)
    /// - Not a blacklisted filename (.DS_Store, .driverignore)
    ///
    /// Note: Binary detection and hex content detection are handled separately
    /// during blob processing.
    pub fn is_analyzable(&self, path: &str) -> bool {
        let path_obj = Path::new(path);

        // Check for blacklisted directories in path
        for component in path_obj.components() {
            if let std::path::Component::Normal(name) = component {
                if let Some(name_str) = name.to_str() {
                    if BLACKLIST_DIRS.contains(name_str) {
                        return false;
                    }
                }
            }
        }

        // Check for blacklisted filename
        if let Some(filename) = path_obj.file_name() {
            if let Some(filename_str) = filename.to_str() {
                if BLACKLIST_FILENAMES.contains(filename_str) {
                    return false;
                }
            }
        }

        // Check for blacklisted extension
        if let Some(extension) = path_obj.extension() {
            if let Some(ext_str) = extension.to_str() {
                // Build the extension with the dot prefix
                let ext_with_dot = format!(".{}", ext_str);
                if BLACKLIST_EXTENSIONS.contains(ext_with_dot.as_str()) {
                    return false;
                }
            }
        }

        true
    }

    /// Check if content is mostly hex characters (>99% threshold).
    ///
    /// This matches the Python `is_hex_content()` function.
    /// The pattern includes hex chars (0-9, a-f, A-F) plus whitespace (newlines, spaces).
    /// This detects firmware/hex dump files.
    pub fn is_hex_content(&self, content: &[u8]) -> bool {
        if content.is_empty() {
            return false;
        }

        // Try to decode as UTF-8, ignoring errors
        let text = String::from_utf8_lossy(content);
        if text.is_empty() {
            return false;
        }

        let total_chars = text.len();
        let hex_chars = text.chars().filter(|c| is_hex_char(*c)).count();

        (hex_chars as f64 / total_chars as f64) > HEX_THRESHOLD
    }
}

impl Default for FileFilter {
    fn default() -> Self {
        Self::new()
    }
}

/// Check if a character is a hex character (including whitespace).
/// Matches the Python pattern: `[a-fA-F0-9\n ]`
#[inline]
fn is_hex_char(c: char) -> bool {
    matches!(c, '0'..='9' | 'a'..='f' | 'A'..='F' | '\n' | ' ')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_blacklisted_directories() {
        let filter = FileFilter::new();

        // Should be blacklisted
        assert!(!filter.is_analyzable(".git/config"));
        assert!(!filter.is_analyzable(".git/objects/pack/file"));
        assert!(!filter.is_analyzable("driver_docs/README.md"));
        assert!(!filter.is_analyzable("driver_docs/api/overview.md"));

        // Should NOT be blacklisted
        assert!(filter.is_analyzable("src/main.py"));
        assert!(filter.is_analyzable("git_utils.py")); // "git" in name, not directory
        assert!(filter.is_analyzable("docs/readme.md"));
    }

    #[test]
    fn test_blacklisted_extensions() {
        let filter = FileFilter::new();

        // Should be blacklisted
        assert!(!filter.is_analyzable("assets/logo.svg"));
        assert!(!filter.is_analyzable("build/output.exe"));
        assert!(!filter.is_analyzable("lib/mylib.dll"));
        assert!(!filter.is_analyzable("obj/file.o"));
        assert!(!filter.is_analyzable("data/dump.hex"));
        assert!(!filter.is_analyzable("firmware.bin"));

        // Should NOT be blacklisted
        assert!(filter.is_analyzable("src/main.py"));
        assert!(filter.is_analyzable("src/app.js"));
        assert!(filter.is_analyzable("src/Component.tsx"));
        assert!(filter.is_analyzable("package.json"));
        assert!(filter.is_analyzable("README.md"));
    }

    #[test]
    fn test_blacklisted_filenames() {
        let filter = FileFilter::new();

        // Should be blacklisted
        assert!(!filter.is_analyzable(".DS_Store"));
        assert!(!filter.is_analyzable("src/.DS_Store"));
        assert!(!filter.is_analyzable(".driverignore"));

        // Should NOT be blacklisted
        assert!(filter.is_analyzable("Dockerfile"));
        assert!(filter.is_analyzable("Makefile"));
        assert!(filter.is_analyzable(".gitignore"));
    }

    #[test]
    fn test_case_sensitivity() {
        let filter = FileFilter::new();

        // Both .bin and .BIN are blacklisted explicitly
        assert!(!filter.is_analyzable("firmware.bin"));
        assert!(!filter.is_analyzable("firmware.BIN"));

        // But .Bin (mixed case) is NOT blacklisted
        assert!(filter.is_analyzable("firmware.Bin"));
    }

    #[test]
    fn test_hex_content_detection() {
        let filter = FileFilter::new();

        // Pure hex with whitespace (like firmware files)
        let hex_dump = b"00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f\n".repeat(100);
        assert!(filter.is_hex_content(&hex_dump));

        // Pure hex without whitespace
        let pure_hex = b"0123456789abcdefABCDEF".repeat(100);
        assert!(filter.is_hex_content(&pure_hex));

        // Normal code should NOT be detected as hex
        let code = b"def hello():\n    print('Hello, World!')\n    return True\n";
        assert!(!filter.is_hex_content(code));

        // JSON should NOT be detected as hex
        let json = b"{\"name\": \"test\", \"value\": 123, \"active\": true}\n";
        assert!(!filter.is_hex_content(json));

        // Empty content
        assert!(!filter.is_hex_content(b""));
    }

    #[test]
    fn test_path_filtering_comprehensive() {
        let filter = FileFilter::new();

        // All test cases from Python test_filter_constants.py
        let test_cases = [
            ("src/main.py", true),
            ("src/utils/helpers.py", true),
            ("main.py", true),
            ("src/app.js", true),
            ("src/Component.tsx", true),
            ("package.json", true),
            ("README.md", true),
            ("Dockerfile", true),
            ("Makefile", true),
            (".gitignore", true),
            (".git/config", false),
            (".git/objects/pack/file", false),
            ("driver_docs/README.md", false),
            ("assets/logo.svg", false),
            ("build/output.exe", false),
            ("lib/mylib.dll", false),
            ("obj/file.o", false),
            (".DS_Store", false),
            ("src/.DS_Store", false),
            (".driverignore", false),
            ("firmware.bin", false),
            ("data/dump.hex", false),
        ];

        for (path, expected) in test_cases {
            assert_eq!(
                filter.is_analyzable(path),
                expected,
                "Failed for path: {}",
                path
            );
        }
    }
}
