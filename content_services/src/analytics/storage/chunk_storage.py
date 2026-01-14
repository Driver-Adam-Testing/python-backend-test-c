"""Chunk storage for incremental Parquet writes during extraction.

This module provides ChunkStorage for writing commit and file change records
to S3 as Parquet chunks during extraction, enabling memory-efficient processing
of large repositories.

Key features:
- Consistent S3 key patterns for chunks
- zstd compression for efficient storage and transfer
- Pagination-aware listing
- Batched deletion for large chunk counts
"""

import logging
from io import BytesIO
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from analytics.schemas.cold_schemas import FILE_CHANGES_SCHEMA
from analytics.schemas.warm_schemas import COMMITS_SCHEMA

logger = logging.getLogger(__name__)

# S3 DeleteObjects has a 1000 object limit per request
MAX_DELETE_BATCH_SIZE = 1000


class ChunkStorage:
    """Write and manage Parquet chunks on S3.

    Chunks are stored in S3 with the pattern:
        analytics/{codebase_id}/chunks/commits/chunk_00000.parquet
        analytics/{codebase_id}/chunks/file_changes/chunk_00000.parquet

    This allows for:
    - Easy listing/cleanup via prefix operations
    - Predictable ordering via zero-padded indices
    - Separation between commit and file change data
    """

    def __init__(self, s3_client: Any, bucket: str, codebase_id: str) -> None:
        """Initialize chunk storage.

        Args:
            s3_client: boto3 S3 client
            bucket: S3 bucket name
            codebase_id: Codebase identifier for key prefix
        """
        self.s3_client = s3_client
        self.bucket = bucket
        self.codebase_id = codebase_id
        self.base_prefix = f"analytics/{codebase_id}/chunks"

    def get_commit_chunk_key(self, chunk_index: int) -> str:
        """Get S3 key for a commit chunk.

        Args:
            chunk_index: Zero-based chunk index

        Returns:
            S3 key string
        """
        return f"{self.base_prefix}/commits/chunk_{chunk_index:05d}.parquet"

    def get_file_change_chunk_key(self, chunk_index: int) -> str:
        """Get S3 key for a file change chunk.

        Args:
            chunk_index: Zero-based chunk index

        Returns:
            S3 key string
        """
        return f"{self.base_prefix}/file_changes/chunk_{chunk_index:05d}.parquet"

    def get_chunks_prefix(self) -> str:
        """Get S3 prefix for all chunks.

        Returns:
            S3 prefix string (includes trailing slash)
        """
        return f"{self.base_prefix}/"

    def write_commit_chunk(self, records: list[dict], chunk_index: int) -> str:
        """Write commit records to S3 as a Parquet chunk.

        Args:
            records: List of commit record dicts
            chunk_index: Zero-based chunk index

        Returns:
            S3 key of written chunk
        """
        key = self.get_commit_chunk_key(chunk_index)
        self._write_parquet(records, key, COMMITS_SCHEMA)
        logger.debug(
            f"Wrote commit chunk {chunk_index} with {len(records)} records to {key}"
        )
        return key

    def write_file_change_chunk(self, records: list[dict], chunk_index: int) -> str:
        """Write file change records to S3 as a Parquet chunk.

        Args:
            records: List of file change record dicts
            chunk_index: Zero-based chunk index

        Returns:
            S3 key of written chunk
        """
        key = self.get_file_change_chunk_key(chunk_index)
        self._write_parquet(records, key, FILE_CHANGES_SCHEMA)
        logger.debug(
            f"Wrote file change chunk {chunk_index} with {len(records)} records to {key}"
        )
        return key

    def _write_parquet(self, records: list[dict], key: str, schema: pa.Schema) -> None:
        """Write records to S3 as Parquet with zstd compression.

        Args:
            records: List of record dicts
            key: S3 key to write to
            schema: PyArrow schema for the table
        """
        table = pa.Table.from_pylist(records, schema=schema)
        buffer = BytesIO()
        pq.write_table(table, buffer, compression="zstd")
        buffer.seek(0)
        self.s3_client.upload_fileobj(buffer, self.bucket, key)

    def list_commit_chunks(self) -> list[str]:
        """List all commit chunk keys, sorted by index.

        Returns:
            Sorted list of S3 keys
        """
        return self._list_chunks("commits")

    def list_file_change_chunks(self) -> list[str]:
        """List all file change chunk keys, sorted by index.

        Returns:
            Sorted list of S3 keys
        """
        return self._list_chunks("file_changes")

    def _list_s3_objects(self, prefix: str) -> list[str]:
        """List all S3 objects under a prefix with pagination.

        Args:
            prefix: S3 prefix to list objects under

        Returns:
            List of S3 keys (unsorted)
        """
        keys = []
        continuation_token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token

            response = self.s3_client.list_objects_v2(**kwargs)

            if "Contents" in response:
                keys.extend(obj["Key"] for obj in response["Contents"])

            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")

        return keys

    def _list_chunks(self, chunk_type: str) -> list[str]:
        """List chunks of a specific type with pagination handling.

        Args:
            chunk_type: "commits" or "file_changes"

        Returns:
            Sorted list of S3 keys
        """
        prefix = f"{self.base_prefix}/{chunk_type}/"
        return sorted(self._list_s3_objects(prefix))

    def delete_all_chunks(self) -> None:
        """Delete all chunks for this codebase.

        Handles pagination for listing and batching for deletion
        (S3 DeleteObjects has a 1000 object limit).
        """
        keys = self._list_s3_objects(self.get_chunks_prefix())

        if not keys:
            logger.debug(f"No chunks to delete for {self.codebase_id}")
            return

        # Delete in batches of MAX_DELETE_BATCH_SIZE
        for i in range(0, len(keys), MAX_DELETE_BATCH_SIZE):
            batch = keys[i : i + MAX_DELETE_BATCH_SIZE]
            self.s3_client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in batch]}
            )
            logger.debug(f"Deleted {len(batch)} chunks for {self.codebase_id}")

        logger.info(f"Deleted {len(keys)} total chunks for {self.codebase_id}")
