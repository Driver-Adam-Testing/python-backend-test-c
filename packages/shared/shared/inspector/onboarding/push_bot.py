import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def extract_values_from_presigned_url(url: str) -> dict:
    parsed = urlparse(url)
    path = parsed.path.strip("/")

    pattern = r"^driver_docs/([^/]+)/([^/]+)/([^/]+)/([^/]+\.zip)$"
    match = re.match(pattern, path)

    if not match:
        raise ValueError("URL path does not match the expected structure.")

    primary_asset_id, version_id, filename = match.groups()

    return {
        "primary_asset_id": primary_asset_id,
        "version_id": version_id,
        "filename": filename,
    }


def run(
    cmd: str, cwd: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    import subprocess

    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if result.stdout:
        logger.info(f"stdout: {result.stdout}")
    if result.stderr:
        if result.returncode != 0:
            logger.error(f"stderr: {result.stderr}")
        else:
            logger.warning(f"stderr: {result.stderr}")
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result


def run_git_with_bearer_auth(
    args: list[str],
    access_token: str,
    cwd: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run git command with Bearer token authentication via http.extraHeader.

    This is required for Bitbucket Data Center Project/Repository HTTP Access Tokens
    which cannot be embedded in the clone URL.
    """
    cmd = [
        "git",
        "-c",
        f"http.extraHeader=Authorization: Bearer {access_token}",
        *args,
    ]
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.stdout:
        logger.info(f"stdout: {result.stdout}")
    if result.stderr:
        if result.returncode != 0:
            logger.error(f"stderr: {result.stderr}")
        else:
            logger.warning(f"stderr: {result.stderr}")
    if check and result.returncode != 0:
        sanitized_cmd = " ".join(cmd[:3] + ["..."] + args)
        raise subprocess.CalledProcessError(
            result.returncode, sanitized_cmd, result.stdout, result.stderr
        )
    return result


def push_docs(version_id: uuid.UUID) -> None:
    # import boto3
    import hashlib
    import shutil
    import tempfile

    from database.db import engine
    from database.models import GitProviderAppInstallation
    from database.models_enums import PrimaryAssetProvider
    from shared.inspector.onboarding import (
        azure_devops_ops,
        bitbucket_dc_ops,
        bitbucket_ops,
        gh_ops,
        gitlab_ops,
    )
    from shared.inspector.onboarding.onboard_utils import (
        unpack_archive_to_finalized_path,
    )
    from shared.inspector.utils.db import (
        git_provider_app_installation_by_id,
        sync_get_version_by_id,
    )
    from sqlmodel import Session, select

    version = sync_get_version_by_id(version_id)
    primary_asset_id = version.primary_asset.id
    tracked_branch = version.primary_asset.vcs_tracked_branch
    repo_id = version.primary_asset.repository_id
    repo_name = version.primary_asset.display_name
    org_id = version.primary_asset.organization_id
    org_id_hash = hashlib.sha256(org_id.encode()).hexdigest()[:63]

    # Clean provider detection using the enum directly
    provider = version.primary_asset.provider

    with (
        tempfile.TemporaryDirectory() as temp_dir,
        tempfile.NamedTemporaryFile("w", suffix=".zip") as temp_file,
    ):
        # Override so unpack from github doesn't have hash in name.
        object_key = f"{primary_asset_id}/{version_id}/{version_id}_tech_docs.zip"
        download_meta = download_file_from_s3(org_id_hash, object_key, temp_file.name)
        # print(download_meta)
        install_id = download_meta["install_id"]

        extracted_path = unpack_archive_to_finalized_path(
            archive_path=Path(temp_file.name), extraction_root=Path(temp_dir)
        )
        commit_slug = version.vcs_hash[:7]
        branch = f"docs_{commit_slug}"

        # Get repository info based on provider
        if provider == PrimaryAssetProvider.GITHUB:
            access_token = gh_ops.fetch_app_access_token(install_id)
            clone_url, full_name = gh_ops.get_repo_clone_info_from_id(
                repo_id, access_token
            )
        elif provider == PrimaryAssetProvider.BITBUCKET:
            access_token = bitbucket_ops.fetch_access_token(install_id)
            gp_install = git_provider_app_installation_by_id(installation_id=install_id)
            workspace = gp_install.git_provider_app.provider_metadata["workspace"]
            repo_slug = version.primary_asset.display_name
            # Bitbucket allows spaces in repo names, but does not URL encode them
            # and instead replaces them with hyphens.
            # We need to replace spaces with hyphens in the repo name.
            repo_slug = "-".join(
                repo_name.split()
            )  # multiple spaces go to single hyphen

            clone_url, full_name = bitbucket_ops.get_repo_clone_info_from_id(
                workspace, repo_slug, access_token
            )
        elif provider == PrimaryAssetProvider.GITLAB_SELF_MANAGED:
            with Session(engine) as session:
                installation_id = version.primary_asset.installation_id
                app_install = session.exec(
                    select(GitProviderAppInstallation).where(
                        GitProviderAppInstallation.id == installation_id
                    )
                ).one()
                if app_install is None:
                    raise ValueError(f"Installation ID {installation_id} not found.")
                base_url = app_install.git_provider_app.base_url
            access_token = gitlab_ops.fetch_access_token(install_id)
            clone_url, full_name = gitlab_ops.get_repo_clone_info_from_id(
                base_url, repo_id, access_token
            )
        elif provider == PrimaryAssetProvider.AZURE_DEVOPS_CLOUD:
            with Session(engine) as session:
                installation_id = version.primary_asset.installation_id
                app_install = session.exec(
                    select(GitProviderAppInstallation).where(
                        GitProviderAppInstallation.id == installation_id
                    )
                ).one()
                if app_install is None:
                    raise ValueError(f"Installation ID {installation_id} not found.")
                base_url = app_install.git_provider_app.base_url
                # Extract project from VCS metadata
                vcs_metadata = version.vcs_metadata or {}
                project = (
                    vcs_metadata.get("repository", {})
                    .get("namespace", "")
                    .split("/")[-1]
                )
                if not project:
                    raise ValueError(
                        f"Could not determine project from VCS metadata for version {version_id}"
                    )
            access_token = azure_devops_ops.fetch_access_token(install_id)
            clone_url, full_name = azure_devops_ops.get_repo_clone_info_from_id(
                base_url, project, repo_id, access_token
            )
        elif provider == PrimaryAssetProvider.BITBUCKET_DATA_CENTER:
            # Bitbucket DC uses Bearer auth via git http.extraHeader
            access_token, instance_url = bitbucket_dc_ops.fetch_access_token(install_id)
            vcs_metadata = version.vcs_metadata or {}
            project_key = vcs_metadata.get("project_key", "")
            if not project_key:
                project_key = vcs_metadata.get("repository", {}).get("namespace", "")
            if not project_key:
                raise ValueError(
                    f"Could not determine project_key from VCS metadata for version {version_id}"
                )
            repo_slug = repo_name.lower().replace(" ", "-")
            clone_url, full_name = bitbucket_dc_ops.get_repo_clone_info_from_id(
                instance_url, project_key, repo_slug
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        repo_dir = Path(temp_dir) / full_name
        target_dir = "driver_docs"
        if not os.path.exists(repo_dir):
            # Bitbucket DC requires Bearer auth via git http.extraHeader
            if provider == PrimaryAssetProvider.BITBUCKET_DATA_CENTER:
                run_git_with_bearer_auth(
                    ["clone", clone_url, str(repo_dir)],
                    access_token,
                )
            else:
                run(f"git clone {clone_url} {repo_dir}")
            if tracked_branch is not None:
                run(f"git checkout {tracked_branch}", cwd=repo_dir)

        run(f"git checkout -B {branch}", cwd=repo_dir)
        src_path = os.path.abspath(extracted_path)

        driver_docs_path = repo_dir / "driver_docs"
        if os.path.exists(driver_docs_path):
            # NOTE: only need to do this because previous iteration of export landed
            # directly in `driver_docs`. Once we start exporting other content,
            # we'll need a different approach.
            logger.info(f"Removing existing driver_docs directory: {driver_docs_path}")
            shutil.rmtree(driver_docs_path)

        dst_path = repo_dir / "driver_docs" / repo_name
        COMMIT_MESSAGE = "docs: update driver docs for commit: " + commit_slug
        sync_directory(src_path, dst_path)

        run('git config user.name "docs-bot"', cwd=repo_dir)
        run('git config user.email "bot@driverai.com"', cwd=repo_dir)
        run(f"git add {target_dir}", cwd=repo_dir)

        diff = run("git diff --cached --quiet", cwd=repo_dir, check=False)
        if diff.returncode == 0:
            logger.info("No changes to commit")
            return

        run(f'git commit -m "{COMMIT_MESSAGE}"', cwd=repo_dir)
        # Bitbucket DC requires Bearer auth via git http.extraHeader
        if provider == PrimaryAssetProvider.BITBUCKET_DATA_CENTER:
            run_git_with_bearer_auth(
                ["push", "--force", clone_url, branch],
                access_token,
                cwd=str(repo_dir),
            )
        else:
            run(f"git push --force {clone_url} {branch}", cwd=repo_dir)
        logger.info(f"Pushed {target_dir} to {branch}")

        # Create pull request based on provider
        if provider == PrimaryAssetProvider.GITHUB:
            gh_ops.create_pull_request_with_bot_cleanup(
                full_name, branch, access_token, commit_slug
            )
        elif provider == PrimaryAssetProvider.BITBUCKET:
            bitbucket_ops.create_pull_request_with_bot_cleanup(
                workspace,
                repo_slug,
                access_token,
                branch,
                commit_slug,
                tracked_branch,
            )
        elif provider == PrimaryAssetProvider.GITLAB_SELF_MANAGED:
            gitlab_ops.create_pull_request_with_bot_cleanup(
                base_url, repo_id, access_token, branch, commit_slug
            )
        elif provider == PrimaryAssetProvider.AZURE_DEVOPS_CLOUD:
            azure_devops_ops.create_pull_request_with_bot_cleanup(
                base_url, project, repo_id, access_token, branch, commit_slug
            )
        elif provider == PrimaryAssetProvider.BITBUCKET_DATA_CENTER:
            bitbucket_dc_ops.create_pull_request_with_bot_cleanup(
                instance_url,
                project_key,
                repo_slug,
                access_token,
                branch,
                commit_slug,
                install_id,
                tracked_branch,
            )


def sync_directory(src: str, dest: str) -> None:
    import shutil

    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    logger.info(f"Synced {src} to {dest}")


def download_file_from_s3(
    bucket_name: str, object_key: str, local_file_path: str
) -> dict:
    import boto3

    # Create an S3 client
    s3 = boto3.client("s3")
    response = s3.head_object(Bucket=bucket_name, Key=object_key)

    metadata = response.get("Metadata", {})
    logger.info(f"S3 object metadata: {metadata}")
    s3.download_file(bucket_name, object_key, local_file_path)
    logger.info(
        f"Downloaded {object_key} from bucket {bucket_name} to {local_file_path}"
    )
    return metadata


def build_s3_path(org_id_hash: str, primary_asset_id: str, version_id: str) -> str:
    return f"driver_docs/{org_id_hash}/{primary_asset_id}/{version_id}/driver_docs.zip"


if __name__ == "__main__":
    import asyncio
    import uuid

    # Example usage
    version_id = "3d5cf3ea-642e-47ae-a0d5-3385bf6a62f1"
    asyncio.run(push_docs(version_id))
