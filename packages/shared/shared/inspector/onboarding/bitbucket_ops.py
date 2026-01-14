import hashlib
import json
import logging
import os
from uuid import UUID

import requests
from database.models import Organization, PrimaryAssetRoleGrant
from database.models_enums import (
    PrimaryAssetRole,
    PrincipalKind,
    SourceVisibility,
    VcsAutoUpdatePolicy,
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
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

logger = logging.getLogger(__name__)


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


def fetch_access_token(installation_id: str) -> str:
    logger.info(
        f"Fetching workspace access token for installation ID {installation_id}"
    )
    install_key = format_secret_name("GIT_PROVIDER_WAT_INSTALL_SECRET", installation_id)
    secrets_manager = AWSSecretManagementStrategy(
        AWSClientConfig(region_name=os.environ["AWS_REGION"])
    )
    secret_value = secrets_manager.read_secret(install_key)
    if not secret_value:
        raise AccessTokenError("Workspace access token not found")

    if isinstance(secret_value, str):
        return secret_value

    return secret_value["token"]


def get_default_branch(workspace: str, repo_slug: str, access_token: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}"

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    data = response.json()
    # Default branch info is in mainbranch.name
    return data.get("mainbranch", {}).get("name", "main")


def download_repo(
    workspace: str, repo_slug: str, commit: str, access_token: str
) -> bytes:
    import shutil
    import subprocess
    import tempfile
    import zipfile
    from pathlib import Path

    logger.info(
        f"Using git clone to download repository {workspace}/{repo_slug} at commit {commit}"
    )

    # Create a temporary directory for cloning
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir) / repo_slug

        clone_url = f"https://x-token-auth:{access_token}@bitbucket.org/{workspace}/{repo_slug}.git"

        try:
            # Try to clone with shallow depth at specific commit
            logger.info(f"Attempting to clone repository at commit {commit}...")

            # First, try a shallow clone of the specific commit
            clone_cmd = [
                "git",
                "clone",
                "--no-checkout",  # Don't checkout files yet
                clone_url,
                str(repo_path),
            ]

            clone_result = subprocess.run(
                clone_cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            if clone_result.returncode != 0:
                logger.error(f"Clone failed: {clone_result.stderr}")
                raise Exception(f"Failed to clone repository: {clone_result.stderr}")

            logger.info(
                "Repository cloned successfully, checking out specific commit..."
            )

            # Checkout the specific commit
            checkout_result = subprocess.run(
                ["git", "checkout", commit],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
            )

            if checkout_result.returncode != 0:
                logger.warning(
                    f"Could not checkout commit {commit}: {checkout_result.stderr}"
                )
                # Try fetching the commit first
                logger.info("Fetching all commits and trying again...")

                subprocess.run(
                    ["git", "fetch", "--unshallow"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                )

                # Try checkout again
                checkout_result = subprocess.run(
                    ["git", "checkout", commit],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                )

                if checkout_result.returncode != 0:
                    logger.error(
                        f"Failed to checkout commit {commit}: {checkout_result.stderr}"
                    )
                    raise Exception(f"Failed to checkout commit {commit}")

            logger.info(f"Successfully checked out commit {commit}")

            # Remove .git directory to reduce size
            git_dir = repo_path / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)

            # Create a zip archive
            zip_path = Path(temp_dir) / f"{repo_slug}.zip"
            logger.info("Creating zip archive...")

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                # Walk through all files and add them to the zip
                for file_path in repo_path.rglob("*"):
                    if file_path.is_file():
                        # Get the relative path from the repo root
                        arcname = file_path.relative_to(repo_path)
                        zipf.write(file_path, arcname)

            # Read the zip file content
            with open(zip_path, "rb") as f:
                zip_content = f.read()

            logger.info(f"Archive created successfully. Size: {len(zip_content)} bytes")
            return zip_content

        except subprocess.TimeoutExpired:
            raise Exception("Git clone operation timed out")
        except Exception as e:
            logger.error(f"Error during repository download: {e!s}")
            raise


def get_latest_commit(
    workspace: str, repo_slug: str, access_token: str, default_branch: str
) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/commits/{default_branch}"

    response = requests.get(url, headers=headers, params={"pagelen": 1})
    response.raise_for_status()

    commits = response.json().get("values", [])
    if commits:
        return commits[0]["hash"]
    raise ValueError(f"No commits found on default branch '{default_branch}'")


def fetch_vcs_info(
    workspace: str,
    repo_slug: str,
    access_token: str,
    commit_sha: str,
    tracked_branch: str | None = None,
) -> VersionControlInfo:
    headers = {"Authorization": f"Bearer {access_token}"}

    # Fetch repository information
    repo_url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}"
    repo_response = requests.get(repo_url, headers=headers)
    repo_response.raise_for_status()
    repo_data = repo_response.json()
    logger.info(
        f"Repo information retrieved from Bitbucket API (status code {repo_response.status_code}): {repo_data}"
    )

    default_branch = repo_data.get("mainbranch", {}).get("name", "main")

    branch_name = tracked_branch if tracked_branch is not None else default_branch

    # Fetch detailed commit information
    commit_url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/commit/{commit_sha}"
    commit_response = requests.get(commit_url, headers=headers)
    commit_response.raise_for_status()
    commit_data = commit_response.json()
    logger.info(
        f"Commit data retrieved from Bitbucket API (status code {commit_response.status_code}): {commit_data}"
    )

    # Build VersionControlInfo
    author_info = AuthorInfo(
        email=commit_data.get("author", {}).get("raw", "").split("<")[-1].rstrip(">")
        if "<" in commit_data.get("author", {}).get("raw", "")
        else "",
        name=commit_data.get("author", {}).get("raw", "").split("<")[0].strip()
        if "<" in commit_data.get("author", {}).get("raw", "")
        else commit_data.get("author", {}).get("raw", ""),
        date=commit_data.get("date", ""),
    )

    commit_info = CommitInfo(
        sha=commit_data.get("hash", ""),
        message=commit_data.get("message", ""),
        url=commit_data.get("links", {}).get("html", {}).get("href", ""),
        author=author_info,
    )

    branch_info = BranchInfo(name=branch_name)

    repo_info = RepoInfo(
        name=repo_data.get("name", ""),
        namespace=workspace,
        full_name=repo_data.get("full_name", ""),
        url=repo_data.get("links", {}).get("html", {}).get("href", ""),
    )

    return VersionControlInfo(
        repository=repo_info,
        commit=commit_info,
        branch=branch_info,
    )


