"""
Export filter constants for Rust implementation parity testing.

These tests document the EXACT filter values that must be ported to Rust.
Run this test to generate the constants for the Rust implementation.

Usage:
    poetry run pytest tests/analytics/unit/test_filter_constants.py -v -s

The output will show the exact sets to port to filters.rs.
"""


class TestFilterConstantsExport:
    """Export filter constants for Rust implementation."""

    def test_print_blacklist_directories(self):
        """Print the exact blacklisted directories for Rust."""
        from analytics.utils.file_filter import BLACKLIST_DIRS

        print("\n=== BLACKLIST_DIRS (for filters.rs) ===")
        print(f"Count: {len(BLACKLIST_DIRS)}")
        print("Values:")
        for d in sorted(BLACKLIST_DIRS):
            print(f'    "{d}",')
        print()

        # Document the expected values
        assert {".git", "driver_docs"} == BLACKLIST_DIRS

    def test_print_blacklist_extensions(self):
        """Print the exact blacklisted extensions for Rust."""
        from analytics.utils.file_filter import BLACKLIST_EXTENSIONS

        print("\n=== BLACKLIST_EXTENSIONS (for filters.rs) ===")
        print(f"Count: {len(BLACKLIST_EXTENSIONS)}")
        print("Values:")
        for ext in sorted(BLACKLIST_EXTENSIONS):
            print(f'    "{ext}",')
        print()

        # Document the expected values
        expected = {
            ".svg",
            ".hex",
            ".bin",
            ".BIN",
            ".dat",
            ".DAT",
            ".exe",
            ".o",
            ".a",
            ".so",
            ".dll",
            ".dylib",
            ".cdylib",
            ".axf",
            ".elf",
        }
        assert expected == BLACKLIST_EXTENSIONS

    def test_print_blacklist_filenames(self):
        """Print the exact blacklisted filenames for Rust."""
        from analytics.utils.file_filter import BLACKLIST_FILENAMES

        print("\n=== BLACKLIST_FILENAMES (for filters.rs) ===")
        print(f"Count: {len(BLACKLIST_FILENAMES)}")
        print("Values:")
        for name in sorted(BLACKLIST_FILENAMES):
            print(f'    "{name}",')
        print()

        # Document the expected values
        assert {".DS_Store", ".driverignore"} == BLACKLIST_FILENAMES

    def test_hex_threshold(self):
        """Document the hex detection threshold."""
        from analytics.utils.file_filter import HEX_THRESHOLD

        print("\n=== HEX_THRESHOLD (for filters.rs) ===")
        print(f"Value: {HEX_THRESHOLD}")
        print()

        assert HEX_THRESHOLD == 0.99


class TestFilterBehaviorDocumentation:
    """Document filter behavior with test cases for Rust implementation."""

    def test_document_path_filtering(self):
        """Generate test cases for Rust path filtering."""
        from analytics.utils.file_filter import is_analyzable_path

        test_cases = [
            # (path, expected_result, reason)
            ("src/main.py", True, "normal Python file"),
            ("src/utils/helpers.py", True, "nested Python file"),
            ("main.py", True, "root-level Python file"),
            ("src/app.js", True, "JavaScript file"),
            ("src/Component.tsx", True, "TypeScript React file"),
            ("package.json", True, "JSON config file"),
            ("README.md", True, "Markdown file"),
            ("Dockerfile", True, "Dockerfile"),
            ("Makefile", True, "Makefile"),
            (".gitignore", True, "gitignore (not blacklisted)"),
            (".git/config", False, "inside .git directory"),
            (".git/objects/pack/file", False, "deep inside .git"),
            ("driver_docs/README.md", False, "inside driver_docs"),
            ("assets/logo.svg", False, "SVG file (blacklisted extension)"),
            ("build/output.exe", False, "EXE file (blacklisted extension)"),
            ("lib/mylib.dll", False, "DLL file (blacklisted extension)"),
            ("obj/file.o", False, "object file (blacklisted extension)"),
            (".DS_Store", False, "DS_Store (blacklisted filename)"),
            ("src/.DS_Store", False, "nested DS_Store"),
            (".driverignore", False, "driverignore (blacklisted filename)"),
            ("firmware.bin", False, "binary firmware"),
            ("data/dump.hex", False, "hex dump file"),
        ]

        print("\n=== PATH FILTERING TEST CASES (for Rust tests) ===")
        print("// These test cases document expected behavior\n")

        for path, expected, reason in test_cases:
            result = is_analyzable_path(path)
            status = "✓" if result == expected else "✗"
            print(f'{status} is_analyzable("{path}") = {result}  // {reason}')
            assert result == expected, f"Failed: {path} - {reason}"

    def test_document_extension_normalization(self):
        """Document how extensions are handled."""
        from analytics.utils.file_filter import is_blacklisted_extension

        print("\n=== EXTENSION HANDLING ===")
        print("Note: Rust must handle case sensitivity the same way\n")

        # Case sensitivity tests
        test_cases = [
            (".bin", True, "lowercase"),
            (".BIN", True, "uppercase (also blacklisted separately)"),
            (".Bin", False, "mixed case (NOT blacklisted)"),
            (".py", False, "Python (not blacklisted)"),
            (".PY", False, "Python uppercase (not blacklisted)"),
        ]

        for ext, expected, note in test_cases:
            result = is_blacklisted_extension(ext)
            status = "✓" if result == expected else "✗"
            print(f'{status} is_blacklisted("{ext}") = {result}  // {note}')
            assert result == expected, f"Failed: {ext}"

        print("\nIMPORTANT: The blacklist contains BOTH .bin AND .BIN explicitly!")
        print("Rust should use a HashSet with exact string matching, not case-folding.")


