import base64
import hashlib
import logging
import os
import re
import time
from uuid import UUID

import httpx
import jwt
import requests
from database.models import Organization, PrimaryAssetRoleGrant
from database.models_enums import (
    PrimaryAssetRole,
    PrincipalKind,
    SourceVisibility,
    VcsAutoUpdatePolicy,
)
from shared.inspector.onboarding.onboard_utils import AccessTokenError
from shared.inspector.onboarding.vcs_utils import (
    AuthorInfo,
    BranchInfo,
    CommitInfo,
    RepoInfo,
    VersionControlInfo,
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


def generate_jwt() -> str:
    payload = {
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
        "iss": os.environ["GH_CLIENT_ID"],
    }
    decoded_pem = base64.b64decode(os.environ["GH_CLIENT_PEM_SECRET"])
    return jwt.encode(payload, decoded_pem, algorithm="RS256")


def fetch_app_access_token(installation_id: str) -> str:
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    jwt = generate_jwt()
    with httpx.Client() as client:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {jwt}"}
        response = client.post(url, headers=headers)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise AccessTokenError(
                    f"GitHub application installation {installation_id} not found."
                ) from e
            raise
        token_data = response.json()
        if "token" not in token_data:
            raise AccessTokenError("GitHub application access token not found.")
        return token_data["token"]


def get_github_repo_url(full_repo_name: str) -> str:
    return f"https://api.github.com/repos/{full_repo_name}"


def fetch_default_branch_and_commit(full_repo_name: str, access_token: str) -> str:
    headers = {"Authorization": f"token {access_token}"}
    repo_url = get_github_repo_url(full_repo_name=full_repo_name)

    repo = requests.get(repo_url, headers=headers)
    repo_data = repo.json()
    logger.info(
        f"Repo information retrieved from github API (status code {repo.status_code}): {repo_data}"
    )
    default_branch = repo_data["default_branch"]

    branch_url = f"{repo_url}/branches/{default_branch}"
    branch_response = requests.get(branch_url, headers=headers)
    branch_data = branch_response.json()
    logger.info(f"Default branch for {full_repo_name} is {default_branch}")
    logger.info(
        f"Branch data retrieved from github API (status code {branch_response.status_code}): {branch_data}"
    )
    return branch_data["commit"]["sha"]


def fetch_vcs_info(
    full_repo_name: str, access_token: str, commit_sha: str
) -> VersionControlInfo:
    headers = {"Authorization": f"token {access_token}"}
    repo_url = get_github_repo_url(full_repo_name=full_repo_name)

    repo = requests.get(repo_url, headers=headers)
    repo_data = repo.json()
    logger.info(
        f"Repo information retrieved from github API (status code {repo.status_code}): {repo_data}"
    )
    default_branch = repo_data["default_branch"]

    commit_url = f"{repo_url}/commits/{commit_sha}"
    commit_response = requests.get(commit_url, headers=headers)
    commit_data = commit_response.json()
    logger.info(
        f"Commit data retrieved from github API (status code {commit_response.status_code}): {commit_data}"
    )
    author_info = AuthorInfo(
        email=commit_data["commit"]["author"]["email"],
        name=commit_data["commit"]["author"]["name"],
        date=commit_data["commit"]["author"]["date"],
    )
    commit_info = CommitInfo(
        sha=commit_data["sha"],
        message=commit_data["commit"]["message"],
        url=commit_data["html_url"],
        author=author_info,
    )
    branch_info = BranchInfo(name=default_branch)
    repo_info = RepoInfo(
        name=repo_data["name"],
        namespace=repo_data["owner"]["login"],
        full_name=repo_data["full_name"],
        url=repo_data["html_url"],
    )
    return VersionControlInfo(
        repository=repo_info,
        commit=commit_info,
        branch=branch_info,
    )


def fetch_github_default_branch_name(full_repo_name: str, access_token: str) -> str:
    headers = {"Authorization": f"token {access_token}"}
    repo_url = get_github_repo_url(full_repo_name=full_repo_name)
    repo = requests.get(repo_url, headers=headers)
    repo_data = repo.json()
    return repo_data["default_branch"]


def generate_codebase_metadata(
    org_id: str,
    full_repo_name: str,
    repo_id: str | int,
    provider: str,
    version_id: str | UUID,
    asset_name: str,
    install_id: str,
) -> dict:
    from database.models_enums import PrimaryAssetKind

    return {
        "unhashed_organization_id": org_id,
        "full_repo_name": full_repo_name,  # NOTE: just used for debugging
        "provider": provider,
        "version_id": str(version_id),
        "repository_id": str(repo_id),  # NOTE: just used for debugging
        "asset_name": asset_name,
        "asset_kind": PrimaryAssetKind.CODEBASE,
        "install_id": install_id,
    }


def download_github_repo_zip(full_name: str, commit: str, access_token: str) -> bytes:
    headers = {"Authorization": f"token {access_token}"}
    zip_url = f"https://api.github.com/repos/{full_name}/zipball/{commit}"
    response = requests.get(zip_url, headers=headers, timeout=120, allow_redirects=True)
    response.raise_for_status()
    return response.content


def download_and_upload_repo(
    org_id: str,
    repo: dict,
    access_token: str,
    install_id: str,
    is_push: bool = False,
) -> str | None:
    from database.db import engine
    from database.models import PrimaryAsset, Version
    from database.models_enums import (
        PrimaryAssetKind,
        PrimaryAssetProvider,
        VersionStatus,
    )
    from shared.inspector.onboarding.onboard_utils import upload_to_s3_with_metadata
    from sqlalchemy.exc import IntegrityError

    if not repo.get("commit"):
        try:
            commit = fetch_default_branch_and_commit(repo["full_name"], access_token)
        except KeyError:
            logger.error(f"Failed to find commit for {repo}, unable to process")
            return repo
    else:
        commit = repo["commit"]
    try:
        vcs_info = fetch_vcs_info(
            full_repo_name=repo["full_name"],
            access_token=access_token,
            commit_sha=commit,
        )
        with Session(engine) as session, session.begin():
            if is_push:
                primary_asset = session.exec(
                    select(PrimaryAsset)
                    .where(
                        PrimaryAsset.organization_id == org_id,
                        PrimaryAsset.repository_id == repo["id"],
                    )
                    .options(selectinload(PrimaryAsset.versions))
                ).first()
                if not primary_asset:
                    logger.error(
                        f"Failed to find primary asset for {repo} for org: {org_id}, unable to process push event"
                    )
                    return repo
                primary_asset_id = primary_asset.id
                if all(
                    v.status == VersionStatus.CONNECTED for v in primary_asset.versions
                ):
                    new_version = Version(
                        primary_asset_id=primary_asset.id,
                        vcs_hash=commit,
                        status=VersionStatus.CONNECTING,
                        previous_version_id=primary_asset.versions[
                            0
                        ].id,  # TODO: don't link this for connected only?
                        vcs_metadata=vcs_info.model_dump(),
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
                    # versions are in descending order of creation
                    for version in primary_asset.versions:
                        if version.status in [
                            VersionStatus.GENERATION_COMPLETE,
                            VersionStatus.GENERATION_ERROR,
                        ]:
                            new_version = Version(
                                primary_asset_id=primary_asset.id,
                                vcs_hash=commit,
                                status=VersionStatus.GENERATING,  # Immediately jump to generating. This signals run_codebase_connection to start inspection after connection
                                previous_version_id=version.id,
                                vcs_metadata=vcs_info.model_dump(),
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
                elif primary_asset.versions[0].status == VersionStatus.CONNECTING:
                    logger.info(
                        f"Version already in connecting state for {repo['name']}, skipping..."
                    )
                    return repo
                else:
                    # TODO: any other cases to handle explicitly?
                    return repo

            else:
                primary_asset = PrimaryAsset(
                    display_name=repo["name"],
                    organization_id=org_id,
                    kind=PrimaryAssetKind.CODEBASE,
                    repository_id=repo["id"],
                    codebase_settings_auto_commit_docs=False,
                    provider=PrimaryAssetProvider.GITHUB,
                    vcs_auto_update_policy=VcsAutoUpdatePolicy.AFTER_EVERY_COMMIT,
                )
                session.add(primary_asset)
                primary_asset_id = primary_asset.id

                version = Version(
                    primary_asset_id=primary_asset.id,
                    vcs_hash=commit,
                    status=VersionStatus.CONNECTING,
                    previous_version_id=None,
                    vcs_metadata=vcs_info.model_dump(),
                )
                session.add(version)
                version_id = version.id

                _create_git_provider_grants(session, primary_asset_id, org_id)

                logger.info(
                    f"Creating primary asset and version for {repo['name']}:{commit} for org: {org_id}. Version ID: {version_id}"
                )
    except IntegrityError:
        logger.error(
            f"Failed to create primary asset and version {repo['name']}:{commit} for org: {org_id}"
        )
        return repo

    # TODO: update this metadata for latest updates to onboarding logic
    metadata = generate_codebase_metadata(
        org_id,
        repo["full_name"],
        repo["id"],
        "github",
        version_id,
        repo["name"],
        install_id,
    )

    zip_content = download_github_repo_zip(repo["full_name"], commit, access_token)
    logger.info(f"Repository downloaded successfully. Size: {len(zip_content)} bytes")

    org_hashed_id = hashlib.sha256(org_id.encode()).hexdigest()[:63]
    # TODO: make a helper for constructing the upload key
    upload_key = (
        f"assets/{org_hashed_id}/{primary_asset_id}/{version_id}/{repo['name']}.zip"
    )
    upload_to_s3_with_metadata(zip_content, metadata, upload_key)
    logger.info(f"Repository {repo['name']} uploaded successfully to {upload_key}.")

    return None


def get_repo_clone_info_from_id(repo_id: str, github_token: str) -> tuple[str, str]:
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
    }

    with httpx.Client() as client:
        resp = client.get(
            f"https://api.github.com/repositories/{repo_id}", headers=headers
        )
        resp.raise_for_status()
        data = resp.json()

    full_name = data["full_name"]  # e.g., "org/repo"
    clone_url = f"https://x-access-token:{github_token}@github.com/{full_name}.git"
    return clone_url, full_name


def list_pull_requests(full_name: str, access_token: str, state: str = "open") -> list:
    headers = {
        "Authorization": f"Bearer {access_token}",
    }
    all_prs = []
    page_count = 1
    with httpx.Client() as client:
        url = f"https://api.github.com/repos/{full_name}/pulls"
        response = client.get(
            url,
            headers=headers,
            params={"state": state},
        )
        response.raise_for_status()
        all_prs.extend(response.json())

        # handle pagination
        link_header = response.headers.get("link", None)
        while link_header is not None:
            page_count = page_count + 1
            parts = response.headers["link"].split(",")
            matches = [
                re.search(r'<([^>]+)>; rel="([^"]+)"', part.strip()) for part in parts
            ]
            has_next = False
            for match in matches:
                next_url, rel = match.groups()
                if rel == "next" and next_url:
                    has_next = True
                    response = client.get(next_url, headers=headers)
                    response.raise_for_status()
                    all_prs.extend(response.json())
            if not has_next:
                break
    return all_prs


def get_pull_request_commits(full_name: str, pr_id: int, access_token: str) -> list:
    headers = {
        "Authorization": f"Bearer {access_token}",
    }
    with httpx.Client() as client:
        url = f"https://api.github.com/repos/{full_name}/pulls/{pr_id}/commits"
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def close_pull_request(full_name: str, pr_id: int, access_token: str) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
    }
    with httpx.Client() as client:
        # First check if PR is open
        url = f"https://api.github.com/repos/{full_name}/pulls/{pr_id}"
        response = client.get(url, headers=headers)
        response.raise_for_status()
        pr_data = response.json()

        if pr_data.get("state") != "open":
            logger.warning(
                f"PR #{pr_id} is already {pr_data.get('state', 'in unknown state')}, skipping close"
            )
            return

        # Close the PR
        response = client.patch(url, headers=headers, json={"state": "closed"})
        response.raise_for_status()
        logger.info(f"Closed pull request #{pr_id} for {full_name}")


def create_pull_request_with_bot_cleanup(
    full_name: str,
    branch: str,
    access_token: str,
    commit_slug: str,
) -> None:
    BOT_NAME = "docs-bot"
    BOT_EMAIL = "bot@driverai.com"

    logger.info("Checking for existing open bot pull requests...")

    try:
        existing_prs = list_pull_requests(full_name, access_token, state="open")

        for pr in existing_prs:
            # Double-check the PR is actually open
            if pr.get("state") != "open":
                logger.info(
                    f"Skipping PR #{pr['number']} - not in open state (state: {pr.get('state')})"
                )
                continue

            source_branch = pr["head"]["ref"]
            if source_branch.startswith("docs_"):
                pr_number = pr["number"]

                # Check if this PR is for the current commit
                if source_branch == branch:
                    logger.info(
                        f"PR #{pr_number} already exists for commit {commit_slug} on branch {source_branch}"
                    )
                    continue  # Don't close the PR for the current commit

                # Only check if it's a bot PR for OTHER commits
                commits = get_pull_request_commits(full_name, pr_number, access_token)

                is_bot_pr = any(
                    commit["commit"]["author"]["name"] == BOT_NAME
                    or commit["commit"]["author"]["email"] == BOT_EMAIL
                    for commit in commits
                )
                if is_bot_pr:
                    logger.info(
                        f"Closing outdated bot PR #{pr_number} from branch {source_branch}"
                    )
                    close_pull_request(full_name, pr_number, access_token)

    except httpx.HTTPError as e:
        logger.error(f"Error checking for existing bot PRs: {e}")

    create_pull_request(full_name, branch, access_token, commit_slug)


def create_pull_request(
    full_name: str, branch: str, access_token: str, commit_slug: str
) -> None:
    """Create a pull request for the driver docs changes."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }

    # First check for existing PRs for this branch
    with httpx.Client() as client:
        # Get existing PRs
        default_branch = fetch_github_default_branch_name(full_name, access_token)
        response = client.get(
            f"https://api.github.com/repos/{full_name}/pulls",
            headers=headers,
            params={"state": "open", "head": f"{full_name.split('/')[0]}:{branch}"},
        )
        response.raise_for_status()

        # Create new PR if none exists
        logo_image = '<img src="https://raw.githubusercontent.com/driver-ai/driver-assets/main/gray_wordmark.svg" width="100px" />'
        pr_data = {
            "title": f"Update driver docs for commit {commit_slug}",
            "body": f"Automated update of driver documentation for commit {commit_slug}\n<br/>\n{logo_image}",
            "head": branch,
            "base": default_branch,
        }

        try:
            response = client.post(
                f"https://api.github.com/repos/{full_name}/pulls",
                headers=headers,
                json=pr_data,
            )
            response.raise_for_status()
            logger.info(f"Created PR: {response.json()['html_url']}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                logger.warning(
                    "No changes to create PR for - branch is up to date with main"
                )
            else:
                raise
