import hashlib
import logging
import os
import shutil
import ssl
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

import httpx
from database.models import Organization, PrimaryAssetRoleGrant
from database.models_enums import (
    PrimaryAssetKind,
    PrimaryAssetProvider,
    PrimaryAssetRole,
    PrincipalKind,
    SourceVisibility,
    VcsAutoUpdatePolicy,
    VersionStatus,
)
from shared.inspector.onboarding.onboard_utils import (
    AccessTokenError,
    upload_to_s3_with_metadata,
)
from shared.inspector.onboarding.vcs_utils import (
    AuthorInfo,
    BranchInfo,
    CommitInfo,
    RepoInfo,
    VersionControlInfo,
)
from shared.interfaces.aws_client_config import AWSClientConfig
from shared.secret_management.aws_secret_management import (
    AWSSecretManagementStrategy,
    format_secret_name,
)
from shared.utils.decorators import retry_with_exponential_backoff
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

logger = logging.getLogger(__name__)

# Secret prefix for Bitbucket DC installations
BBDC_SECRET_PREFIX = "GIT_PROVIDER_BBDC_HTTP_INSTALL_SECRET"

# Environment variable to override instance URL (for Docker/local dev)
BBDC_INSTANCE_URL_ENV = "BITBUCKET_DC_INSTANCE_URL"

# Retry configuration for transient network failures
NETWORK_RETRY = retry_with_exponential_backoff(
    initial_delay=1,
    exponential_base=2,
    jitter=True,
    max_retries=3,
    errors=(httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException),
)


class BitbucketDCError(Exception):
    pass


def _create_git_provider_grants(
    session: Session,
    primary_asset_id: UUID,
    organization_id: str,
) -> None:
    org = session.get(Organization, organization_id)
    if not org:
        raise ValueError(f"Organization {organization_id} not found")

    visibility = org.default_source_visibility

    if visibility == SourceVisibility.internal:
        grant = PrimaryAssetRoleGrant(
            primary_asset_id=primary_asset_id,
            organization_id=organization_id,
            principal_kind=PrincipalKind.org,
            role=PrimaryAssetRole.asset_member,
        )
        session.add(grant)
        logger.info(f"Created internal visibility grant for asset {primary_asset_id}")
    elif visibility == SourceVisibility.public:
        grant = PrimaryAssetRoleGrant(
            primary_asset_id=primary_asset_id,
            organization_id=organization_id,
            principal_kind=PrincipalKind.public,
            role=PrimaryAssetRole.asset_member,
        )
        session.add(grant)
        logger.info(f"Created public visibility grant for asset {primary_asset_id}")


def fetch_access_token(installation_id: str) -> tuple[str, str]:
    """Returns (token, instance_url). Instance URL can be overridden via BITBUCKET_DC_INSTANCE_URL env var."""
    logger.info(
        f"Fetching HTTP Access Token for Bitbucket DC installation ID {installation_id}"
    )
    install_key = format_secret_name(BBDC_SECRET_PREFIX, installation_id)
    secrets_manager = AWSSecretManagementStrategy(
        AWSClientConfig(region_name=os.environ["AWS_REGION"])
    )
    secret_value = secrets_manager.read_secret(install_key)
    if not secret_value:
        raise AccessTokenError(
            "HTTP Access Token not found for Bitbucket DC installation"
        )

    # Allow environment variable to override instance URL for Docker/local dev
    instance_url = os.environ.get(BBDC_INSTANCE_URL_ENV) or secret_value["instance_url"]

    return (
        secret_value["token"],
        instance_url,
    )


