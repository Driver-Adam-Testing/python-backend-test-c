"""DuckDB-based streaming merge for Parquet chunks.

This module provides ChunkMerger for efficiently combining Parquet chunks
into final storage files using DuckDB's streaming capabilities.

Key features:
- Memory-efficient streaming merge (doesn't load all data into memory)
- Optional ordering for commits (by committed_at)
- Partitioned output for file changes (by commit_year_month)
- zstd compression for output files
"""

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)


class ChunkMerger:
    """Merge Parquet chunks using DuckDB streaming.

    DuckDB handles memory management internally, spilling to disk as needed.
    This allows merging large datasets without OOM.
    """

    def __init__(self) -> None:
        """Initialize merger with a new DuckDB connection."""
        self.conn = duckdb.connect()

    def merge_commits(
        self,
        chunk_pattern: str,
        output_path: str,
        order_by: str | None = None,
    ) -> None:
        """Merge commit chunks to a single Parquet file.

        Args:
            chunk_pattern: Glob pattern matching chunk files (e.g., "/path/*.parquet")
            output_path: Path for merged output file
            order_by: Optional column name to order results by
        """
        order_clause = f"ORDER BY {order_by}" if order_by else ""

        query = f"""
            COPY (
                SELECT * FROM read_parquet('{chunk_pattern}')
                {order_clause}
            )
            TO '{output_path}'
            (FORMAT PARQUET, COMPRESSION ZSTD)
        """

        logger.debug(f"Merging commits: {chunk_pattern} -> {output_path}")
        self.conn.execute(query)
        logger.info(f"Merged commit chunks to {output_path}")

    def merge_file_changes(
        self,
        chunk_pattern: str,
        output_dir: str,
        partition_by: str,
    ) -> None:
        """Merge file change chunks with partitioning.

        Creates a partitioned directory structure based on the partition column.
        For example, partitioning by commit_year_month creates:
            output_dir/commit_year_month=2024-01/*.parquet
            output_dir/commit_year_month=2024-02/*.parquet

        Args:
            chunk_pattern: Glob pattern matching chunk files
            output_dir: Directory for partitioned output
            partition_by: Column name to partition by
        """
        # Ensure output directory exists
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        query = f"""
            COPY (
                SELECT * FROM read_parquet('{chunk_pattern}')
            )
            TO '{output_dir}'
            (FORMAT PARQUET, PARTITION_BY ({partition_by}), COMPRESSION ZSTD)
        """

        logger.debug(
            f"Merging file changes: {chunk_pattern} -> {output_dir} (partitioned by {partition_by})"
        )
        self.conn.execute(query)
        logger.info(f"Merged file change chunks to {output_dir}")

    def merge_generic(
        self,
        chunk_pattern: str,
        output_path: str,
    ) -> None:
        """Merge any Parquet chunks without ordering or partitioning.

        Useful for simple concatenation of chunks.

        Args:
            chunk_pattern: Glob pattern matching chunk files
            output_path: Path for merged output file
        """
        query = f"""
            COPY (
                SELECT * FROM read_parquet('{chunk_pattern}')
            )
            TO '{output_path}'
            (FORMAT PARQUET, COMPRESSION ZSTD)
        """

        logger.debug(f"Merging chunks: {chunk_pattern} -> {output_path}")
        self.conn.execute(query)
        logger.info(f"Merged chunks to {output_path}")

    def close(self) -> None:
        """Close the DuckDB connection.

        Should be called when done with the merger to release resources.
        """
        self.conn.close()
        logger.debug("Closed DuckDB connection")