def generate_codebase_metadata(
    org_id: str,
    workspace: str,
    repo_name: str,
    repo_id: str | int,
    provider: str,
    version_id: str | UUID,
    asset_name: str,
    install_id: str,
) -> dict:
    from database.models_enums import PrimaryAssetKind

    return {
        "unhashed_organization_id": org_id,
        "full_repo_name": repo_name,  # NOTE: just used for debugging
        "workspace": workspace,
        "provider": provider,
        "version_id": str(version_id),
        "repository_id": str(repo_id),
        "asset_name": asset_name,
        "asset_kind": PrimaryAssetKind.CODEBASE,
        "install_id": install_id,
    }


def download_and_upload_repo(
    org_id: str, repo: dict, access_token: str, is_push: bool = False
) -> str | None:
    """Download and upload Bitbucket repository"""
    from database.db import engine
    from database.models import PrimaryAsset, Version
    from database.models_enums import (
        PrimaryAssetKind,
        PrimaryAssetProvider,
        VersionStatus,
    )
    from sqlalchemy.exc import IntegrityError

    # Handle different repo dict structures
    # From GitRepository model or from webhook
    metadata = repo.get("metadata", {})
    repo_id = repo.get("repo_id") or metadata.get("id") or metadata.get("uuid")
    repo_name = repo.get("repo_name") or repo.get("name")
    workspace = metadata.get("workspace") or repo.get("workspace")
    repo_slug = "-".join(
        repo_name.split()
    )  # Bitbucket allows spaces in repo names, which are replaced by dashes in the slug

    # Handle missing fields
    if not repo_id:
        logger.error(f"Missing repo_id in repo data: {repo}")
        return repo_name
    if not repo_name:
        logger.error(f"Missing repo_name in repo data: {repo}")
        return "unknown"
    if not workspace:
        logger.error(f"Missing workspace in repo data: {repo}")
        return repo_name
    if not repo_slug:
        logger.error(f"Missing repo slug in repo data: {repo}")
        return repo_name

    # Get latest commit if not provided
    commit = None
    if repo.get("latest_commit"):
        if isinstance(repo["latest_commit"], dict):
            commit = repo["latest_commit"].get("id") or repo["latest_commit"].get(
                "commit", {}
            ).get("id")
        else:
            commit = repo["latest_commit"]

    installation_id = repo.get("installation_id")
    if not installation_id:
        logger.error(f"Missing installation_id for repo {repo_name}")
        return repo_name

    tracked_branch = repo.get("tracked_branch")

    if not commit:
        branch_to_check = tracked_branch if tracked_branch else repo["default_branch"]
        commit = get_latest_commit(workspace, repo_slug, access_token, branch_to_check)

    # Fetch version control information with tracked branch
    vcs_info = fetch_vcs_info(
        workspace=workspace,
        repo_slug=repo_slug,
        access_token=access_token,
        commit_sha=commit,
        tracked_branch=tracked_branch,
    )

    try:
        with Session(engine) as session, session.begin():
            if is_push:
                primary_asset = session.exec(
                    select(PrimaryAsset)
                    .where(
                        PrimaryAsset.organization_id == org_id,
                        PrimaryAsset.repository_id
                        == str(repo_id),  # Ensure it's a string
                    )
                    .options(selectinload(PrimaryAsset.versions))
                ).first()
                if not primary_asset:
                    logger.error(
                        f"Failed to find primary asset for {repo_name} for org: {org_id}, unable to process push event"
                    )
                    return repo_name
                primary_asset_id = primary_asset.id

                # Handle version creation based on current status
                if all(
                    v.status == VersionStatus.CONNECTED for v in primary_asset.versions
                ):
                    new_version = Version(
                        primary_asset_id=primary_asset.id,
                        vcs_hash=commit,
                        status=VersionStatus.CONNECTING,
                        previous_version_id=primary_asset.versions[0].id
                        if primary_asset.versions
                        else None,
                        vcs_metadata=vcs_info.model_dump() if vcs_info else None,
                    )
                    session.add(new_version)
                    version_id = new_version.id
                elif any(
                    v.status
                    in [
                        VersionStatus.GENERATING,
                        VersionStatus.GENERATION_COMPLETE,
                        VersionStatus.GENERATION_ERROR,
                    ]
                    for v in primary_asset.versions
                ):
                    # Handle existing generating versions
                    for version in primary_asset.versions:
                        if version.status in [
                            VersionStatus.GENERATION_COMPLETE,
                            VersionStatus.GENERATION_ERROR,
                        ]:
                            new_version = Version(
                                primary_asset_id=primary_asset.id,
                                vcs_hash=commit,
                                status=VersionStatus.GENERATING,
                                previous_version_id=version.id,
                                vcs_metadata=vcs_info.model_dump()
                                if vcs_info
                                else None,
                            )
                            session.add(new_version)
                            version_id = new_version.id
                            break
                        elif version.status == VersionStatus.GENERATING:
                            # STOPGAP: Ignore push events during active generation to ensure completion
                            logger.warning(
                                f"Generation already in progress for {repo.get('name', 'unknown')}. "
                                f"Ignoring push event to allow current generation to complete."
                            )
                            return repo

                elif (
                    primary_asset.versions
                    and primary_asset.versions[0].status == VersionStatus.CONNECTING
                ):
                    logger.info(
                        f"Version already in connecting state for {repo_name}, skipping..."
                    )
                    return repo_name
                else:
                    return repo_name
            else:
                # Create new primary asset and version
                primary_asset = PrimaryAsset(
                    display_name=repo_name,
                    organization_id=org_id,
                    kind=PrimaryAssetKind.CODEBASE,
                    repository_id=str(repo_id),  # Ensure it's a string
                    installation_id=installation_id,
                    codebase_settings_auto_commit_docs=False,
                    provider=PrimaryAssetProvider.BITBUCKET,
                    vcs_auto_update_policy=VcsAutoUpdatePolicy.AFTER_EVERY_COMMIT,
                )
                session.add(primary_asset)
                primary_asset_id = primary_asset.id

                version = Version(
                    primary_asset_id=primary_asset.id,
                    vcs_hash=commit,
                    status=VersionStatus.CONNECTING,
                    previous_version_id=None,
                    vcs_metadata=vcs_info.model_dump() if vcs_info else None,
                )
                session.add(version)
                version_id = version.id

                _create_git_provider_grants(session, primary_asset_id, org_id)

                logger.info(
                    f"Creating primary asset and version for {repo_name}:{commit} for org: {org_id}. Version ID: {version_id}"
                )
    except IntegrityError:
        logger.error(
            f"Failed to create primary asset and version {repo_name}:{commit} for org: {org_id}"
        )
        return repo_name

    # Generate metadata
    metadata = generate_codebase_metadata(
        org_id,
        workspace,
        repo_name,
        repo_id,
        "bitbucket",
        version_id,
        repo_name,
        installation_id,
    )

    # Download repository
    zip_content = download_repo(workspace, repo_slug, commit, access_token)
    logger.info(f"Repository downloaded successfully. Size: {len(zip_content)} bytes")

    # Upload to S3
    org_hashed_id = hashlib.sha256(org_id.encode("utf-8")).hexdigest()[:63]
    upload_key = (
        f"assets/{org_hashed_id}/{primary_asset_id}/{version_id}/{repo_name}.zip"
    )
    upload_to_s3_with_metadata(zip_content, metadata, upload_key)
    logger.info(f"Repository {repo_name} uploaded successfully to {upload_key}.")

    return None


