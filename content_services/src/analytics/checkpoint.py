"""Checkpoint data class and storage for analytics resume capability.

This module provides:
- ExtractionCheckpoint: Pydantic model for checkpoint state
- S3 upload/download/delete functions
- Validation logic for checkpoint compatibility
"""

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    import pygit2

logger = logging.getLogger(__name__)

# Increment this when checkpoint schema changes
CHECKPOINT_VERSION = "2.0"


class ExtractionCheckpoint(BaseModel):
    """In-flight checkpoint for fault-tolerant extraction.

    This model captures the state needed to resume extraction
    after a failure or timeout.

    v2.0 changes:
    - Removed commit_records and file_change_records (caused OOM on large repos)
    - Added commit_chunk_count and file_change_chunk_count for chunk-based storage
    - Records are now written incrementally to S3 Parquet chunks during extraction
    """

    # Schema version for compatibility
    version: str = CHECKPOINT_VERSION

    # Identity
    codebase_id: str

    # Timing
    started_at: datetime
    last_updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Progress tracking (tree size phase)
    commits_total: int
    commits_processed: int
    last_processed_index: int
    last_processed_sha: str

    # Tree size cache from Rust (SHA -> (bytes, lines))
    tree_size_cache: dict[str, tuple[int, int]]

    # Commit processing checkpoint (Phase 2)
    # Set of commit SHAs that have been fully processed
    processed_commit_shas: set[str] = Field(default_factory=set)
    # Number of commit chunks written to S3 (v2.0)
    commit_chunk_count: int = 0
    # Number of file change chunks written to S3 (v2.0)
    file_change_chunk_count: int = 0


def validate_checkpoint_version(checkpoint_data: dict) -> bool:
    """Check if checkpoint version matches current version.

    Args:
        checkpoint_data: Raw checkpoint data dict

    Returns:
        True if version matches, False otherwise
    """
    version = checkpoint_data.get("version", "unknown")
    if version != CHECKPOINT_VERSION:
        logger.warning(
            f"Checkpoint version mismatch: {version} != {CHECKPOINT_VERSION}"
        )
        return False
    return True


def validate_checkpoint(
    repo: "pygit2.Repository", checkpoint: ExtractionCheckpoint
) -> bool:
    """Validate checkpoint is still valid for the repository.

    Checks that the last processed commit still exists (handles force push).

    Args:
        repo: pygit2.Repository instance
        checkpoint: Checkpoint to validate

    Returns:
        True if valid, False if checkpoint should be discarded
    """
    try:
        obj = repo.get(checkpoint.last_processed_sha)
        if obj is None:
            logger.warning(
                f"Checkpoint references missing commit {checkpoint.last_processed_sha}"
            )
            return False
        return True
    except (KeyError, ValueError) as e:
        logger.warning(
            f"Checkpoint references missing commit {checkpoint.last_processed_sha}: {e}"
        )
        return False


# S3 Storage Functions


def _get_checkpoint_key(codebase_id: str) -> str:
    """Get the S3 key for a checkpoint."""
    return f"analytics/{codebase_id}/checkpoint.json"


def upload_checkpoint(
    checkpoint: ExtractionCheckpoint,
    bucket: str,
    s3_client: Any = None,
) -> None:
    """Upload checkpoint to S3.

    Args:
        checkpoint: Checkpoint to upload
        bucket: S3 bucket name
        s3_client: Optional boto3 S3 client (for testing)
    """
    if s3_client is None:
        s3_client = boto3.client("s3")

    key = _get_checkpoint_key(checkpoint.codebase_id)
    body = checkpoint.model_dump_json().encode("utf-8")

    logger.info(f"Uploading checkpoint to s3://{bucket}/{key} ({len(body)} bytes)")
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )


def _validate_tree_size_cache(tree_size_cache: dict[str, tuple[int, int]]) -> bool:
    """Validate tree_size_cache has proper types.

    Args:
        tree_size_cache: Cache to validate

    Returns:
        True if valid, False if corrupted
    """
    if not tree_size_cache:
        return True

    # Sample a few entries to validate types
    sample_keys = list(tree_size_cache.keys())[:10]
    for key in sample_keys:
        value = tree_size_cache[key]
        if not isinstance(value, tuple | list) or len(value) != 2:
            logger.warning(
                f"Invalid tree_size_cache entry for {key}: {value!r} "
                f"(expected tuple of 2 ints)"
            )
            return False
        try:
            int(value[0])
            int(value[1])
        except (TypeError, ValueError) as e:
            logger.warning(
                f"Invalid tree_size_cache value types for {key}: {value!r} "
                f"(elements must be convertible to int): {e}"
            )
            return False
    return True


def download_checkpoint(
    bucket: str,
    codebase_id: str,
    s3_client: Any = None,
) -> ExtractionCheckpoint | None:
    """Download checkpoint from S3.

    Args:
        bucket: S3 bucket name
        codebase_id: Codebase ID to download checkpoint for
        s3_client: Optional boto3 S3 client (for testing)

    Returns:
        ExtractionCheckpoint if found and valid, None otherwise
    """
    if s3_client is None:
        s3_client = boto3.client("s3")

    key = _get_checkpoint_key(codebase_id)

    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read()

        # Parse and validate version first
        data = json.loads(body)
        if not validate_checkpoint_version(data):
            logger.warning("Discarding checkpoint due to version mismatch")
            return None

        checkpoint = ExtractionCheckpoint.model_validate_json(body)

        # Validate tree_size_cache types (defensive check for corrupted data)
        if not _validate_tree_size_cache(checkpoint.tree_size_cache):
            logger.warning(
                "Discarding checkpoint due to corrupted tree_size_cache data"
            )
            return None

        logger.info(
            f"Downloaded checkpoint: {checkpoint.commits_processed}/{checkpoint.commits_total} commits, "
            f"{len(checkpoint.tree_size_cache)} tree sizes, "
            f"{len(checkpoint.processed_commit_shas)} processed commits"
        )
        return checkpoint

    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            logger.debug(f"No checkpoint found at s3://{bucket}/{key}")
            return None
        logger.warning(f"Error downloading checkpoint: {e}")
        return None
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Checkpoint corrupted or invalid: {e}")
        return None


def delete_checkpoint(
    bucket: str,
    codebase_id: str,
    s3_client: Any = None,
) -> None:
    """Delete checkpoint from S3.

    Args:
        bucket: S3 bucket name
        codebase_id: Codebase ID to delete checkpoint for
        s3_client: Optional boto3 S3 client (for testing)
    """
    if s3_client is None:
        s3_client = boto3.client("s3")

    key = _get_checkpoint_key(codebase_id)
    logger.info(f"Deleting checkpoint at s3://{bucket}/{key}")
    s3_client.delete_object(Bucket=bucket, Key=key)
