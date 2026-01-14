#!/usr/bin/env python3
"""
Analyze which file extensions contain hex content in a repository.

This helps identify which extensions are safe to skip hex detection for.

Run from content_services directory:
    poetry run python tests/analytics/analyze_hex_by_extension.py /path/to/repo
"""

import sys
from collections import defaultdict
from pathlib import Path

import pygit2

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from analytics.utils.file_filter import BLACKLIST_EXTENSIONS, is_hex_content


def analyze_repo(repo_path: Path) -> None:
    """Analyze hex content by file extension across all commits."""
    repo = pygit2.Repository(str(repo_path))

    # Track stats by extension
    # extension -> {"total": count, "hex": count, "binary": count, "examples": [paths]}
    stats: dict[str, dict] = defaultdict(
        lambda: {
            "total": 0,
            "hex": 0,
            "binary": 0,
            "hex_examples": [],
            "non_hex_examples": [],
        }
    )

    # Track unique blobs we've already analyzed
    seen_blobs: dict[
        str, tuple[str, bool, bool]
    ] = {}  # oid -> (ext, is_binary, is_hex)

    print(f"Repository: {repo_path}")
    print("Scanning all commits for unique blobs...\n")

    # Walk all commits
    commit_count = 0
    for branch in repo.branches.local:
        try:
            branch_ref = repo.branches[branch]
            head_oid = branch_ref.peel().id
            for commit in repo.walk(head_oid, pygit2.GIT_SORT_TOPOLOGICAL):
                commit_count += 1
                if commit_count % 500 == 0:
                    print(
                        f"  Processed {commit_count} commits, found {len(seen_blobs)} unique blobs..."
                    )

                # Walk the tree
                _walk_tree(repo, commit.tree, "", seen_blobs, stats)
        except Exception as e:
            print(f"  Error walking branch {branch}: {e}")
            continue

    print(f"\nProcessed {commit_count} commits")
    print(f"Found {len(seen_blobs)} unique blobs\n")

    # Print results
    print("=" * 80)
    print("HEX CONTENT ANALYSIS BY EXTENSION")
    print("=" * 80)

    # Sort by total count descending
    sorted_exts = sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True)

    # First show extensions with hex files
    print("\n--- EXTENSIONS WITH HEX FILES ---")
    print(
        f"{'Extension':<15} {'Total':>8} {'Hex':>8} {'Binary':>8} {'Hex%':>8}  Examples"
    )
    print("-" * 80)

    has_hex = [(ext, s) for ext, s in sorted_exts if s["hex"] > 0]
    for ext, s in has_hex:
        ext_display = ext if ext else "(no ext)"
        hex_pct = (s["hex"] / s["total"] * 100) if s["total"] > 0 else 0
        examples = ", ".join(s["hex_examples"][:3])
        print(
            f"{ext_display:<15} {s['total']:>8} {s['hex']:>8} {s['binary']:>8} {hex_pct:>7.1f}%  {examples}"
        )

    if not has_hex:
        print("  (none found)")

    # Then show top extensions WITHOUT hex files (candidates for skipping)
    print("\n--- TOP EXTENSIONS WITHOUT HEX FILES (safe to skip hex check) ---")
    print(f"{'Extension':<15} {'Total':>8} {'Binary':>8}  Sample files")
    print("-" * 80)

    no_hex = [(ext, s) for ext, s in sorted_exts if s["hex"] == 0 and s["total"] >= 10]
    for ext, s in no_hex[:30]:  # Top 30
        ext_display = ext if ext else "(no ext)"
        examples = ", ".join(s["non_hex_examples"][:3])
        print(f"{ext_display:<15} {s['total']:>8} {s['binary']:>8}  {examples}")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    total_blobs = sum(s["total"] for s in stats.values())
    total_hex = sum(s["hex"] for s in stats.values())
    total_binary = sum(s["binary"] for s in stats.values())

    print(f"Total unique blobs:  {total_blobs}")
    print(f"Binary blobs:        {total_binary} ({total_binary/total_blobs*100:.1f}%)")
    print(f"Hex content blobs:   {total_hex} ({total_hex/total_blobs*100:.1f}%)")

    # Extensions safe to skip
    safe_extensions = {
        ext for ext, s in stats.items() if s["hex"] == 0 and s["total"] >= 5
    }
    safe_blob_count = sum(
        s["total"] for ext, s in stats.items() if ext in safe_extensions
    )

    print(f"\nExtensions with 0 hex files (5+ samples): {len(safe_extensions)}")
    print(
        f"Blobs covered by safe extensions: {safe_blob_count} ({safe_blob_count/total_blobs*100:.1f}%)"
    )

    print("\n--- SAFE EXTENSIONS SET (copy-paste ready) ---")
    # Filter to common code extensions
    code_safe = sorted([ext for ext in safe_extensions if ext.startswith(".")])
    print("SKIP_HEX_EXTENSIONS = {")
    for i in range(0, len(code_safe), 8):
        chunk = code_safe[i : i + 8]
        print(f"    {', '.join(repr(e) for e in chunk)},")
    print("}")


def _walk_tree(
    repo: pygit2.Repository,
    tree: pygit2.Tree,
    path_prefix: str,
    seen_blobs: dict,
    stats: dict,
) -> None:
    """Walk tree and collect blob stats."""
    for entry in tree:
        try:
            full_path = f"{path_prefix}/{entry.name}" if path_prefix else entry.name

            if entry.type_str == "blob":
                blob_oid = str(entry.id)

                # Skip if already seen
                if blob_oid in seen_blobs:
                    continue

                # Get extension
                ext = Path(entry.name).suffix.lower()

                # Skip blacklisted extensions
                if ext in BLACKLIST_EXTENSIONS or ext.upper() in BLACKLIST_EXTENSIONS:
                    seen_blobs[blob_oid] = (ext, True, False)
                    stats[ext]["total"] += 1
                    stats[ext]["binary"] += 1
                    continue

                # Get blob
                blob = repo.get(entry.id)
                if blob is None:
                    continue

                is_binary = blob.is_binary
                is_hex = False

                if is_binary:
                    stats[ext]["binary"] += 1
                else:
                    # Check hex content
                    is_hex = is_hex_content(blob.data)
                    if is_hex:
                        stats[ext]["hex"] += 1
                        if len(stats[ext]["hex_examples"]) < 5:
                            stats[ext]["hex_examples"].append(full_path)
                    else:
                        if len(stats[ext]["non_hex_examples"]) < 5:
                            stats[ext]["non_hex_examples"].append(full_path)

                stats[ext]["total"] += 1
                seen_blobs[blob_oid] = (ext, is_binary, is_hex)

            elif entry.type_str == "tree":
                subtree = repo.get(entry.id)
                if subtree:
                    _walk_tree(repo, subtree, full_path, seen_blobs, stats)

        except Exception:
            continue


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: poetry run python tests/analytics/analyze_hex_by_extension.py /path/to/repo"
        )
        return 1

    repo_path = Path(sys.argv[1])
    if not repo_path.exists():
        print(f"ERROR: Repository not found: {repo_path}")
        return 1

    analyze_repo(repo_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