def get_repo_clone_info_from_id(
    workspace: str, repo_slug: str, access_token: str
) -> tuple[str, str]:
    """Get repository clone URL and full name"""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}"
    #
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    data = response.json()
    full_name = data.get("full_name")

    clone_url = (
        f"https://x-token-auth:{access_token}@bitbucket.org/{workspace}/{repo_slug}.git"
    )

    return clone_url, full_name


def fetch_bitbucket_default_branch_name(
    workspace: str, repo_slug: str, access_token: str
) -> str:
    """Fetch default branch name for Bitbucket repository"""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}"

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    data = response.json()
    return data.get("mainbranch", {}).get("name", "main")


def list_pull_requests(
    workspace: str, repo_slug: str, access_token: str, state: str = "OPEN"
) -> list:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests"
    params = {"state": state}

    all_prs = []

    # Handle pagination
    while url:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()

        data = response.json()
        all_prs.extend(data.get("values", []))

        # Get next page URL
        url = data.get("next")
        params = {}  # Clear params for subsequent requests as they're in the URL

    return all_prs


def get_pull_request_commits(
    workspace: str, repo_slug: str, pr_id: int, access_token: str
) -> list:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/commits"

    all_commits = []

    # Handle pagination
    while url:
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        all_commits.extend(data.get("values", []))

        # Get next page URL
        url = data.get("next")

    return all_commits