class TestLineCountingBehavior:
    """Document line counting behavior for Rust implementation."""

    def test_line_counting_with_trailing_newline(self):
        """Document: files WITH trailing newline."""
        content = b"line1\nline2\nline3\n"
        lines = content.count(b"\n")  # Simple newline count

        print("\n=== LINE COUNTING: WITH TRAILING NEWLINE ===")
        print('Content: "line1\\nline2\\nline3\\n"')
        print(f"Byte length: {len(content)}")
        print(f"Newline count: {lines}")
        print("Expected lines: 3")
        print()

        assert lines == 3

    def test_line_counting_without_trailing_newline(self):
        """Document: files WITHOUT trailing newline need +1."""
        content = b"line1\nline2\nline3"
        newlines = content.count(b"\n")

        print("\n=== LINE COUNTING: WITHOUT TRAILING NEWLINE ===")
        print('Content: "line1\\nline2\\nline3" (no trailing newline)')
        print(f"Byte length: {len(content)}")
        print(f"Newline count: {newlines}")
        print("Expected lines: 3 (need to add 1)")
        print()

        # Rust logic:
        # if !content.ends_with(b"\n") && !content.is_empty():
        #     lines = newlines + 1
        expected_lines = newlines + 1
        assert expected_lines == 3

    def test_line_counting_empty_file(self):
        """Document: empty files have 0 lines."""
        content = b""

        print("\n=== LINE COUNTING: EMPTY FILE ===")
        print('Content: ""')
        print(f"Byte length: {len(content)}")
        print("Expected lines: 0")
        print()

        assert len(content) == 0

    def test_line_counting_single_newline(self):
        """Document: single newline = 0 or 1 line depending on interpretation."""
        content = b"\n"
        newlines = content.count(b"\n")

        print("\n=== LINE COUNTING: SINGLE NEWLINE ===")
        print('Content: "\\n"')
        print(f"Byte length: {len(content)}")
        print(f"Newline count: {newlines}")
        print("Expected: 1 line (empty line with newline)")
        print()

        # This represents one empty line
        assert newlines == 1


class TestRustCodeGeneration:
    """Generate Rust code snippets for the filter implementation."""

    def test_generate_rust_constants(self):
        """Generate Rust const declarations."""
        from analytics.utils.file_filter import (
            BLACKLIST_DIRS,
            BLACKLIST_EXTENSIONS,
            BLACKLIST_FILENAMES,
            HEX_THRESHOLD,
        )

        print("\n" + "=" * 60)
        print("RUST CODE FOR filters.rs")
        print("=" * 60)

        print("""
use std::collections::HashSet;
use once_cell::sync::Lazy;

/// Directories that should be excluded from analysis
pub static BLACKLIST_DIRS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    HashSet::from([""")
        for d in sorted(BLACKLIST_DIRS):
            print(f'        "{d}",')
        print("""    ])
});

/// File extensions that should be excluded from analysis
pub static BLACKLIST_EXTENSIONS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    HashSet::from([""")
        for ext in sorted(BLACKLIST_EXTENSIONS):
            print(f'        "{ext}",')
        print("""    ])
});

/// Specific filenames that should be excluded from analysis
pub static BLACKLIST_FILENAMES: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    HashSet::from([""")
        for name in sorted(BLACKLIST_FILENAMES):
            print(f'        "{name}",')
        print("    ])")
        print("});")
        print()
        print(
            "/// Hex content detection threshold (files with >99% hex chars are excluded)"
        )
        print(f"pub const HEX_THRESHOLD: f64 = {HEX_THRESHOLD};")
