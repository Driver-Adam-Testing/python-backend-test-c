"""Language detection for analytics.

A11: Language Detection

Detects primary language from file changes using:
1. File extension mapping (from Linguist languages.yml)
2. Special filename detection (Dockerfile, Makefile, etc.)
"""

import logging
from collections import defaultdict
from pathlib import Path

# Import existing language detection from shared utils
try:
    from shared.inspector.onboarding.onboard_utils import (
        get_file_type_from_extension,
        get_file_type_from_filename,
    )
except ImportError:
    # Fallback for testing without shared package
    get_file_type_from_extension = None
    get_file_type_from_filename = None

logger = logging.getLogger(__name__)

# Fallback extension mapping when shared utils not available
FALLBACK_EXTENSION_MAP = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TSX",
    ".jsx": "JSX",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".h": "Header",
    ".hpp": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".r": "R",
    ".R": "R",
    ".sql": "SQL",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".json": "JSON",
    ".xml": "XML",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",
    ".md": "Markdown",
    ".txt": "Text",
    ".rst": "reStructuredText",
}

FALLBACK_FILENAME_MAP = {
    "Dockerfile": "Dockerfile",
    "Makefile": "Makefile",
    "CMakeLists.txt": "CMake",
    "Gemfile": "Ruby",
    "Rakefile": "Ruby",
    "package.json": "JSON",
    "requirements.txt": "Text",
    "setup.py": "Python",
    "pyproject.toml": "TOML",
    "Cargo.toml": "TOML",
    "go.mod": "Go Module",
    ".gitignore": "Ignore List",
    ".dockerignore": "Ignore List",
}


def _get_language_from_path(file_path: str) -> str | None:
    """Get language for a file path.

    Tries extension first, then falls back to filename detection.
    Uses shared utils if available, otherwise uses fallback maps.

    Args:
        file_path: Path to file (e.g., 'src/main.py')

    Returns:
        Language name or None if not recognized
    """
    path = Path(file_path)
    extension = path.suffix
    filename = path.name

    # Try extension first
    if extension:
        # Try shared utils (may fail if languages.yml not available)
        if get_file_type_from_extension:
            try:
                lang = get_file_type_from_extension(extension)
                if lang:
                    return lang
            except Exception:
                pass  # Fall through to fallback

        # Fallback mapping
        lang = FALLBACK_EXTENSION_MAP.get(extension)
        if lang:
            return lang

    # Try filename for special files
    if get_file_type_from_filename:
        try:
            lang = get_file_type_from_filename(filename)
            if lang:
                return lang
        except Exception:
            pass  # Fall through to fallback

    # Fallback mapping
    lang = FALLBACK_FILENAME_MAP.get(filename)
    if lang:
        return lang

    return None


def detect_primary_language(file_changes: list[dict]) -> str | None:
    """Detect primary language from file changes.

    Determines the primary language based on bytes added, not file count.
    This ensures large files in a language count more than many small files.

    Args:
        file_changes: List of file change dicts with 'file_path' and 'addition_bytes'

    Returns:
        Primary language name, or None if no languages detected
    """
    if not file_changes:
        return None

    language_bytes: dict[str, int] = defaultdict(int)

    for fc in file_changes:
        file_path = fc.get("file_path", "")
        addition_bytes = fc.get("addition_bytes", 0)

        if not file_path:
            continue

        lang = _get_language_from_path(file_path)
        if lang:
            language_bytes[lang] += addition_bytes

    if not language_bytes:
        return None

    # Return language with most bytes
    primary_language = max(language_bytes.items(), key=lambda x: x[1])[0]

    logger.debug(f"Language detection: {dict(language_bytes)} -> {primary_language}")

    return primary_language