def close_pull_request(
    workspace: str, repo_slug: str, pr_id: int, access_token: str
) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # First, check the PR status
    pr_url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}"
    pr_response = requests.get(pr_url, headers=headers)

    if pr_response.status_code == 200:
        pr_data = pr_response.json()
        state = pr_data.get("state", "").upper()

        # Check if PR is already closed
        if state in ["MERGED", "DECLINED", "SUPERSEDED"]:
            logger.info(f"Pull request #{pr_id} is already {state.lower()}")
            return

    # Try to decline the PR
    decline_url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/decline"
    data = {"message": "Closing this PR - no longer needed"}
    response = requests.post(decline_url, headers=headers, data=json.dumps(data))

    try:
        response.raise_for_status()
        logger.info(f"Closed pull request #{pr_id}")
    except requests.HTTPError as e:
        # Get more details about the error
        error_detail = ""
        try:
            error_json = e.response.json()
            error_detail = f" - {error_json}"
        except (ValueError, AttributeError):
            error_detail = f" - {e.response.text}"

        logger.error(f"Failed to close pull request #{pr_id}: {e}{error_detail}")
        raise


def create_pull_request(
    workspace: str,
    repo_slug: str,
    access_token: str,
    branch: str,
    commit_slug: str,
    tracked_branch: str | None,
) -> None:
    if tracked_branch is None:
        tracked_branch = fetch_bitbucket_default_branch_name(
            workspace, repo_slug, access_token
        )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    pr_data = {
        "title": f"Update driver docs for commit {commit_slug}",
        "description": f"Automated update of driver documentation for commit {commit_slug}",
        "source": {"branch": {"name": branch}},
        "destination": {"branch": {"name": tracked_branch}},
        "close_source_branch": False,
    }

    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests"
    response = requests.post(url, headers=headers, json=pr_data)

    try:
        response.raise_for_status()
        logger.info(
            f"Pull request created successfully: {response.json()['links']['html']['href']}"
        )
    except requests.HTTPError as e:
        if e.response.status_code == 400:
            error_detail = e.response.json()
            if "already exists" in str(error_detail):
                logger.info("Pull request already exists for this branch")
            else:
                raise
        else:
            raise


