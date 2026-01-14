"""Code ownership calculation for analytics.

A2: Ownership Calculation

Calculates code ownership by counting net lines (additions - deletions)
per author per file.
"""

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def calculate_ownership(file_changes: list[dict]) -> dict[str, list[dict]]:
    """Calculate code ownership from file changes.

    For each file, determines what percentage each author "owns"
    based on their net line contributions.

    Args:
        file_changes: List of file change dicts with:
            - file_path: str
            - author_email: str
            - additions_lines: int
            - deletions_lines: int

    Returns:
        Dict mapping file_path to list of owner dicts:
            {
                'src/main.py': [
                    {'author_email': 'alice@example.com', 'net_lines': 100, 'percentage': 66.67},
                    {'author_email': 'bob@example.com', 'net_lines': 50, 'percentage': 33.33},
                ]
            }
    """
    if not file_changes:
        return {}

    # Aggregate net lines by file and author
    # file_author_lines[file_path][author_email] = net_lines
    file_author_lines: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for fc in file_changes:
        file_path = fc.get("file_path", "")
        author_email = fc.get("author_email", "")
        additions = fc.get("additions_lines", 0)
        deletions = fc.get("deletions_lines", 0)

        if not file_path or not author_email:
            continue

        net_lines = additions - deletions
        file_author_lines[file_path][author_email] += net_lines

    # Calculate ownership percentages
    result: dict[str, list[dict]] = {}

    for file_path, author_lines in file_author_lines.items():
        # Calculate total absolute contribution for percentage basis
        # Use absolute values so negative contributions don't cancel out
        total_abs_lines = sum(abs(lines) for lines in author_lines.values())

        if total_abs_lines == 0:
            # All authors have 0 net contribution
            owners = [
                {
                    "author_email": email,
                    "net_lines": 0,
                    "percentage": 100.0 / len(author_lines) if author_lines else 0.0,
                }
                for email in author_lines
            ]
        else:
            owners = []
            for email, net_lines in author_lines.items():
                percentage = (abs(net_lines) / total_abs_lines) * 100.0
                owners.append(
                    {
                        "author_email": email,
                        "net_lines": net_lines,
                        "percentage": round(percentage, 2),
                    }
                )

        # Sort by percentage descending
        owners.sort(key=lambda x: x["percentage"], reverse=True)
        result[file_path] = owners

    logger.debug(f"Calculated ownership for {len(result)} files")
    return result
