"""
File filtering logic matching the onboarding flow's is_analyzable criteria.

This ensures analytics SLOC matches onboarding SLOC for parity between
the codebase connection flow and the analytics flow.

The onboarding flow determines a file is "analyzable" based on:
1. Not binary
2. Not hex content (>99% hex characters including whitespace)
3. Not in a blacklisted directory (.git, driver_docs)
4. Not a blacklisted extension (.svg, .exe, .dll, etc.)
5. Not a blacklisted filename (.DS_Store, .driverignore)

Note: Unlike previous implementation, files do NOT need a recognized language
in languages.yml. Files without a recognized language are still analyzable
(they get language="Other" in the onboarding flow).
"""

import logging
import re
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Blacklist directories - files in these directories are excluded
BLACKLIST_DIRS = {
    ".git",
    "driver_docs",
}

# Blacklist extensions - files with these extensions are excluded
# Matches inspector's blacklist_file_exts
BLACKLIST_EXTENSIONS = {
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

# Blacklist filenames - specific filenames that are excluded
BLACKLIST_FILENAMES = {
    ".DS_Store",
    ".driverignore",
}

# Extensions where language syntax guarantees non-hex content.
# These can skip hex detection entirely for performance.
# Excluded: .h, .c, .cpp, .hpp, .cc (could be auto-generated hex arrays in embedded)
# Excluded: .asm, .s, .S (assembly could be hex opcodes)
SKIP_HEX_EXTENSIONS = {
    # Scripting languages (require keywords like def, class, import, etc.)
    ".py",
    ".rb",
    ".pl",
    ".pm",
    ".lua",
    ".r",
    ".R",
    ".php",
    # JVM languages (require public, class, fun, def, etc.)
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".clj",
    ".cljs",
    ".groovy",
    # JavaScript/TypeScript (require function, const, let, =>, etc.)
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".mjs",
    ".cjs",
    ".vue",
    ".svelte",
    # Systems languages (non-C family)
    ".go",
    ".rs",
    ".swift",
    # Functional languages
    ".hs",
    ".ml",
    ".mli",
    ".ex",
    ".exs",
    ".erl",
    ".hrl",
    ".fs",
    ".fsx",
    ".fsi",
    # Shell scripts (require $, |, if/then/fi, etc.)
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".psm1",
    # Config/markup (have punctuation like {}, [], :, <>, etc.)
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    # Editor/tooling
    ".vim",
    ".el",
    ".md",
    ".rst",
    ".tex",
    # SQL and query languages
    ".sql",
    ".graphql",
    ".gql",
}


def can_skip_hex_check(extension: str) -> bool:
    """
    Check if hex detection can be skipped for this extension.

    These are extensions where language syntax guarantees the file
    cannot be a pure hex dump (>99% hex characters).

    Args:
        extension: File extension including dot (e.g., '.py')

    Returns:
        True if hex check can be safely skipped
    """
    return extension.lower() in SKIP_HEX_EXTENSIONS


# Hex detection - matches inspector's evaluate_file_hex logic
# The inspector uses 99% threshold and includes whitespace (newlines, spaces)
# in the "allowed hex characters" pattern. This detects firmware/hex dump files.
HEX_THRESHOLD = 0.99
HEX_PATTERN = re.compile(r"[a-fA-F0-9\n ]")


def is_blacklisted_path(path_parts: tuple[str, ...]) -> bool:
    """
    Check if path contains a blacklisted directory.

    Args:
        path_parts: Tuple of path components (e.g., ('src', '.git', 'config'))

    Returns:
        True if any part of the path is a blacklisted directory
    """
    return any(part in BLACKLIST_DIRS for part in path_parts)


def is_blacklisted_extension(extension: str) -> bool:
    """
    Check if file has a blacklisted extension.

    Args:
        extension: File extension including dot (e.g., '.exe')

    Returns:
        True if extension is blacklisted
    """
    return extension in BLACKLIST_EXTENSIONS


def is_blacklisted_filename(filename: str) -> bool:
    """
    Check if file has a blacklisted name.

    Args:
        filename: Full filename (e.g., '.DS_Store')

    Returns:
        True if filename is blacklisted
    """
    return filename in BLACKLIST_FILENAMES


def is_hex_content(data: bytes | None) -> bool:
    """
    Check if content is mostly hex characters (>99% threshold).

    This matches the inspector's evaluate_file_hex logic which uses a 99%
    threshold and includes whitespace (newlines, spaces) in the pattern.
    This detects firmware/hex dump files which are nearly 100% hex with
    formatting whitespace.

    Args:
        data: File content as bytes

    Returns:
        True if >99% of characters match the hex pattern (hex chars + whitespace)
    """
    if not data:
        return False

    try:
        text = data.decode("utf-8", errors="ignore")
        if not text:
            return False

        # Count characters matching the hex pattern (hex + newlines + spaces)
        hex_char_count = len(HEX_PATTERN.findall(text))
        return (hex_char_count / len(text)) > HEX_THRESHOLD
    except Exception:
        return False


@cache
def _load_languages_yml() -> tuple[set[str], set[str]]:
    """
    Load recognized extensions and filenames from languages.yml.

    Returns:
        Tuple of (extensions_set, filenames_set)
    """
    import yaml

    extensions = set()
    filenames = set()

    # Try multiple possible paths for languages.yml
    # Path from file_filter.py: utils -> analytics -> src -> content_services -> python-backend
    python_backend_root = Path(__file__).parent.parent.parent.parent.parent
    possible_paths = [
        # Local development path
        python_backend_root
        / "packages/shared/shared/inspector/onboarding/languages.yml",
        # Docker container path (packages copied to /packages, not /app/packages)
        # This matches how onboarding loads it: open("/packages/shared/...")
        Path("/packages/shared/shared/inspector/onboarding/languages.yml"),
    ]

    for path in possible_paths:
        try:
            with open(path) as f:
                language_dict = yaml.safe_load(f)

            for lang_data in language_dict.values():
                if lang_data.get("extensions"):
                    extensions.update(lang_data["extensions"])
                if lang_data.get("filenames"):
                    filenames.update(lang_data["filenames"])

            logger.debug(
                f"Loaded languages.yml: {len(extensions)} extensions, {len(filenames)} filenames"
            )
            return extensions, filenames

        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning(f"Error loading languages.yml from {path}: {e}")
            continue

    logger.warning("Could not load languages.yml, using empty sets")
    return set(), set()


def has_recognized_language(extension: str, filename: str) -> bool:
    """
    Check if file has a recognized language in languages.yml.

    This determines if a file is "code" vs "documentation/config".

    Args:
        extension: File extension including dot (e.g., '.py')
        filename: Full filename (e.g., 'Dockerfile')

    Returns:
        True if file has a recognized language type
    """
    extensions, filenames = _load_languages_yml()

    # If we couldn't load languages.yml, be permissive for testing
    if not extensions and not filenames:
        # Fallback: use common code extensions
        fallback_extensions = {
            ".py",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".java",
            ".go",
            ".rs",
            ".c",
            ".cpp",
            ".h",
            ".hpp",
            ".cs",
            ".rb",
            ".php",
            ".swift",
            ".kt",
            ".scala",
            ".r",
            ".R",
            ".m",
            ".mm",
            ".pl",
            ".pm",
            ".sh",
            ".bash",
            ".zsh",
            ".fish",
            ".ps1",
            ".bat",
            ".cmd",
            ".lua",
            ".vim",
            ".el",
            ".clj",
            ".ex",
            ".exs",
            ".erl",
            ".hrl",
            ".hs",
            ".ml",
            ".mli",
            ".fs",
            ".fsx",
            ".v",
            ".sv",
            ".vhd",
            ".sql",
            ".graphql",
            ".proto",
            ".thrift",
        }
        fallback_filenames = {
            "Dockerfile",
            "Makefile",
            "Rakefile",
            "Gemfile",
            "Brewfile",
            "Vagrantfile",
            "Jenkinsfile",
            "Procfile",
        }
        return extension in fallback_extensions or filename in fallback_filenames

    return extension in extensions or filename in filenames


def is_analyzable_file(
    path_parts: tuple[str, ...],
    filename: str,
    extension: str,
    is_binary: bool,
    content: bytes | None = None,
) -> bool:
    """
    Determine if a file should be counted for SLOC.

    This matches the onboarding flow's is_analyzable logic to ensure
    analytics SLOC matches onboarding SLOC. Files do NOT need a recognized
    language - they are analyzable as long as they pass the other checks.

    Args:
        path_parts: Tuple of path components
        filename: Full filename
        extension: File extension including dot
        is_binary: Whether file is binary
        content: Optional file content for hex detection

    Returns:
        True if file should be counted for SLOC
    """
    # Binary files excluded
    if is_binary:
        return False

    # Blacklist checks
    if is_blacklisted_path(path_parts):
        return False
    if is_blacklisted_extension(extension):
        return False
    if is_blacklisted_filename(filename):
        return False

    # Hex file check (requires content)
    # Note: We do NOT require a recognized language. Files without a recognized
    # language are still analyzable (matching the onboarding flow which assigns
    # them language="Other").
    return not (content and is_hex_content(content))


def is_analyzable_path(file_path: str) -> bool:
    """
    Quick path-only check for diff filtering.

    This is used during diff processing to filter files without access to
    content. Cannot check hex content (requires file access), but catches:
    - Blacklisted directories (.git, driver_docs)
    - Blacklisted extensions (.svg, .exe, .dll, etc.)
    - Blacklisted filenames (.DS_Store, .driverignore)

    Note: This does NOT filter by file type/language. Files like .md, .json,
    .yaml are now considered analyzable (matching the onboarding flow).

    Args:
        file_path: Full file path from diff (e.g., "src/main.py")

    Returns:
        True if path appears to be analyzable
    """
    path = Path(file_path)
    return is_analyzable_file(
        path_parts=path.parts,
        filename=path.name,
        extension=path.suffix,
        is_binary=False,  # Can't determine from path alone
        content=None,  # No content access during diff parsing
    )