def fetch_secrets(installation_id: str) -> dict[str, Any]:
    """Instance URL in returned dict can be overridden via BITBUCKET_DC_INSTANCE_URL env var."""
    install_key = format_secret_name(BBDC_SECRET_PREFIX, installation_id)
    secrets_manager = AWSSecretManagementStrategy(
        AWSClientConfig(region_name=os.environ["AWS_REGION"])
    )
    secret_value = secrets_manager.read_secret(install_key)
    if not secret_value:
        raise AccessTokenError("Secrets not found for Bitbucket DC installation")

    # Allow environment variable to override instance URL for Docker/local dev
    env_url = os.environ.get(BBDC_INSTANCE_URL_ENV)
    if env_url:
        secret_value = dict(secret_value)  # Copy to avoid mutating cached value
        secret_value["instance_url"] = env_url

    return secret_value


def _get_ssl_context(secrets: dict[str, Any]) -> bool | ssl.SSLContext:
    """Get SSL context based on secrets configuration."""
    if secrets.get("disable_ssl_verify", False):
        return False
    if secrets.get("ca_bundle_path"):
        context = ssl.create_default_context()
        context.load_verify_locations(secrets["ca_bundle_path"])
        return context
    return True


def build_clone_url(
    instance_url: str,
    project_key: str,
    repo_slug: str,
) -> str:
    """Credentials must be passed via git header, not embedded in URL."""
    parsed = urlparse(instance_url)
    host = parsed.netloc
    scheme = parsed.scheme
    return f"{scheme}://{host}/scm/{project_key}/{repo_slug}.git"


@NETWORK_RETRY
def get_default_branch(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    access_token: str,
    verify: bool | ssl.SSLContext = True,
) -> str | None:
    """Returns None if repository is empty (no commits)."""
    api_base = f"{instance_url.rstrip('/')}/rest/api/1.0"
    headers = {"Authorization": f"Bearer {access_token}"}

    with httpx.Client(verify=verify, timeout=30.0) as client:
        # First, try the default-branch endpoint
        default_branch = None
        try:
            url = f"{api_base}/projects/{project_key}/repos/{repo_slug}/default-branch"
            response = client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            default_branch = data.get("displayId")
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
            logger.warning(
                f"Default branch endpoint returned 404 for {project_key}/{repo_slug}"
            )

        # Validate the default branch exists by listing branches
        branches_url = f"{api_base}/projects/{project_key}/repos/{repo_slug}/branches"
        try:
            response = client.get(branches_url, headers=headers, params={"limit": 100})
            response.raise_for_status()
            branches_data = response.json()
            branches = branches_data.get("values", [])

            if not branches:
                # Empty repo - no branches means no commits
                logger.warning(
                    f"No branches found for {project_key}/{repo_slug} - repo appears empty"
                )
                return None

            # Check if reported default branch actually exists
            if default_branch:
                for b in branches:
                    if b.get("displayId") == default_branch:
                        return default_branch
                logger.warning(
                    f"Default branch '{default_branch}' not found in branches list"
                )

            # Look for branch with isDefault flag
            for b in branches:
                if b.get("isDefault"):
                    actual_default = b.get("displayId")
                    logger.info(f"Using isDefault branch: {actual_default}")
                    return actual_default

            # Fall back to first branch if it has commits
            first_branch = branches[0].get("displayId")
            if branches[0].get("latestCommit"):
                logger.info(f"No default found, using first branch: {first_branch}")
                return first_branch

            # Branch exists but has no commits
            logger.warning(
                f"Branch {first_branch} exists but has no commits for {project_key}/{repo_slug}"
            )
            return None

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to list branches: {e}")
            return None


def _find_branch_commit(branches: list[dict[str, Any]], branch: str) -> str | None:
    """Find commit SHA for exact branch match."""
    for b in branches:
        if b["displayId"] == branch:
            return b["latestCommit"]
    return None


def _find_default_branch_commit(branches: list[dict[str, Any]]) -> str | None:
    """Find commit SHA for default branch."""
    for b in branches:
        if b.get("isDefault"):
            logger.info(f"Using default branch {b['displayId']}")
            return b["latestCommit"]
    return None


