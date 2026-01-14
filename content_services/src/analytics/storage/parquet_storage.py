"""Parquet storage backend for warm and cold layers."""

import logging
import shutil
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from analytics.schemas.cold_schemas import FILE_CHANGES_SCHEMA
from analytics.schemas.warm_schemas import (
    COMMITS_SCHEMA,
    CONTRIBUTORS_SCHEMA,
)

logger = logging.getLogger(__name__)


class ParquetStorage:
    """Parquet-based storage for warm and cold layers.

    The warm layer stores commit-level and contributor-level data optimized for
    analytical queries. The cold layer stores detailed file change data.

    Supports partitioning, column pruning, and predicate pushdown for efficient queries.

    Usage:
        storage = ParquetStorage(Path('/data/repositories'))
        storage.write_commits(codebase_id=1, commits=[...])
        df = storage.read_commits(codebase_id=1, filters=[('branch_name', '=', 'main')])
    """

    def __init__(self, storage_root: Path) -> None:
        """Initialize Parquet storage.

        Args:
            storage_root: Root path for Parquet files
        """
        self.storage_root = Path(storage_root)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Initialized ParquetStorage at {self.storage_root}")

    @property
    def base_path(self) -> Path:
        """Alias for storage_root for API consistency."""
        return self.storage_root

    def _get_path(self, layer: str, table: str, codebase_id: str) -> Path:
        """Get path for a Parquet file.

        Args:
            layer: 'warm' or 'cold'
            table: Table name (e.g., 'commits', 'contributors')
            codebase_id: Repository ID

        Returns:
            Path to Parquet file
        """
        return self.storage_root / str(codebase_id) / layer / f"{table}.parquet"

    # Write operations - Commits
    def write_commits(
        self,
        codebase_id: str,
        commits: list[dict],
        partition_by: list[str] | None = None,
    ) -> None:
        """Write commits to Parquet.

        Args:
            codebase_id: Repository ID
            commits: List of commit dictionaries
            partition_by: Optional partitioning columns (e.g., ['branch_name', 'commit_year'])
        """
        if not commits:
            logger.debug("No commits to write")
            return

        logger.info(f"Writing {len(commits)} commits for repo {codebase_id}")
        path = self._get_path("warm", "commits", codebase_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Convert to PyArrow table
            table = pa.Table.from_pylist(commits, schema=COMMITS_SCHEMA)

            # Write with optional partitioning
            if partition_by:
                logger.debug(f"Writing with partitioning by: {partition_by}")
                pq.write_to_dataset(
                    table,
                    root_path=str(path.parent / "commits_partitioned"),
                    partition_cols=partition_by,
                    compression="snappy",
                    existing_data_behavior="overwrite_or_ignore",
                    max_partitions=10000,
                )
            else:
                pq.write_table(table, path, compression="snappy", write_statistics=True)

            logger.info(f"Successfully wrote commits for repo {codebase_id}")
        except Exception as e:
            logger.error(f"Failed to write commits: {e}")
            raise

    def append_commits(self, codebase_id: str, new_commits: list[dict]) -> None:
        """Append new commits to existing Parquet file.

        Args:
            codebase_id: Repository ID
            new_commits: List of new commit dictionaries
        """
        if not new_commits:
            logger.debug("No commits to append")
            return

        logger.info(f"Appending {len(new_commits)} commits for repo {codebase_id}")
        path = self._get_path("warm", "commits", codebase_id)

        try:
            if path.exists():
                # Read existing
                existing = pq.read_table(path)
                logger.debug(f"Found existing commits table with {len(existing)} rows")

                # Convert new commits
                new_table = pa.Table.from_pylist(new_commits, schema=COMMITS_SCHEMA)

                # Concatenate
                combined = pa.concat_tables([existing, new_table])
            else:
                logger.debug("No existing commits file, creating new one")
                combined = pa.Table.from_pylist(new_commits, schema=COMMITS_SCHEMA)

            # Write back
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(combined, path, compression="snappy", write_statistics=True)

            logger.info(f"Successfully appended commits for repo {codebase_id}")
        except Exception as e:
            logger.error(f"Failed to append commits: {e}")
            raise

    # Write operations - Contributors
    def write_contributors(self, codebase_id: str, contributors: list[dict]) -> None:
        """Write contributors to Parquet.

        Args:
            codebase_id: Repository ID
            contributors: List of contributor dictionaries
        """
        if not contributors:
            logger.debug("No contributors to write")
            return

        logger.info(f"Writing {len(contributors)} contributors for repo {codebase_id}")
        path = self._get_path("warm", "contributors", codebase_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            table = pa.Table.from_pylist(contributors, schema=CONTRIBUTORS_SCHEMA)
            pq.write_table(table, path, compression="snappy", write_statistics=True)
            logger.info(f"Successfully wrote contributors for repo {codebase_id}")
        except Exception as e:
            logger.error(f"Failed to write contributors: {e}")
            raise

    # Write operations - File Changes (Cold Layer)
    def write_file_changes(
        self, codebase_id: str, file_changes: list[dict], partition_by_date: bool = True
    ) -> None:
        """Write file changes to Parquet (cold layer).

        Args:
            codebase_id: Repository ID
            file_changes: List of file change dictionaries
            partition_by_date: Whether to partition by commit_date (default: True)
        """
        if not file_changes:
            logger.debug("No file changes to write")
            return

        logger.info(f"Writing {len(file_changes)} file changes for repo {codebase_id}")
        path = self._get_path("cold", "file_changes", codebase_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Add commit_year_month for partitioning (reduces partitions from ~5000 days to ~120 months)
            for fc in file_changes:
                if "commit_year_month" not in fc:
                    commit_date = fc.get("commit_date")
                    if commit_date:
                        fc["commit_year_month"] = commit_date.strftime("%Y-%m")
                    else:
                        fc["commit_year_month"] = "unknown"

            table = pa.Table.from_pylist(file_changes, schema=FILE_CHANGES_SCHEMA)

            # Partition by year-month for efficient queries without too many partitions
            if partition_by_date:
                logger.debug("Writing with year-month partitioning")
                pq.write_to_dataset(
                    table,
                    root_path=str(path.parent / "file_changes_partitioned"),
                    partition_cols=["commit_year_month"],
                    compression="snappy",
                    existing_data_behavior="overwrite_or_ignore",
                    max_partitions=1000,
                )
            else:
                pq.write_table(table, path, compression="snappy", write_statistics=True)

            logger.info(f"Successfully wrote file changes for repo {codebase_id}")
        except Exception as e:
            logger.error(f"Failed to write file changes: {e}")
            raise

    # Read operations - Commits
    def read_commits(
        self,
        codebase_id: str,
        columns: list[str] | None = None,
        filters: list[tuple] | None = None,
    ) -> pd.DataFrame:
        """Read commits from Parquet.

        Args:
            codebase_id: Repository ID
            columns: Columns to read (None = all)
            filters: PyArrow filters for predicate pushdown
                    e.g., [('branch_name', '=', 'main'), ('commit_year', '>=', 2024)]

        Returns:
            DataFrame of commits
        """
        # Try partitioned directory first (written with partition_by parameter)
        partitioned_path = (
            self.storage_root / str(codebase_id) / "warm" / "commits_partitioned"
        )
        single_file_path = self._get_path("warm", "commits", codebase_id)

        path_to_read = None
        if partitioned_path.exists():
            path_to_read = partitioned_path
            logger.debug(
                f"Reading from partitioned commits directory: {partitioned_path}"
            )
        elif single_file_path.exists():
            path_to_read = single_file_path
            logger.debug(f"Reading from single commits file: {single_file_path}")
        else:
            logger.debug(f"No commits found for repo {codebase_id}")
            return pd.DataFrame()

        logger.debug(
            f"Reading commits for repo {codebase_id} (columns={columns}, filters={filters})"
        )

        try:
            # Read from either partitioned dataset or single file
            if partitioned_path.exists() and path_to_read == partitioned_path:
                # Read partitioned dataset
                dataset = pq.ParquetDataset(path_to_read, filters=filters)
                table = dataset.read(columns=columns)
            else:
                # Read single file
                table = pq.read_table(path_to_read, columns=columns, filters=filters)

            df = table.to_pandas()

            logger.info(f"Read {len(df)} commits for repo {codebase_id}")
            return df
        except Exception as e:
            logger.error(f"Failed to read commits: {e}")
            raise

    # Read operations - Contributors
    def read_contributors(
        self, codebase_id: str, columns: list[str] | None = None
    ) -> pd.DataFrame:
        """Read contributors from Parquet.

        Args:
            codebase_id: Repository ID
            columns: Columns to read (None = all)

        Returns:
            DataFrame of contributors
        """
        path = self._get_path("warm", "contributors", codebase_id)
        if not path.exists():
            logger.debug(f"No contributors file found for repo {codebase_id}")
            return pd.DataFrame()

        logger.debug(f"Reading contributors for repo {codebase_id}")

        try:
            table = pq.read_table(path, columns=columns)
            df = table.to_pandas()
            logger.info(f"Read {len(df)} contributors for repo {codebase_id}")
            return df
        except Exception as e:
            logger.error(f"Failed to read contributors: {e}")
            raise

    # Read operations - File Changes
    def read_file_changes(
        self,
        codebase_id: str,
        columns: list[str] | None = None,
        filters: list[tuple] | None = None,
    ) -> pd.DataFrame:
        """Read file changes from Parquet.

        Args:
            codebase_id: Repository ID
            columns: Columns to read (None = all)
            filters: PyArrow filters for predicate pushdown

        Returns:
            DataFrame of file changes
        """
        path = self._get_path("cold", "file_changes", codebase_id)
        partitioned_path = path.parent / "file_changes_partitioned"

        # Try partitioned path first (new default), then fall back to single file
        if partitioned_path.exists():
            read_path = partitioned_path
            logger.debug(
                f"Reading partitioned file changes for repo {codebase_id} from {read_path}"
            )
        elif path.exists():
            read_path = path
            logger.debug(
                f"Reading file changes for repo {codebase_id} from {read_path}"
            )
        else:
            logger.debug(f"No file changes found for repo {codebase_id}")
            return pd.DataFrame()

        logger.debug(f"Reading file changes for repo {codebase_id} (filters={filters})")

        try:
            table = pq.read_table(read_path, columns=columns, filters=filters)
            df = table.to_pandas()
            logger.info(f"Read {len(df)} file changes for repo {codebase_id}")
            return df
        except Exception as e:
            logger.error(f"Failed to read file changes: {e}")
            raise

    # Utility methods
    def exists(self, layer: str, table: str, codebase_id: str) -> bool:
        """Check if Parquet file exists.

        Args:
            layer: 'warm' or 'cold'
            table: Table name
            codebase_id: Repository ID

        Returns:
            True if file exists
        """
        path = self._get_path(layer, table, codebase_id)
        exists = path.exists()
        logger.debug(
            f"Checking existence of {layer}/{table} for repo {codebase_id}: {exists}"
        )
        return exists

    def delete_repository(self, codebase_id: str) -> None:
        """Delete all data for a repository.

        Args:
            codebase_id: Repository ID
        """
        repo_path = self.storage_root / str(codebase_id)
        if repo_path.exists():
            logger.info(f"Deleting all data for repo {codebase_id}")
            try:
                shutil.rmtree(repo_path)
                logger.info(f"Successfully deleted data for repo {codebase_id}")
            except Exception as e:
                logger.error(f"Failed to delete repository data: {e}")
                raise
        else:
            logger.debug(f"No data found for repo {codebase_id}")

    def list_repositories(self) -> list[str]:
        """List all repository IDs with stored data.

        Returns:
            List of repository IDs (UUID strings)
        """
        logger.debug("Listing all repositories")

        try:
            codebase_ids = []
            for path in self.storage_root.iterdir():
                # Accept both UUID strings and numeric IDs
                if path.is_dir():
                    name = path.name
                    # UUID pattern: 8-4-4-4-12 hex chars with dashes
                    is_uuid = len(name) == 36 and name.count("-") == 4
                    if is_uuid or name.isdigit():
                        codebase_ids.append(name)

            logger.info(f"Found {len(codebase_ids)} repositories")
            return sorted(codebase_ids)
        except Exception as e:
            logger.error(f"Failed to list repositories: {e}")
            raise
