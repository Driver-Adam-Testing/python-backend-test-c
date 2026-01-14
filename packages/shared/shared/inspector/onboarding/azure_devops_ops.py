import base64
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


logger = logging.getLogger(__name__)

API_VERSION = "7.2-preview"


def fetch_access_token(installation_id: str) -> str:
    logger.info(f"Fetching Personal Access Token for installation ID {installation_id}")
    install_key = format_secret_name("GIT_PROVIDER_PAT_INSTALL_SECRET", installation_id)
    secrets_manager = AWSSecretManagementStrategy(
        AWSClientConfig(region_name=os.environ["AWS_REGION"])
    )
    secret_value = secrets_manager.read_secret(install_key)
    if not secret_value:
        raise AccessTokenError("Personal Access Token not found")

    if isinstance(secret_value, str):
        return secret_value

    return secret_value["token"]


def download_repo(
    base_url: str, project: str, repo_id: str, commit: str, access_token: str
) -> bytes:
    # Azure DevOps uses Basic auth with PAT as username and empty password
    auth_header = base64.b64encode(f"{access_token}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}

    # Extract organization from base_url for logging
    if base_url.startswith("https://dev.azure.com/"):
        organization = base_url.replace("https://dev.azure.com/", "").rstrip("/")
    else:
        organization = "unknown"

    # Azure DevOps archive download endpoint
    url = f"{base_url}/{project}/_apis/git/repositories/{repo_id}/items"
    params = {
        "path": "/",
        "versionDescriptor.version": commit,
        "versionDescriptor.versionType": "commit",
        "$format": "zip",
        "api-version": API_VERSION,
        "download": "true",
    }

    logger.info(
        f"Downloading Azure DevOps repository {organization}/{project}/{repo_id} at commit {commit}"
    )

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=120,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def get_default_branch(
    base_url: str, project: str, repo_id: str, access_token: str
) -> str:
    auth_header = base64.b64encode(f"{access_token}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}

    repo_url = f"{base_url}/{project}/_apis/git/repositories/{repo_id}"
    params = {"api-version": API_VERSION}

    response = requests.get(repo_url, headers=headers, params=params, timeout=120)
    response.raise_for_status()
    repo_data = response.json()

    return repo_data["defaultBranch"].replace("refs/heads/", "")


def get_latest_commit(
    base_url: str, project: str, repo_id: str, access_token: str, default_branch: str
) -> str:
    auth_header = base64.b64encode(f"{access_token}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}

    # Fetch commits from the default branch
    commits_url = f"{base_url}/{project}/_apis/git/repositories/{repo_id}/commits"
    params = {
        "searchCriteria.itemVersion.version": default_branch,
        "searchCriteria.itemVersion.versionType": "branch",
        "$top": 1,
        "api-version": API_VERSION,
    }

    response = requests.get(
        commits_url,
        headers=headers,
        params=params,
        timeout=120,
    )
    response.raise_for_status()
    commits_data = response.json()

    commits = commits_data.get("value", [])
    if commits:
        return commits[0]["commitId"]
    raise ValueError(f"No commits found on default branch '{default_branch}'")


def fetch_vcs_info(
    base_url: str,
    project: str,
    repo_id: str,
    access_token: str,
    commit_sha: str | None = None,
) -> VersionControlInfo:
    auth_header = base64.b64encode(f"{access_token}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}

    # Extract organization from base_url
    if base_url.startswith("https://dev.azure.com/"):
        organization = base_url.replace("https://dev.azure.com/", "").rstrip("/")
    else:
        raise ValueError(
            f"Invalid base_url format: {base_url} organization could not be determined"
        )

    # Fetch repository information
    repo_url = f"{base_url}/{project}/_apis/git/repositories/{repo_id}"
    repo_params = {"api-version": API_VERSION}

    repo_response = requests.get(
        repo_url,
        headers=headers,
        params=repo_params,
        timeout=120,
    )
    repo_response.raise_for_status()
    repo_data = repo_response.json()
    logger.info(
        f"Repo information retrieved from Azure DevOps API (status code {repo_response.status_code}): {repo_data}"
    )

    default_branch = repo_data["defaultBranch"].replace("refs/heads/", "")

    commit_url = (
        f"{base_url}/{project}/_apis/git/repositories/{repo_id}/commits/{commit_sha}"
    )
    commit_params = {"api-version": API_VERSION}

    commit_response = requests.get(
        commit_url,
        headers=headers,
        params=commit_params,
        timeout=120,
    )
    commit_response.raise_for_status()
    commit_data = commit_response.json()

    logger.info(
        f"Commit data retrieved from Azure DevOps API (status code {commit_response.status_code})"
    )

    author_info = AuthorInfo(
        email=commit_data["author"]["email"],
        name=commit_data["author"]["name"],
        date=commit_data["author"]["date"],
    )

    commit_info = CommitInfo(
        sha=commit_data["commitId"],
        message=commit_data["comment"],
        url=commit_data.get("remoteUrl", ""),
        author=author_info,
    )

    branch_info = BranchInfo(
        name=default_branch,
    )

    repo_info = RepoInfo(
        name=repo_data["name"],
        namespace=f"{organization}/{project}",
        full_name=f"{organization}/{project}/{repo_data['name']}",
        url=repo_data.get("webUrl", ""),
    )

    return VersionControlInfo(
        repository=repo_info,
        branch=branch_info,
        commit=commit_info,
    )


def generate_codebase_metadata(
    org_id: str,
    organization: str,
    project: str,
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
        "full_repo_name": f"{organization}/{project}/{repo_name}",  # Azure DevOps org/project structure
        "provider": provider,
        "version_id": str(version_id),
        "repository_id": str(repo_id),
        "asset_name": asset_name,
        "asset_kind": PrimaryAssetKind.CODEBASE,
        "install_id": install_id,
        "organization": organization,
        "project": project,
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

    metadata = repo.get("metadata", {})
    repo_id = repo.get("repo_id") or metadata.get("id")
    repo_name = repo.get("repo_name") or repo.get("name")
    installation_id = repo.get("installation_id")

    if not repo_id:
        logger.error(f"Missing repo_id in repo data: {repo}")
        return repo_name or "unknown"
    if not repo_name:
        logger.error(f"Missing repo_name in repo data: {repo}")
        return "unknown"
    if not installation_id:
        logger.error(f"Missing installation_id for repo {repo_name}")
        return repo_name

    try:
        with Session(engine) as session, session.begin():
            app_install = session.exec(
                select(GitProviderAppInstallation)
                .where(GitProviderAppInstallation.id == installation_id)
                .options(selectinload(GitProviderAppInstallation.git_provider_app))
            ).one()

            # Get the base_url - should be https://dev.azure.com/{organization}
            base_url = app_install.git_provider_app.base_url
            if not base_url:
                logger.error(
                    f"No base_url found in GitProviderApp for installation {installation_id}"
                )
                return repo_name

            # Extract organization from base_url
            if base_url.startswith("https://dev.azure.com/"):
                organization = base_url.replace("https://dev.azure.com/", "").rstrip(
                    "/"
                )
            else:
                logger.error(f"Invalid base_url format: {base_url}")
                return repo_name

            # Extract project from repo metadata
            project_metadata = metadata.get("project", {})
            if isinstance(project_metadata, dict):
                project = project_metadata.get("name", "")
            else:
                project = str(project_metadata) if project_metadata else ""

            if not project:
                logger.error(f"Could not determine project from metadata: {metadata}")
                return repo_name

            commit = None
            if repo.get("latest_commit"):
                if isinstance(repo["latest_commit"], dict):
                    commit = (
                        repo["latest_commit"].get("commitId")
                        or repo["latest_commit"].get("commit", {}).get("commitId")
                        or repo["latest_commit"].get(
                            "id"
                        )  # fallback for other providers
                    )
                else:
                    commit = repo["latest_commit"]

            # If no commit found in repo data, fetch the latest commit from the API
            if not commit:
                default_branch = get_default_branch(
                    base_url, project, repo_id, access_token
                )
                commit = get_latest_commit(
                    base_url, project, repo_id, access_token, default_branch
                )

            # Fetch version control information
            vcs_info = fetch_vcs_info(
                base_url=base_url,
                project=project,
                repo_id=repo_id,
                access_token=access_token,
                commit_sha=commit,
            )

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
                            return repo_name

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
                    provider=PrimaryAssetProvider.AZURE_DEVOPS_CLOUD,
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
        org_id=org_id,
        organization=organization,
        project=project,
        repo_name=repo_name,
        repo_id=repo_id,
        provider="azure_devops",
        version_id=version_id,
        asset_name=repo_name,
        install_id=installation_id,
    )

    # Download repository content
    try:
        zip_content = download_repo(
            base_url=base_url,
            project=project,
            repo_id=repo_id,
            commit=commit,
            access_token=access_token,
        )
        logger.info(
            f"Repository downloaded successfully. Size: {len(zip_content)} bytes"
        )
    except Exception as e:
        logger.exception(f"Failed to download repo {repo_name}: {e}")
        return repo_name

    try:
        org_hashed_id = hashlib.sha256(org_id.encode("utf-8")).hexdigest()[:63]
        upload_key = (
            f"assets/{org_hashed_id}/{primary_asset_id}/{version_id}/{repo_name}.zip"
        )
        upload_to_s3_with_metadata(zip_content, metadata, upload_key)
        logger.info(f"Repository {repo_name} uploaded successfully to {upload_key}.")
    except Exception as e:
        logger.exception(f"Failed to upload {repo_name} to S3: {e}")
        return repo_name

    logger.info(f"Successfully processed {repo_name}")
    return None


def get_repo_clone_info_from_id(
    base_url: str, project: str, repo_id: str, access_token: str
) -> tuple[str, str]:
    import urllib

    auth_header = base64.b64encode(f"{access_token}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}

    repo_url = f"{base_url}/{project}/_apis/git/repositories/{repo_id}"
    params = {"api-version": API_VERSION}

    response = requests.get(repo_url, headers=headers, params=params, timeout=120)
    response.raise_for_status()

    data = response.json()

    # Extract organization from base_url
    if base_url.startswith("https://dev.azure.com/"):
        organization = base_url.replace("https://dev.azure.com/", "").rstrip("/")
    else:
        raise ValueError(f"Invalid base_url format: {base_url}")

    full_name = f"{organization}/{project}/{data['name']}"

    # Azure DevOps clone URL format
    safe_org = urllib.parse.quote(organization, safe="")
    safe_project = urllib.parse.quote(project, safe="")
    safe_repo = urllib.parse.quote(data["name"], safe="")
    clone_url = f"https://{access_token}@dev.azure.com/{safe_org}/{safe_project}/_git/{safe_repo}"

    return clone_url, full_name


def list_pull_requests(
    base_url: str, project: str, repo_id: str, access_token: str, state: str = "active"
) -> list:
    auth_header = base64.b64encode(f"{access_token}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}

    url = f"{base_url}/{project}/_apis/git/repositories/{repo_id}/pullrequests"
    params = {
        "searchCriteria.status": state,
        "api-version": API_VERSION,
        "$top": 100,  # Azure DevOps default pagination
    }

    all_prs = []

    # Handle pagination
    while url:
        response = requests.get(url, headers=headers, params=params, timeout=120)
        response.raise_for_status()

        data = response.json()
        all_prs.extend(data.get("value", []))

        # Check for continuation token for next page
        if data.get("count", 0) == 100:  # If we got max results, there might be more
            params["$skip"] = len(all_prs)
        else:
            break

    return all_prs


def get_pull_request_commits(
    base_url: str, project: str, repo_id: str, pr_id: int, access_token: str
) -> list:
    auth_header = base64.b64encode(f"{access_token}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}

    url = f"{base_url}/{project}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/commits"
    params = {"api-version": API_VERSION}

    all_commits = []

    # Handle pagination
    while url:
        response = requests.get(url, headers=headers, params=params, timeout=120)
        response.raise_for_status()

        data = response.json()
        all_commits.extend(data.get("value", []))

        # Azure DevOps uses continuation token for pagination
        continuation_token = response.headers.get("x-ms-continuationtoken")
        if continuation_token:
            params["continuationToken"] = continuation_token
        else:
            break

    return all_commits


def close_pull_request(
    base_url: str, project: str, repo_id: str, pr_id: int, access_token: str
) -> None:
    auth_header = base64.b64encode(f"{access_token}:".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/json",
    }

    # First, check the PR status
    pr_url = (
        f"{base_url}/{project}/_apis/git/repositories/{repo_id}/pullrequests/{pr_id}"
    )
    pr_response = requests.get(
        pr_url, headers=headers, params={"api-version": API_VERSION}, timeout=120
    )

    if pr_response.status_code == 200:
        pr_data = pr_response.json()
        status = pr_data.get("status", "").lower()

        # Check if PR is already closed
        if status in ["completed", "abandoned"]:
            logger.info(f"Pull request #{pr_id} is already {status}")
            return

    # Update PR to abandon it
    update_data = {"status": "abandoned"}

    response = requests.patch(
        pr_url,
        headers=headers,
        params={"api-version": API_VERSION},
        data=json.dumps(update_data),
        timeout=120,
    )

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
    base_url: str,
    project: str,
    repo_id: str,
    access_token: str,
    source_branch: str,
    commit_slug: str,
) -> None:
    default_branch = get_default_branch(base_url, project, repo_id, access_token)

    auth_header = base64.b64encode(f"{access_token}:".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/json",
    }

    pr_data = {
        "sourceRefName": f"refs/heads/{source_branch}",
        "targetRefName": f"refs/heads/{default_branch}",
        "title": f"Update driver docs for commit {commit_slug}",
        "description": f"Automated update of driver documentation for commit {commit_slug}",
        "isDraft": False,
    }

    url = f"{base_url}/{project}/_apis/git/repositories/{repo_id}/pullrequests"
    params = {"api-version": API_VERSION}

    response = requests.post(
        url, headers=headers, params=params, data=json.dumps(pr_data), timeout=120
    )

    try:
        response.raise_for_status()
        pr_response = response.json()
        pr_url = pr_response.get("url", "")
        logger.info(f"Pull request created successfully: {pr_url}")
    except requests.HTTPError as e:
        if e.response.status_code == 409:
            error_detail = e.response.json()
            if "already exists" in str(error_detail).lower():
                logger.info("Pull request already exists for this branch")
            else:
                raise
        else:
            raise


def create_pull_request_with_bot_cleanup(
    base_url: str,
    project: str,
    repo_id: str,
    access_token: str,
    source_branch: str,
    commit_slug: str,
) -> None:
    BOT_NAME = "docs-bot"
    BOT_EMAIL = "bot@driverai.com"

    logger.info("Checking for existing bot pull requests...")

    existing_prs = list_pull_requests(base_url, project, repo_id, access_token)

    for pr in existing_prs:
        source_ref = pr.get("sourceRefName", "")
        source_branch_name = (
            source_ref.replace("refs/heads/", "")
            if source_ref.startswith("refs/heads/")
            else source_ref
        )

        if source_branch_name.startswith("docs_"):
            pr_id = pr["pullRequestId"]
            commits = get_pull_request_commits(
                base_url, project, repo_id, pr_id, access_token
            )

            # Check if any commit is authored by the bot
            is_bot_pr = any(
                BOT_EMAIL in commit.get("author", {}).get("email", "")
                or BOT_NAME in commit.get("author", {}).get("name", "")
                for commit in commits
            )

            if is_bot_pr:
                close_pull_request(base_url, project, repo_id, pr_id, access_token)
                logger.info(
                    f"Closed existing bot PR #{pr_id} from branch {source_branch_name}"
                )

    create_pull_request(
        base_url, project, repo_id, access_token, source_branch, commit_slug
    )