@NETWORK_RETRY
def get_latest_commit_on_branch(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    branch: str,
    access_token: str,
    verify: bool | ssl.SSLContext = True,
) -> str | None:
    """Returns None if repository is empty (no commits)."""
    api_base = f"{instance_url.rstrip('/')}/rest/api/1.0"
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{api_base}/projects/{project_key}/repos/{repo_slug}/branches"

    try:
        with httpx.Client(verify=verify, timeout=30.0) as client:
            response = client.get(
                url, headers=headers, params={"filterText": branch, "limit": 10}
            )
            response.raise_for_status()
            branches = response.json().get("values", [])

            commit = _find_branch_commit(branches, branch)
            if commit:
                return commit

            if not branches:
                response = client.get(url, headers=headers, params={"limit": 100})
                response.raise_for_status()
                branches = response.json().get("values", [])

            commit = _find_branch_commit(branches, branch)
            if commit:
                return commit

            commit = _find_default_branch_commit(branches)
            if commit:
                return commit

            if branches:
                logger.info(f"Using first available branch {branches[0]['displayId']}")
                return branches[0]["latestCommit"]

            logger.info(f"No branches found for {project_key}/{repo_slug}")
            return None

    except httpx.HTTPStatusError as e:
        logger.error(f"Failed to get latest commit on {branch}: {e}")
        raise


def _configure_git_ssl_env(
    ca_bundle_path: str | None, disable_ssl_verify: bool
) -> dict[str, str]:
    """Configure environment variables for git SSL handling."""
    env = os.environ.copy()
    if disable_ssl_verify:
        logger.warning("SSL verification disabled for git clone.")
        env["GIT_SSL_NO_VERIFY"] = "true"
    elif ca_bundle_path:
        env["GIT_SSL_CAINFO"] = ca_bundle_path
    return env


def _clone_repository(
    clone_url: str, repo_path: Path, access_token: str, env: dict[str, str]
) -> None:
    """Clone repository with Bearer authentication."""
    clone_cmd = [
        "git",
        "-c",
        f"http.extraHeader=Authorization: Bearer {access_token}",
        "clone",
        "--no-checkout",
        clone_url,
        str(repo_path),
    ]
    result = subprocess.run(
        clone_cmd, capture_output=True, text=True, timeout=300, env=env
    )
    if result.returncode != 0:
        raise BitbucketDCError(f"Failed to clone repository: {result.stderr}")


