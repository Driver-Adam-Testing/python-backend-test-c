import hashlib
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

# TODO: add fetch_github_default_branch_name
# TODO: update generate_codebase_metadata to include installation_id
# TODO: update download_and_upload_repo match gh_ops:download_and_upload_repo
# TODO: add get_repo_clone_info_from_id like in gh_ops


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
    logger.info(f"Fetching group access token for installation ID {installation_id}")
    install_key = format_secret_name("GIT_PROVIDER_GAT_INSTALL_SECRET", installation_id)
    secrets_manager = AWSSecretManagementStrategy(
        AWSClientConfig(region_name=os.environ["AWS_REGION"])
    )
    secret_value = secrets_manager.read_secret(install_key)
    if not secret_value:
        raise AccessTokenError("Access token not found")

    group_access_tokens = secret_value["token"]

    return group_access_tokens


def download_repo(base_url: str, repo_id: str, commit: str, access_token: str) -> bytes:
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(
        f"{base_url}/api/v4/projects/{repo_id}/repository/archive.zip?sha={commit}",
        headers=headers,
        timeout=120,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def fetch_vcs_info(
    base_url: str, repo_id: str, access_token: str, commit_sha: str | None = None
) -> VersionControlInfo:
    headers = {"Authorization": f"Bearer {access_token}"}

    # Fetch repository information
    repo_response = requests.get(
        f"{base_url}/api/v4/projects/{repo_id}",
        headers=headers,
        timeout=120,
    )
    repo_response.raise_for_status()
    repo_data = repo_response.json()
    logger.info(
        f"Repo information retrieved from GitLab API (status code {repo_response.status_code}): {repo_data}"
    )

    default_branch = repo_data["default_branch"]

    # Fetch detailed commit information
    commit_response = requests.get(
        f"{base_url}/api/v4/projects/{repo_id}/repository/commits/{commit_sha}",
        headers=headers,
        timeout=120,
    )
    commit_response.raise_for_status()
    commit_data = commit_response.json()
    logger.info(
        f"Commit data retrieved from GitLab API (status code {commit_response.status_code}): {commit_data}"
    )

    # Build VersionControlInfo
    author_info = AuthorInfo(
        email=commit_data["author_email"],
        name=commit_data["author_name"],
        date=commit_data["authored_date"],
    )

    commit_info = CommitInfo(
        sha=commit_data["id"],
        message=commit_data["message"],
        url=commit_data["web_url"],
        author=author_info,
    )

    branch_info = BranchInfo(name=default_branch)

    repo_info = RepoInfo(
        name=repo_data["name"],
        namespace=repo_data["namespace"]["name"],
        full_name=repo_data["name_with_namespace"],
        url=repo_data["web_url"],
    )

    return VersionControlInfo(
        repository=repo_info,
        commit=commit_info,
        branch=branch_info,
    )


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


def download_and_upload_repo(
    org_id: str, repo: dict, access_token: str, is_push: bool = False
) -> str | None:
    from database.db import engine
    from database.models import (
        GitProviderAppInstallation,
        PrimaryAsset,
        Version,
    )
    from database.models_enums import (
        PrimaryAssetKind,
        PrimaryAssetProvider,
        VersionStatus,
    )
    from sqlalchemy.exc import IntegrityError

    repo_id = repo["metadata"]["id"]
    repo_name = repo["repo_name"]
    commit = repo["latest_commit"]["commit"]["id"]
    installation_id = repo["installation_id"]

    try:
        with Session(engine) as session, session.begin():
            app_install = session.exec(
                select(GitProviderAppInstallation).where(
                    GitProviderAppInstallation.id == installation_id
                )
            ).one()
            base_url = app_install.git_provider_app.base_url

            vcs_info = fetch_vcs_info(
                base_url=base_url,
                repo_id=repo_id,
                access_token=access_token,
                commit_sha=commit,
            )

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
                                f"Generation already in progress for {repo.get('repo_name', 'unknown')}. "
                                f"Ignoring push event to allow current generation to complete."
                            )
                            return repo
                elif primary_asset.versions[0].status == VersionStatus.CONNECTING:
                    logger.info(
                        f"Version already in connecting state for {repo_name}, skipping..."
                    )
                    return repo
                else:
                    # TODO: any other cases to handle explicitly?
                    return repo

            else:
                primary_asset = PrimaryAsset(
                    display_name=repo_name,
                    organization_id=org_id,
                    kind=PrimaryAssetKind.CODEBASE,
                    repository_id=repo_id,
                    installation_id=installation_id,
                    codebase_settings_auto_commit_docs=False,
                    provider=PrimaryAssetProvider.GITLAB_SELF_MANAGED,
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

                _create_git_provider_grants(session, primary_asset_id, org_id)
                version_id = version.id
                logger.info(
                    f"Creating primary asset and version for {repo_name}:{commit} for org: {org_id}. Version ID: {version_id}"
                )
    except IntegrityError:
        logger.error(
            f"Failed to create primary asset and version {repo_name}:{commit} for org: {org_id}"
        )
        return repo
    full_repo_name = repo["metadata"]["path_with_namespace"]
    # TODO: update this with additional metadata
    metadata = generate_codebase_metadata(
        org_id,
        full_repo_name,
        repo_id,
        "gitlab_enterprise_self_managed",
        version_id,
        repo_name,
        installation_id,
    )

    zip_content = download_repo(base_url, repo_id, commit, access_token)
    logger.info(f"Repository downloaded successfully. Size: {len(zip_content)} bytes")

    org_hashed_id = hashlib.sha256(org_id.encode("utf-8")).hexdigest()[:63]
    upload_key = (
        f"assets/{org_hashed_id}/{primary_asset_id}/{version_id}/{repo_name}.zip"
    )
    upload_to_s3_with_metadata(zip_content, metadata, upload_key)
    logger.info(f"Repository {repo_name} uploaded successfully to {upload_key}.")

    return None


def fetch_gitlab_default_branch_name(
    base_url: str, repo_id: str, access_token: str
) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(
        f"{base_url}/api/v4/projects/{repo_id}",
        headers=headers,
        timeout=120,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.json()["default_branch"]


def get_repo_clone_info_from_id(
    base_url: str, repo_id: str, access_token: str
) -> tuple[str, str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(
        f"{base_url}/api/v4/projects/{repo_id}",
        headers=headers,
        timeout=120,
        allow_redirects=True,
    )
    response.raise_for_status()
    clone_url = response.json()["http_url_to_repo"]
    full_name = response.json()["path_with_namespace"]
    username = get_gitlab_username(base_url, access_token)
    if clone_url and username:
        clone_url = (
            clone_url.replace("https://", f"https://{username}:{access_token}@")
            if clone_url.startswith("https")
            else clone_url.replace("http://", f"https://{username}:{access_token}@")
        )
    else:
        raise ValueError("Clone URL or username is missing")
    return clone_url, full_name


def create_pull_request(
    base_url: str,
    repo_id: str,
    access_token: str,
    branch: str,
    commit_slug: str,
) -> None:
    default_branch = fetch_gitlab_default_branch_name(base_url, repo_id, access_token)
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.post(
        f"{base_url}/api/v4/projects/{repo_id}/merge_requests",
        headers=headers,
        json={
            "source_branch": branch,
            "target_branch": default_branch,
            "title": f"Update driver docs for commit {commit_slug}",
        },
    )
    response.raise_for_status()
    logger.info(f"Pull request created successfully: {response.json()['web_url']}")


def get_gitlab_username(base_url: str, access_token: str) -> str:
    url = f"{base_url.rstrip('/')}/api/v4/user"
    headers = {"PRIVATE-TOKEN": access_token}
    logger.debug(f"Fetching GitLab username from {url}")
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    logger.debug(f"GitLab user response: {response.json()}")
    return response.json()["username"]


def list_merge_requests(
    base_url: str, repo_id: str, access_token: str, state: str = "opened"
) -> list:
    headers = {"Authorization": f"Bearer {access_token}"}
    # GitLab supports pagination; fetch all pages
    url = f"{base_url}/api/v4/projects/{repo_id}/merge_requests"
    params = {"state": state, "per_page": 100, "order_by": "updated_at", "sort": "desc"}
    all_mrs: list = []
    while url:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        all_mrs.extend(response.json())
        # Pagination: look for next page via Link header
        link = response.headers.get("Link")
        next_url = None
        if link:
            parts = [p.strip() for p in link.split(",")]
            for part in parts:
                if 'rel="next"' in part:
                    start = part.find("<")
                    end = part.find(">", start + 1)
                    if start != -1 and end != -1:
                        next_url = part[start + 1 : end]
                        break
        url = next_url
        params = {}  # clear params when using absolute next_url
    return all_mrs


def get_merge_request_commits(
    base_url: str, repo_id: str, mr_iid: int, access_token: str
) -> list:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{base_url}/api/v4/projects/{repo_id}/merge_requests/{mr_iid}/commits"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def close_merge_request(
    base_url: str, repo_id: str, mr_iid: int, access_token: str
) -> None:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{base_url}/api/v4/projects/{repo_id}/merge_requests/{mr_iid}"
    response = requests.put(url, headers=headers, json={"state_event": "close"})
    response.raise_for_status()
    logger.info(f"Closed merge request !{mr_iid}")


def create_pull_request_with_bot_cleanup(
    base_url: str,
    repo_id: str,
    access_token: str,
    branch: str,
    commit_slug: str,
) -> None:
    BOT_NAME = "docs-bot"
    BOT_EMAIL = "bot@driverai.com"
    try:
        existing_mrs = list_merge_requests(
            base_url, repo_id, access_token, state="opened"
        )
        for mr in existing_mrs:
            source_branch = mr.get("source_branch", "")
            if source_branch.startswith("docs_"):
                mr_iid = mr.get("iid")  # GitLab uses IID per project

                commits = get_merge_request_commits(
                    base_url, repo_id, mr_iid, access_token
                )
                is_bot_mr = any(
                    (commit.get("author_email") == BOT_EMAIL)
                    or (commit.get("author_name") == BOT_NAME)
                    for commit in commits
                )
                if is_bot_mr:
                    logger.info(f"Closing merge request !{mr_iid}")
                    close_merge_request(base_url, repo_id, mr_iid, access_token)
    except requests.HTTPError as e:
        logger.error(f"Error checking for existing bot PRs: {e}")
    create_pull_request(base_url, repo_id, access_token, branch, commit_slug)
