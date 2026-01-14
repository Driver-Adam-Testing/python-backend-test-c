"""
PyArrow schema definitions for cold details layer (Parquet).

The cold layer stores detailed file-level change data. This is the most
granular tier, optimized for deep analysis and debugging.

Version: 2.0
"""

import pyarrow as pa

COLD_SCHEMA_VERSION = "2.0"

# File changes table schema
FILE_CHANGES_SCHEMA = pa.schema(
    [
        # Identity
        pa.field("codebase_id", pa.string(), nullable=False),
        pa.field("commit_sha", pa.string(), nullable=False),
        pa.field("file_path", pa.string(), nullable=False),
        # Temporal (for partitioning)
        pa.field("commit_date", pa.date32(), nullable=False),
        pa.field(
            "commit_year_month", pa.string(), nullable=False
        ),  # "YYYY-MM" for partitioning
        # Change type
        pa.field(
            "change_type", pa.string(), nullable=False
        ),  # added/modified/deleted/renamed
        pa.field("previous_path", pa.string(), nullable=True),
        # Traditional line-based metrics
        pa.field("additions_lines", pa.int32(), nullable=False),
        pa.field("deletions_lines", pa.int32(), nullable=False),
        pa.field("changes_lines", pa.int32(), nullable=False),
        # Byte-based SLOC metrics
        pa.field("addition_bytes", pa.int64(), nullable=False),
        pa.field("deletion_bytes", pa.int64(), nullable=False),
        pa.field("file_sloc", pa.int64(), nullable=False),
        # File metadata
        pa.field("file_extension", pa.string(), nullable=True),
        pa.field("file_language", pa.string(), nullable=True),
        # Optional: patch data reference
        pa.field("has_patch_data", pa.bool_(), nullable=False),
        pa.field("patch_blob_key", pa.string(), nullable=True),
    ]
)