def _checkout_commit(repo_path: Path, commit: str | None, env: dict[str, str]) -> None:
    """Checkout specific commit or default branch."""
    if not commit:
        result = subprocess.run(
            ["git", "checkout"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise BitbucketDCError(
                f"Failed to checkout default branch: {result.stderr}"
            )
        return

    result = subprocess.run(
        ["git", "checkout", commit],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        env=env,
    )

    if result.returncode != 0:
        subprocess.run(
            ["git", "fetch", "--unshallow"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            env=env,
        )
        result = subprocess.run(
            ["git", "checkout", commit],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise BitbucketDCError(f"Failed to checkout commit {commit}")


def _create_zip_archive(repo_path: Path, repo_slug: str, temp_dir: str) -> bytes:
    """Create ZIP archive of repository contents."""
    git_dir = repo_path / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)

    zip_path = Path(temp_dir) / f"{repo_slug}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in repo_path.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(repo_path)
                zipf.write(file_path, arcname)

    with open(zip_path, "rb") as f:
        return f.read()


def download_repo(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    commit: str,
    access_token: str,
    ca_bundle_path: str | None = None,
    disable_ssl_verify: bool = False,
) -> bytes:
    """Clone repo and return ZIP content using Bearer auth."""
    logger.info(
        f"Cloning Bitbucket DC repository {project_key}/{repo_slug} at commit {commit}"
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir) / repo_slug
        clone_url = build_clone_url(instance_url, project_key, repo_slug)
        env = _configure_git_ssl_env(ca_bundle_path, disable_ssl_verify)

        try:
            _clone_repository(clone_url, repo_path, access_token, env)
            _checkout_commit(repo_path, commit, env)
            return _create_zip_archive(repo_path, repo_slug, temp_dir)
        except subprocess.TimeoutExpired:
            raise BitbucketDCError("Git clone operation timed out")
        except OSError as e:
            raise BitbucketDCError(f"File system error: {e!s}")


@NETWORK_RETRY
def get_commit_info(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    commit_sha: str,
    access_token: str,
    verify: bool | ssl.SSLContext = True,
) -> dict[str, Any]:
    api_base = f"{instance_url.rstrip('/')}/rest/api/1.0"
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{api_base}/projects/{project_key}/repos/{repo_slug}/commits/{commit_sha}"

    with httpx.Client(verify=verify, timeout=30.0) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


@NETWORK_RETRY
def get_repository_info(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    access_token: str,
    verify: bool | ssl.SSLContext = True,
) -> dict[str, Any]:
    api_base = f"{instance_url.rstrip('/')}/rest/api/1.0"
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{api_base}/projects/{project_key}/repos/{repo_slug}"

    with httpx.Client(verify=verify, timeout=30.0) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def fetch_vcs_info(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    access_token: str,
    commit_sha: str,
    tracked_branch: str | None = None,
    verify: bool | ssl.SSLContext = True,
) -> VersionControlInfo:
    """Fetch version control information for a repository."""
    # Get repository info
    repo_data = get_repository_info(
        instance_url, project_key, repo_slug, access_token, verify
    )

    # Get default branch if not tracking specific branch
    if tracked_branch is None:
        tracked_branch = get_default_branch(
            instance_url, project_key, repo_slug, access_token, verify
        )

    # Get commit info
    commit_data = get_commit_info(
        instance_url, project_key, repo_slug, commit_sha, access_token, verify
    )

    # Build VersionControlInfo
    author_info = AuthorInfo(
        email=commit_data.get("author", {}).get("emailAddress", ""),
        name=commit_data.get("author", {}).get("name", ""),
        date=str(commit_data.get("authorTimestamp", "")),
    )

    # Build repo URL
    repo_url = f"{instance_url}/projects/{project_key}/repos/{repo_slug}"

    commit_info = CommitInfo(
        sha=commit_data.get("id", commit_sha),
        message=commit_data.get("message", ""),
        url=f"{repo_url}/commits/{commit_sha}",
        author=author_info,
    )

    branch_info = BranchInfo(name=tracked_branch)

    repo_info = RepoInfo(
        name=repo_data.get("name", repo_slug),
        namespace=project_key,
        full_name=f"{project_key}/{repo_slug}",
        url=repo_url,
    )

    return VersionControlInfo(
        repository=repo_info,
        commit=commit_info,
        branch=branch_info,
    )


def generate_codebase_metadata(
    org_id: str,
    project_key: str,
    repo_name: str,
    repo_id: str | int,
    version_id: str | UUID,
    asset_name: str,
    install_id: str,
    instance_url: str,
) -> dict[str, Any]:
    return {
        "unhashed_organization_id": org_id,
        "full_repo_name": f"{project_key}/{repo_name}",
        "project_key": project_key,
        "provider": PrimaryAssetProvider.BITBUCKET_DATA_CENTER.value,
        "version_id": str(version_id),
        "repository_id": str(repo_id),
        "asset_name": asset_name,
        "asset_kind": PrimaryAssetKind.CODEBASE,
        "install_id": install_id,
        "instance_url": instance_url,
    }


def _extract_repo_info(repo: dict[str, Any]) -> dict[str, Any]:
    metadata = repo.get("metadata", {})
    return {
        "repo_id": repo.get("repo_id") or metadata.get("id"),
        "repo_name": repo.get("repo_name") or repo.get("name"),
        "project_key": repo.get("project_key") or metadata.get("project_key"),
        "repo_slug": repo.get("repo_slug")
        or metadata.get("slug")
        or repo.get("repo_name")
        or repo.get("name"),
        "installation_id": repo.get("installation_id"),
        "tracked_branch": repo.get("tracked_branch"),
        "metadata": metadata,
    }


def _get_repo_commit(
    repo: dict[str, Any],
    instance_url: str,
    project_key: str,
    repo_slug: str,
    access_token: str,
    tracked_branch: str | None,
    verify: bool | ssl.SSLContext,
) -> str | None:
    if repo.get("commit"):
        return repo["commit"]
    if repo.get("latest_commit"):
        latest = repo["latest_commit"]
        return latest.get("id") if isinstance(latest, dict) else latest

    branch = tracked_branch or get_default_branch(
        instance_url, project_key, repo_slug, access_token, verify
    )
    if not branch:
        # No branch means empty repo with no commits
        logger.info(
            f"No branch available for {project_key}/{repo_slug} - repo is empty"
        )
        return None

    logger.info(f"No commit specified, fetching latest commit on branch {branch}...")
    commit = get_latest_commit_on_branch(
        instance_url, project_key, repo_slug, branch, access_token, verify
    )
    if commit:
        logger.info(f"Using commit {commit}")
    return commit


def _handle_push_version(
    session: Session,
    primary_asset: Any,
    commit: str,
    vcs_info: VersionControlInfo | None,
    repo_name: str,
) -> UUID | None:
    """Handle version creation for push events. Returns version_id or None if skipped."""
    from database.models import Version

    versions = primary_asset.versions
    if all(v.status == VersionStatus.CONNECTED for v in versions):
        new_version = Version(
            primary_asset_id=primary_asset.id,
            vcs_hash=commit,
            status=VersionStatus.CONNECTING,
            previous_version_id=versions[0].id if versions else None,
            vcs_metadata=vcs_info.model_dump() if vcs_info else None,
        )
        session.add(new_version)
        return new_version.id

    generation_statuses = [
        VersionStatus.GENERATING,
        VersionStatus.GENERATION_COMPLETE,
        VersionStatus.GENERATION_ERROR,
    ]
    if any(v.status in generation_statuses for v in versions):
        for version in versions:
            if version.status in [
                VersionStatus.GENERATION_COMPLETE,
                VersionStatus.GENERATION_ERROR,
            ]:
                new_version = Version(
                    primary_asset_id=primary_asset.id,
                    vcs_hash=commit,
                    status=VersionStatus.GENERATING,
                    previous_version_id=version.id,
                    vcs_metadata=vcs_info.model_dump() if vcs_info else None,
                )
                session.add(new_version)
                return new_version.id
            if version.status == VersionStatus.GENERATING:
                logger.warning(f"Generation in progress for {repo_name}, ignoring push")
                return None

    if versions and versions[0].status == VersionStatus.CONNECTING:
        logger.info(f"Version already connecting for {repo_name}")
        return None

    return None


def _create_new_asset(
    session: Session,
    org_id: str,
    repo_name: str,
    repo_id: str | int,
    installation_id: str,
    commit: str,
    vcs_info: VersionControlInfo | None,
) -> tuple[UUID, UUID]:
    """Create new primary asset and version. Returns (primary_asset_id, version_id)."""
    from database.models import PrimaryAsset, Version

    primary_asset = PrimaryAsset(
        display_name=repo_name,
        organization_id=org_id,
        kind=PrimaryAssetKind.CODEBASE,
        repository_id=str(repo_id),
        installation_id=installation_id,
        codebase_settings_auto_commit_docs=False,
        provider=PrimaryAssetProvider.BITBUCKET_DATA_CENTER,
        vcs_auto_update_policy=VcsAutoUpdatePolicy.AFTER_EVERY_COMMIT,
    )
    session.add(primary_asset)

    version = Version(
        primary_asset_id=primary_asset.id,
        vcs_hash=commit,
        status=VersionStatus.CONNECTING,
        previous_version_id=None,
        vcs_metadata=vcs_info.model_dump() if vcs_info else None,
    )
    session.add(version)

    _create_git_provider_grants(session, primary_asset.id, org_id)
    logger.info(f"Created primary asset and version for {repo_name}:{commit}")

    return primary_asset.id, version.id


def _prepare_repo_context(repo: dict[str, Any]) -> dict[str, Any] | None:
    """Prepare and validate repo context for download. Returns None on validation failure."""
    info = _extract_repo_info(repo)
    repo_name = info["repo_name"]
    installation_id = info["installation_id"]

    if not installation_id:
        logger.warning(f"Missing installation_id for repo {repo_name}")
        return None

    secrets = fetch_secrets(installation_id)
    instance_url = secrets.get("instance_url") or info["metadata"].get("instance_url")
    repo_id = info["repo_id"]
    project_key = info["project_key"]

    if not repo_id or not repo_name or not project_key or not instance_url:
        logger.warning(f"Missing required repo data: {repo}")
        return None

    return {
        "repo_name": repo_name,
        "repo_id": repo_id,
        "project_key": project_key,
        "repo_slug": info["repo_slug"],
        "installation_id": installation_id,
        "tracked_branch": info["tracked_branch"],
        "instance_url": instance_url,
        "verify": _get_ssl_context(secrets),
        "ca_bundle_path": secrets.get("ca_bundle_path"),
        "disable_ssl_verify": secrets.get("disable_ssl_verify", False),
    }


def _persist_version_in_db(
    org_id: str,
    repo_id: str | int,
    repo_name: str,
    installation_id: str,
    commit: str,
    vcs_info: VersionControlInfo | None,
    is_push: bool,
) -> tuple[UUID, UUID] | None:
    """Create or update version in database. Returns (primary_asset_id, version_id) or None."""
    from database.db import engine
    from database.models import PrimaryAsset

    try:
        with Session(engine) as session, session.begin():
            if is_push:
                primary_asset = session.exec(
                    select(PrimaryAsset)
                    .where(
                        PrimaryAsset.organization_id == org_id,
                        PrimaryAsset.repository_id == str(repo_id),
                    )
                    .options(selectinload(PrimaryAsset.versions))
                ).first()
                if not primary_asset:
                    logger.warning(
                        f"Primary asset not found for {repo_name}, org: {org_id}"
                    )
                    return None
                version_id = _handle_push_version(
                    session, primary_asset, commit, vcs_info, repo_name
                )
                if not version_id:
                    return None
                return primary_asset.id, version_id

            return _create_new_asset(
                session, org_id, repo_name, repo_id, installation_id, commit, vcs_info
            )
    except IntegrityError:
        logger.error(f"Failed to create primary asset for {repo_name}:{commit}")
        return None


def _download_and_upload_to_s3(
    org_id: str,
    primary_asset_id: UUID,
    version_id: UUID,
    ctx: dict[str, Any],
    commit: str,
    access_token: str,
) -> None:
    """Download repository and upload to S3."""
    codebase_metadata = generate_codebase_metadata(
        org_id,
        ctx["project_key"],
        ctx["repo_name"],
        ctx["repo_id"],
        version_id,
        ctx["repo_name"],
        ctx["installation_id"],
        ctx["instance_url"],
    )

    zip_content = download_repo(
        ctx["instance_url"],
        ctx["project_key"],
        ctx["repo_slug"],
        commit,
        access_token,
        ctx["ca_bundle_path"],
        ctx["disable_ssl_verify"],
    )
    logger.info(f"Repository downloaded. Size: {len(zip_content)} bytes")

    org_hashed_id = hashlib.sha256(org_id.encode("utf-8")).hexdigest()[:63]
    upload_key = (
        f"assets/{org_hashed_id}/{primary_asset_id}/{version_id}/{ctx['repo_name']}.zip"
    )
    upload_to_s3_with_metadata(zip_content, codebase_metadata, upload_key)
    logger.info(f"Repository {ctx['repo_name']} uploaded to {upload_key}")


def download_and_upload_repo(
    org_id: str, repo: dict[str, Any], access_token: str, is_push: bool = False
) -> str | None:
    """Returns repo_name on failure, None on success."""
    ctx = _prepare_repo_context(repo)
    if not ctx:
        return repo.get("repo_name") or repo.get("name") or "unknown"

    commit = _get_repo_commit(
        repo,
        ctx["instance_url"],
        ctx["project_key"],
        ctx["repo_slug"],
        access_token,
        ctx["tracked_branch"],
        ctx["verify"],
    )
    if not commit:
        logger.warning(
            f"Repository {ctx['repo_name']} appears to be empty (no commits). Skipping."
        )
        return ctx["repo_name"]

    vcs_info = fetch_vcs_info(
        ctx["instance_url"],
        ctx["project_key"],
        ctx["repo_slug"],
        access_token,
        commit,
        ctx["tracked_branch"],
        ctx["verify"],
    )

    result = _persist_version_in_db(
        org_id,
        ctx["repo_id"],
        ctx["repo_name"],
        ctx["installation_id"],
        commit,
        vcs_info,
        is_push,
    )
    if not result:
        return ctx["repo_name"]

    primary_asset_id, version_id = result
    _download_and_upload_to_s3(
        org_id, primary_asset_id, version_id, ctx, commit, access_token
    )
    return None


def get_repo_clone_info_from_id(
    instance_url: str,
    project_key: str,
    repo_slug: str,
) -> tuple[str, str]:
    """Returns (clone_url, full_name). Clone URL requires Bearer auth via git http.extraHeader."""
    clone_url = build_clone_url(instance_url, project_key, repo_slug)
    full_name = f"{project_key}/{repo_slug}"
    return clone_url, full_name


# =============================================================================
# Pull Request Functions for Push Bot
# =============================================================================


@NETWORK_RETRY
def list_pull_requests(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    access_token: str,
    state: Literal["OPEN", "MERGED", "DECLINED", "ALL"] = "OPEN",
    verify: bool | ssl.SSLContext = True,
) -> list[dict[str, Any]]:
    api_base = f"{instance_url.rstrip('/')}/rest/api/1.0"
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{api_base}/projects/{project_key}/repos/{repo_slug}/pull-requests"

    all_prs: list[dict[str, Any]] = []
    start = 0
    limit = 25

    with httpx.Client(verify=verify, timeout=30.0) as client:
        while True:
            params = {"state": state, "start": start, "limit": limit}
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()

            data = response.json()
            all_prs.extend(data.get("values", []))

            if data.get("isLastPage", True):
                break

            start = data.get("nextPageStart", start + limit)

    return all_prs


@NETWORK_RETRY
def get_pull_request_commits(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    pr_id: int,
    access_token: str,
    verify: bool | ssl.SSLContext = True,
) -> list[dict[str, Any]]:
    api_base = f"{instance_url.rstrip('/')}/rest/api/1.0"
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{api_base}/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/commits"

    all_commits: list[dict[str, Any]] = []
    start = 0
    limit = 25

    with httpx.Client(verify=verify, timeout=30.0) as client:
        while True:
            params = {"start": start, "limit": limit}
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()

            data = response.json()
            all_commits.extend(data.get("values", []))

            if data.get("isLastPage", True):
                break

            start = data.get("nextPageStart", start + limit)

    return all_commits


@NETWORK_RETRY
def decline_pull_request(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    pr_id: int,
    access_token: str,
    verify: bool | ssl.SSLContext = True,
) -> None:
    api_base = f"{instance_url.rstrip('/')}/rest/api/1.0"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    pr_url = (
        f"{api_base}/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
    )

    with httpx.Client(verify=verify, timeout=30.0) as client:
        pr_response = client.get(pr_url, headers=headers)
        pr_response.raise_for_status()

        pr_data = pr_response.json()
        state = pr_data.get("state", "").upper()

        if state in ["MERGED", "DECLINED"]:
            logger.info(f"Pull request #{pr_id} is already {state.lower()}")
            return

        version = pr_data.get("version", 0)

        decline_url = f"{pr_url}/decline"
        params = {"version": version}

        response = client.post(decline_url, headers=headers, params=params)

        try:
            response.raise_for_status()
            logger.info(f"Declined pull request #{pr_id}")
        except httpx.HTTPStatusError as e:
            error_detail = ""
            try:
                error_json = e.response.json()
                error_detail = f" - {error_json}"
            except (ValueError, KeyError):
                error_detail = f" - {e.response.text}"

            logger.error(f"Failed to decline pull request #{pr_id}: {e}{error_detail}")
            raise


@NETWORK_RETRY
def create_pull_request(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    access_token: str,
    source_branch: str,
    commit_slug: str,
    target_branch: str | None = None,
    verify: bool | ssl.SSLContext = True,
) -> dict[str, Any] | None:
    """Returns created PR data or None if PR already exists."""
    api_base = f"{instance_url.rstrip('/')}/rest/api/1.0"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Get default branch if target not specified
    if target_branch is None:
        target_branch = get_default_branch(
            instance_url, project_key, repo_slug, access_token, verify
        )

    pr_data = {
        "title": f"Update driver docs for commit {commit_slug}",
        "description": f"Automated update of driver documentation for commit {commit_slug}",
        "fromRef": {
            "id": f"refs/heads/{source_branch}",
            "repository": {
                "slug": repo_slug,
                "project": {"key": project_key},
            },
        },
        "toRef": {
            "id": f"refs/heads/{target_branch}",
            "repository": {
                "slug": repo_slug,
                "project": {"key": project_key},
            },
        },
    }

    url = f"{api_base}/projects/{project_key}/repos/{repo_slug}/pull-requests"

    with httpx.Client(verify=verify, timeout=30.0) as client:
        response = client.post(url, headers=headers, json=pr_data)

        try:
            response.raise_for_status()
            result = response.json()
            pr_link = result.get("links", {}).get("self", [{}])[0].get("href", "")
            logger.info(f"Pull request created successfully: {pr_link}")
            return result
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                logger.info("Pull request already exists for this branch")
                return None
            error_detail = ""
            try:
                error_json = e.response.json()
                error_detail = f" - {error_json}"
            except (ValueError, KeyError):
                error_detail = f" - {e.response.text}"

            logger.error(f"Failed to create pull request: {e}{error_detail}")
            raise


def _decline_bot_pull_requests(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    access_token: str,
    verify: bool | ssl.SSLContext,
) -> None:
    """Decline all open PRs from docs_* branches created by the bot."""
    bot_email = "bot@driverai.com"

    existing_prs = list_pull_requests(
        instance_url, project_key, repo_slug, access_token, state="OPEN", verify=verify
    )
    errors: list[Exception] = []

    for pr in existing_prs:
        source_branch = pr["fromRef"]["displayId"]
        if not source_branch.startswith("docs_"):
            continue

        try:
            pr_id = pr["id"]
            commits = get_pull_request_commits(
                instance_url, project_key, repo_slug, pr_id, access_token, verify=verify
            )

            is_bot_pr = any(
                bot_email in commit["author"]["emailAddress"] for commit in commits
            )

            if is_bot_pr:
                decline_pull_request(
                    instance_url,
                    project_key,
                    repo_slug,
                    pr_id,
                    access_token,
                    verify=verify,
                )
                logger.info(
                    f"Closed existing bot PR #{pr_id} from branch {source_branch}"
                )
        except Exception as e:
            errors.append(e)

    if errors:
        raise ExceptionGroup("Failed to decline some bot PRs", errors)


def create_pull_request_with_bot_cleanup(
    instance_url: str,
    project_key: str,
    repo_slug: str,
    access_token: str,
    branch: str,
    commit_slug: str,
    installation_id: str,
    tracked_branch: str | None = None,
) -> None:
    """Create PR and decline existing bot PRs from docs_* branches."""
    secrets = fetch_secrets(installation_id)
    verify = _get_ssl_context(secrets)

    _decline_bot_pull_requests(
        instance_url, project_key, repo_slug, access_token, verify
    )
    create_pull_request(
        instance_url,
        project_key,
        repo_slug,
        access_token,
        branch,
        commit_slug,
        tracked_branch,
        verify=verify,
    )