def create_pull_request_with_bot_cleanup(
    workspace: str,
    repo_slug: str,
    access_token: str,
    branch: str,
    commit_slug: str,
    tracked_branch: str | None,
) -> None:
    """Create a pull request and close any existing bot PRs from docs_* branches."""
    BOT_NAME = "docs-bot"
    BOT_EMAIL = "bot@driverai.com"

    logger.info("Checking for existing bot pull requests...")

    try:
        existing_prs = list_pull_requests(workspace, repo_slug, access_token)

        for pr in existing_prs:
            source_branch = pr.get("source", {}).get("branch", {}).get("name", "")

            if source_branch.startswith("docs_"):
                try:
                    pr_id = pr["id"]
                    commits = get_pull_request_commits(
                        workspace, repo_slug, pr_id, access_token
                    )

                    # Check if any commit is authored by the bot
                    is_bot_pr = any(
                        BOT_EMAIL in commit.get("author", {}).get("raw", "")
                        or BOT_NAME in commit.get("author", {}).get("raw", "")
                        for commit in commits
                    )

                    if is_bot_pr:
                        try:
                            close_pull_request(
                                workspace, repo_slug, pr_id, access_token
                            )
                            logger.info(
                                f"Closed existing bot PR #{pr_id} from branch {source_branch}"
                            )
                        except Exception as close_error:
                            # Log but don't fail if we can't close the PR
                            logger.warning(
                                f"Could not close PR #{pr_id}: {close_error}"
                            )

                except Exception as e:
                    logger.error(f"Error checking PR #{pr.get('id', 'unknown')}: {e}")

    except Exception as e:
        logger.error(f"Error listing pull requests: {e}")

    # Create new pull request
    create_pull_request(
        workspace, repo_slug, access_token, branch, commit_slug, tracked_branch
    )
