//! Language detection from file paths.
//!
//! Ports the Python `_get_language_from_path` function to Rust.
//! Uses static hash maps for extension and filename lookings.

use once_cell::sync::Lazy;
use std::collections::HashMap;
use std::path::Path;

/// Extension to language mapping.
/// Matches FALLBACK_EXTENSION_MAP from Python.
static EXTENSION_MAP: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    HashMap::from([
        (".py", "Python"),
        (".js", "JavaScript"),
        (".ts", "TypeScript"),
        (".tsx", "TSX"),
        (".jsx", "JSX"),
        (".java", "Java"),
        (".go", "Go"),
        (".rs", "Rust"),
        (".rb", "Ruby"),
        (".php", "PHP"),
        (".c", "C"),
        (".cpp", "C++"),
        (".cc", "C++"),
        (".h", "Header"),
        (".hpp", "C++"),
        (".cs", "C#"),
        (".swift", "Swift"),
        (".kt", "Kotlin"),
        (".scala", "Scala"),
        (".r", "R"),
        (".R", "R"),
        (".sql", "SQL"),
        (".sh", "Shell"),
        (".bash", "Shell"),
        (".zsh", "Shell"),
        (".yml", "YAML"),
        (".yaml", "YAML"),
        (".json", "JSON"),
        (".xml", "XML"),
        (".html", "HTML"),
        (".htm", "HTML"),
        (".css", "CSS"),
        (".scss", "SCSS"),
        (".sass", "Sass"),
        (".less", "Less"),
        (".md", "Markdown"),
        (".txt", "Text"),
        (".rst", "reStructuredText"),
    ])
});

/// Filename to language mapping for special files.
/// Matches FALLBACK_FILENAME_MAP from Python.
static FILENAME_MAP: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    HashMap::from([
        ("Dockerfile", "Dockerfile"),
        ("Makefile", "Makefile"),
        ("CMakeLists.txt", "CMake"),
        ("Gemfile", "Ruby"),
        ("Rakefile", "Ruby"),
        ("package.json", "JSON"),
        ("requirements.txt", "Text"),
        ("setup.py", "Python"),
        ("pyproject.toml", "TOML"),
        ("Cargo.toml", "TOML"),
        ("go.mod", "Go Module"),
        (".gitignore", "Ignore List"),
        (".dockerignore", "Ignore List"),
    ])
});

/// Get language for a file path.
///
/// Tries extension first, then falls back to filename detection.
/// Returns None if not recognized.
///
/// # Arguments
///
/// * `file_path` - Path to file (e.g., "src/main.py")
///
/// # Returns
///
/// Language name or None if not recognized
pub fn get_language_from_path(file_path: &str) -> Option<&'static str> {
    let path = Path::new(file_path);

    // Try extension first
    if let Some(ext) = path.extension() {
        if let Some(ext_str) = ext.to_str() {
            // Build extension with dot prefix
            let ext_with_dot = format!(".{}", ext_str);
            if let Some(&lang) = EXTENSION_MAP.get(ext_with_dot.as_str()) {
                return Some(lang);
            }
        }
    }

    // Try filename for special files
    if let Some(filename) = path.file_name() {
        if let Some(filename_str) = filename.to_str() {
            if let Some(&lang) = FILENAME_MAP.get(filename_str) {
                return Some(lang);
            }
        }
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_python_extension() {
        assert_eq!(get_language_from_path("main.py"), Some("Python"));
        assert_eq!(get_language_from_path("src/utils.py"), Some("Python"));
    }

    #[test]
    fn test_javascript_extension() {
        assert_eq!(get_language_from_path("app.js"), Some("JavaScript"));
    }

    #[test]
    fn test_typescript_extensions() {
        assert_eq!(get_language_from_path("app.ts"), Some("TypeScript"));
        assert_eq!(get_language_from_path("Component.tsx"), Some("TSX"));
    }

    #[test]
    fn test_special_filenames() {
        assert_eq!(get_language_from_path("Dockerfile"), Some("Dockerfile"));
        assert_eq!(get_language_from_path("Makefile"), Some("Makefile"));
        assert_eq!(get_language_from_path("CMakeLists.txt"), Some("CMake"));
    }

    #[test]
    fn test_unknown_extension() {
        assert_eq!(get_language_from_path("data.xyz"), None);
    }

    #[test]
    fn test_nested_paths() {
        assert_eq!(
            get_language_from_path("src/components/Button.tsx"),
            Some("TSX")
        );
    }

    #[test]
    fn test_case_sensitive_r() {
        assert_eq!(get_language_from_path("script.r"), Some("R"));
        assert_eq!(get_language_from_path("script.R"), Some("R"));
    }
}
