import hashlib
import logging
import os
import re
import subprocess
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from pathlib import Path

from database.models_enums import (
    ContentKind,
    NodeKind,
    VersionStatus,
)
from shared.analytics_cleanup import cleanup_analytics_for_codebase
from workflows.inspector_workflow import InspectorInput, inspector_task

logger = logging.getLogger(__name__)


def _cleanup_analytics_before_delete(org_id: str, codebase_id: str) -> None:
    """Clean up analytics data before deleting a codebase.

    Args:
        org_id: Organization UUID string
        codebase_id: Codebase UUID string
    """
    try:
        # Get AWS config from environment (set by Modal/worker)
        aws_region = os.environ.get("AWS_REGION", "us-east-1")

        cleanup_analytics_for_codebase(
            organization_id=org_id,
            codebase_id=codebase_id,
            aws_region=aws_region,
        )
        print(f"Analytics cleanup complete for codebase {codebase_id}")
    except Exception as e:
        # Log but don't fail the deletion
        print(f"Warning: Analytics cleanup failed for {codebase_id}: {e}")


def _trigger_incremental_analytics_for_push(
    org_id: str,
    repo: dict,
    install_id: str | None,
    provider: str,
) -> None:
    """Trigger incremental analytics after a push event.

    Looks up the codebase_id from the database and spawns an incremental
    analytics task. Silently fails if analytics cannot be triggered.

    Args:
        org_id: Organization UUID
        repo: Repository dict with 'id' (repository_id), 'name', and 'full_name'
        install_id: Provider app installation ID
        provider: Git provider name
    """
    from database.db import engine
    from database.models import PrimaryAsset
    from onboarding.analytics_trigger import spawn_analytics_task
    from sqlmodel import Session, select

    repository_id = str(repo.get("id", ""))
    full_name = repo.get("full_name", repo.get("name", ""))

    if not repository_id:
        logger.warning(f"Cannot trigger analytics: no repository_id for {full_name}")
        return

    try:
        with Session(engine) as session:
            # Look up codebase_id from PrimaryAsset
            primary_asset = session.exec(
                select(PrimaryAsset).where(
                    PrimaryAsset.repository_id == repository_id,
                    PrimaryAsset.organization_id == org_id,
                )
            ).first()

            if not primary_asset:
                logger.warning(
                    f"Cannot trigger analytics: no PrimaryAsset for repo {full_name}"
                )
                return

            codebase_id = str(primary_asset.id)

        # Spawn incremental analytics task
        spawn_analytics_task(
            codebase_id=codebase_id,
            organization_id=org_id,
            codebase_name=full_name,
            provider=provider,
            install_id=install_id,
            incremental=True,
        )
        logger.info(
            f"Triggered incremental analytics for {full_name} (codebase={codebase_id})"
        )

    except Exception as e:
        logger.error(f"Failed to trigger analytics for {full_name}: {e}")
        # Don't raise - analytics failure should not block push processing


def collect_file_paths(extracted_path: Path) -> tuple[list[Path], list[Path]]:
    """Collect all files under extracted_path."""
    file_list = os.listdir(extracted_path)
    if ".driverignore" in file_list:
        cmd = [
            "rg",
            "--files",
            "--hidden",
            "--ignore-file=.driverignore",
            "--no-ignore-parent",
            "--no-ignore-vcs",
        ]
    else:
        cmd = ["rg", "--files", "--hidden", "--no-ignore-parent", "--no-ignore-vcs"]
    try:
        result = subprocess.run(
            cmd,
            cwd=extracted_path,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"A subprocess error occurred: {e.stderr}")
        raise
    output = result.stdout
    all_files = [
        extracted_path / Path(file_path.strip()) for file_path in output.splitlines()
    ]
    all_directories = set()
    all_directories.add(extracted_path)
    for file_path in all_files:
        parents = file_path.relative_to(extracted_path).parents
        for parent in parents:
            all_directories.add(extracted_path / parent)
    all_directories = list(all_directories)
    return all_files, all_directories


def collect_ignored_file_paths(extracted_path: Path) -> list[Path]:
    """Return list of files ignored by .driverignore under extracted_path."""
    file_list = os.listdir(extracted_path)
    if ".driverignore" not in file_list:
        return []  # no ignored files if no ignore file

    def run_rg(cmd: list[str]) -> set[Path]:
        try:
            result = subprocess.run(
                cmd,
                cwd=extracted_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return {Path(p.strip()) for p in result.stdout.splitlines()}
        except subprocess.CalledProcessError as e:
            print(f"Error running rg: {e.stderr}")
            raise

    # All files without any ignore
    all_files = run_rg(["rg", "--files", "--hidden", "--no-ignore"])

    # Files *not* ignored by .driverignore
    unignored_files = run_rg(
        [
            "rg",
            "--files",
            "--hidden",
            "--ignore-file=.driverignore",
            "--no-ignore-parent",
            "--no-ignore-vcs",
        ]
    )

    ignored_files = all_files - unignored_files
    return [extracted_path / p for p in ignored_files]


def process_file(local_path_and_extracted_path: tuple[Path, Path]) -> tuple[Path, dict]:
    from shared.inspector.onboarding.onboard_utils import run_file_stats_and_reencode

    """Wrapper for multiprocessing, unpacks arguments."""
    local_path, extracted_path = local_path_and_extracted_path
    return local_path, run_file_stats_and_reencode(local_path, extracted_path)


def handle_github_events(
    installation_id: str | None,
    org_id: str,
    repos_added: list[dict],
    repos_deleted: list[dict],
    repos_pushed: list[dict],
) -> None:
    from database.db import (
        engine,  # We defer the import since we'll have the secrets set here
    )
    from database.models import (
        GithubAppInstallation,  # noqa: F401
        PrimaryAsset,
    )
    from shared.inspector.onboarding.gh_ops import (
        download_and_upload_repo,
        fetch_app_access_token,
    )
    from shared.inspector.onboarding.onboard_utils import AccessTokenError
    from sqlalchemy.orm import selectinload
    from sqlmodel import Session, select

    if installation_id is None and (repos_added or repos_pushed):
        raise ValueError(
            "Installation ID is required for added or pushed repos. It only can be null for delete-only events"
        )

    if repos_deleted:
        with Session(engine) as session, session.begin():
            for repo in repos_deleted:
                primary_asset = session.exec(
                    select(PrimaryAsset)
                    .where(
                        PrimaryAsset.repository_id == str(repo["id"]),
                        PrimaryAsset.organization_id == org_id,
                    )
                    .options(selectinload(PrimaryAsset.versions))
                ).first()

                if not primary_asset:
                    print(f"Primary asset for repo {repo['name']} not found.")
                    continue

                if all(
                    v.status
                    in {
                        VersionStatus.CONNECTED,
                        VersionStatus.CONNECTING,
                        VersionStatus.CONNECTION_FAILED,
                    }
                    for v in primary_asset.versions
                ):
                    # Clean up analytics before deleting the asset
                    _cleanup_analytics_before_delete(org_id, str(primary_asset.id))
                    print(
                        f"Deleting primary asset {primary_asset.id} for repo {repo['name']}"
                    )
                    session.delete(primary_asset)
                else:
                    print(
                        f"Primary asset {primary_asset.id} for repo {repo['name']} has versions with tech docs. Not deleting."
                    )

    if not repos_added and not repos_pushed:
        return

    # Fetch token for additions/updates
    try:
        token = fetch_app_access_token(installation_id=installation_id)
    except AccessTokenError:
        print(f"GitHub installation {installation_id} not found.")
        raise

    errant_repos = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(
                download_and_upload_repo,
                org_id,
                repo,
                token,
                installation_id,
            )
            for repo in repos_added
        ]
        wait(futures)
        for f in futures:
            if f.result():
                errant_repos.append(f.result())

    for repo in repos_pushed:
        repo_name_or_none = download_and_upload_repo(
            org_id=org_id,
            repo=repo,
            access_token=token,
            install_id=installation_id,
            is_push=True,
        )
        if repo_name_or_none is not None:
            errant_repos.append(repo_name_or_none)
        else:
            # Push succeeded - trigger incremental analytics
            _trigger_incremental_analytics_for_push(
                org_id=org_id,
                repo=repo,
                install_id=installation_id,
                provider="github",
            )


def handle_gitlab_events(
    installation_id: str | None,
    org_id: str,
    repos_added: list[dict],
    repos_deleted: list[dict],
    repos_pushed: list[dict],
) -> None:
    from database.db import (
        engine,  # We defer the import since we'll have the secrets set here
    )
    from database.models import (
        GithubAppInstallation,  # noqa: F401
        PrimaryAsset,
    )
    from shared.inspector.onboarding import gitlab_ops
    from shared.inspector.onboarding.onboard_utils import AccessTokenError
    from sqlalchemy.orm import selectinload
    from sqlmodel import Session, select

    if installation_id is None and (repos_added or repos_pushed):
        raise ValueError(
            "Installation ID is required for added or pushed repos. It only can be null for delete-only events"
        )

    if repos_deleted:
        with Session(engine) as session, session.begin():
            for repo in repos_deleted:
                primary_asset = session.exec(
                    select(PrimaryAsset)
                    .where(
                        PrimaryAsset.repository_id == str(repo["id"]),
                        PrimaryAsset.organization_id == org_id,
                    )
                    .options(selectinload(PrimaryAsset.versions))
                ).first()

                if not primary_asset:
                    print(f"Primary asset for repo {repo['name']} not found.")
                    continue

                if all(
                    v.status
                    in {
                        VersionStatus.CONNECTED,
                        VersionStatus.CONNECTING,
                        VersionStatus.CONNECTION_FAILED,
                    }
                    for v in primary_asset.versions
                ):
                    # Clean up analytics before deleting the asset
                    _cleanup_analytics_before_delete(org_id, str(primary_asset.id))
                    print(
                        f"Deleting primary asset {primary_asset.id} for repo {repo['name']}"
                    )
                    session.delete(primary_asset)
                else:
                    print(
                        f"Primary asset {primary_asset.id} for repo {repo['name']} has versions with tech docs. Not deleting."
                    )

    if not repos_added and not repos_pushed:
        return

    # Fetch token for additions/updates
    try:
        token = gitlab_ops.fetch_access_token(installation_id=installation_id)
    except AccessTokenError:
        print(f"GitLab installation {installation_id} not found.")
        raise

    errant_repos = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(gitlab_ops.download_and_upload_repo, org_id, repo, token)
            for repo in repos_added
        ]
        wait(futures)
        for f in futures:
            if f.result():
                errant_repos.append(f.result())

    for repo in repos_pushed:
        repo_name_or_none = gitlab_ops.download_and_upload_repo(
            org_id=org_id,
            repo=repo,
            access_token=token,
            is_push=True,
        )
        if repo_name_or_none is not None:
            errant_repos.append(repo_name_or_none)
        else:
            # Push succeeded - trigger incremental analytics
            _trigger_incremental_analytics_for_push(
                org_id=org_id,
                repo=repo,
                install_id=installation_id,
                provider="gitlab",
            )


def handle_bitbucket_events(
    installation_id: str | None,
    org_id: str,
    repos_added: list[dict],
    repos_deleted: list[dict],
    repos_pushed: list[dict],
) -> None:
    from database.db import (
        engine,
    )
    from database.models import (
        GithubAppInstallation,  # noqa: F401
        PrimaryAsset,
    )
    from shared.inspector.onboarding import bitbucket_ops
    from shared.inspector.onboarding.onboard_utils import AccessTokenError
    from sqlalchemy.orm import selectinload
    from sqlmodel import Session, select

    if installation_id is None and (repos_added or repos_pushed):
        raise ValueError(
            "Installation ID is required for added or pushed repos. It only can be null for delete-only events"
        )

    if repos_deleted:
        with Session(engine) as session, session.begin():
            for repo in repos_deleted:
                primary_asset = session.exec(
                    select(PrimaryAsset)
                    .where(
                        PrimaryAsset.repository_id == str(repo["id"]),
                        PrimaryAsset.organization_id == org_id,
                    )
                    .options(selectinload(PrimaryAsset.versions))
                ).first()

                if not primary_asset:
                    print(f"Primary asset for repo {repo['name']} not found.")
                    continue

                if all(
                    v.status
                    in {
                        VersionStatus.CONNECTED,
                        VersionStatus.CONNECTING,
                        VersionStatus.CONNECTION_FAILED,
                    }
                    for v in primary_asset.versions
                ):
                    # Clean up analytics before deleting the asset
                    _cleanup_analytics_before_delete(org_id, str(primary_asset.id))
                    print(
                        f"Deleting primary asset {primary_asset.id} for repo {repo['name']}"
                    )
                    session.delete(primary_asset)
                else:
                    print(
                        f"Primary asset {primary_asset.id} for repo {repo['name']} has versions with tech docs. Not deleting."
                    )

    if not repos_added and not repos_pushed:
        return

    # Fetch token for additions/updates
    try:
        token = bitbucket_ops.fetch_access_token(installation_id=installation_id)
    except AccessTokenError:
        print(f"Bitbucket installation {installation_id} not found.")
        raise

    errant_repos = []
    # Add installation_id to each repo dict if not present
    for repo in repos_added:
        if "installation_id" not in repo:
            repo["installation_id"] = installation_id
    # Using 2 workers to stay within BitBucket's rate limits
    # Testing showed this provides optimal throughput without hitting limits
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(bitbucket_ops.download_and_upload_repo, org_id, repo, token)
            for repo in repos_added
        ]
        wait(futures)
        for f in futures:
            if f.result():
                errant_repos.append(f.result())

    for repo in repos_pushed:
        # Add installation_id to repo dict if not present
        if "installation_id" not in repo:
            repo["installation_id"] = installation_id

        repo_name_or_none = bitbucket_ops.download_and_upload_repo(
            org_id=org_id,
            repo=repo,
            access_token=token,
            is_push=True,
        )
        if repo_name_or_none is not None:
            errant_repos.append(repo_name_or_none)
        else:
            # Push succeeded - trigger incremental analytics
            _trigger_incremental_analytics_for_push(
                org_id=org_id,
                repo=repo,
                install_id=installation_id,
                provider="bitbucket",
            )


def handle_azure_devops_events(
    installation_id: str | None,
    org_id: str,
    repos_added: list[dict],
    repos_deleted: list[dict],
    repos_pushed: list[dict],
) -> None:
    from database.db import (
        engine,
    )
    from database.models import (
        GithubAppInstallation,  # noqa: F401
        PrimaryAsset,
    )
    from shared.inspector.onboarding import azure_devops_ops
    from shared.inspector.onboarding.onboard_utils import AccessTokenError
    from sqlalchemy.orm import selectinload
    from sqlmodel import Session, select

    if installation_id is None and (repos_added or repos_pushed):
        raise ValueError(
            "Installation ID is required for added or pushed repos. It only can be null for delete-only events"
        )

    if repos_deleted:
        with Session(engine) as session, session.begin():
            for repo in repos_deleted:
                primary_asset = session.exec(
                    select(PrimaryAsset)
                    .where(
                        PrimaryAsset.repository_id == str(repo["id"]),
                        PrimaryAsset.organization_id == org_id,
                    )
                    .options(selectinload(PrimaryAsset.versions))
                ).first()

                if not primary_asset:
                    print(f"Primary asset for repo {repo['name']} not found.")
                    continue

                if all(
                    v.status
                    in {
                        VersionStatus.CONNECTED,
                        VersionStatus.CONNECTING,
                        VersionStatus.CONNECTION_FAILED,
                    }
                    for v in primary_asset.versions
                ):
                    # Clean up analytics before deleting the asset
                    _cleanup_analytics_before_delete(org_id, str(primary_asset.id))
                    print(
                        f"Deleting primary asset {primary_asset.id} for repo {repo['name']}"
                    )
                    session.delete(primary_asset)
                else:
                    print(
                        f"Primary asset {primary_asset.id} for repo {repo['name']} has versions with tech docs. Not deleting."
                    )

    if not repos_added and not repos_pushed:
        return

    # Fetch token for additions/updates
    try:
        token = azure_devops_ops.fetch_access_token(installation_id=installation_id)
    except AccessTokenError:
        print(f"Azure DevOps installation {installation_id} not found.")
        raise

    errant_repos = []
    # Add installation_id to each repo dict if not present
    for repo in repos_added:
        print(repo)
        if "installation_id" not in repo:
            repo["installation_id"] = installation_id

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(
                azure_devops_ops.download_and_upload_repo, org_id, repo, token
            )
            for repo in repos_added
        ]
        wait(futures)
        for f in futures:
            if f.result():
                errant_repos.append(f.result())

    for repo in repos_pushed:
        # Add installation_id to repo dict if not present
        if "installation_id" not in repo:
            repo["installation_id"] = installation_id

        repo_name_or_none = azure_devops_ops.download_and_upload_repo(
            org_id=org_id,
            repo=repo,
            access_token=token,
            is_push=True,
        )
        if repo_name_or_none is not None:
            errant_repos.append(repo_name_or_none)
        else:
            # Push succeeded - trigger incremental analytics
            _trigger_incremental_analytics_for_push(
                org_id=org_id,
                repo=repo,
                install_id=installation_id,
                provider="azure_devops",
            )


def handle_bitbucket_dc_events(
    installation_id: str | None,
    org_id: str,
    repos_added: list[dict],
    repos_deleted: list[dict],
    repos_pushed: list[dict],
) -> None:
    from database.db import (
        engine,
    )
    from database.models import (
        GithubAppInstallation,  # noqa: F401
        PrimaryAsset,
    )
    from shared.inspector.onboarding import bitbucket_dc_ops
    from shared.inspector.onboarding.onboard_utils import AccessTokenError
    from sqlalchemy.orm import selectinload
    from sqlmodel import Session, select

    if installation_id is None and (repos_added or repos_pushed):
        raise ValueError(
            "Installation ID is required for added or pushed repos. It only can be null for delete-only events"
        )

    if repos_deleted:
        with Session(engine) as session, session.begin():
            for repo in repos_deleted:
                primary_asset = session.exec(
                    select(PrimaryAsset)
                    .where(
                        PrimaryAsset.repository_id == str(repo["id"]),
                        PrimaryAsset.organization_id == org_id,
                    )
                    .options(selectinload(PrimaryAsset.versions))
                ).first()

                if not primary_asset:
                    print(f"Primary asset for repo {repo['name']} not found.")
                    continue

                if all(
                    v.status
                    in {
                        VersionStatus.CONNECTED,
                        VersionStatus.CONNECTING,
                        VersionStatus.CONNECTION_FAILED,
                    }
                    for v in primary_asset.versions
                ):
                    print(
                        f"Deleting primary asset {primary_asset.id} for repo {repo['name']}"
                    )
                    session.delete(primary_asset)
                else:
                    print(
                        f"Primary asset {primary_asset.id} for repo {repo['name']} has versions with tech docs. Not deleting."
                    )

    if not repos_added and not repos_pushed:
        return

    # Fetch token for additions/updates
    try:
        token, instance_url = bitbucket_dc_ops.fetch_access_token(
            installation_id=installation_id
        )
    except AccessTokenError:
        print(f"Bitbucket DC installation {installation_id} not found.")
        raise

    errant_repos = []
    # Add installation_id to each repo dict if not present
    for repo in repos_added:
        print(repo)
        if "installation_id" not in repo:
            repo["installation_id"] = installation_id

    # Using 2 workers to be conservative with Bitbucket DC rate limits
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                bitbucket_dc_ops.download_and_upload_repo, org_id, repo, token
            )
            for repo in repos_added
        ]
        wait(futures)
        for f in futures:
            if f.result():
                errant_repos.append(f.result())

    for repo in repos_pushed:
        # Add installation_id to repo dict if not present
        if "installation_id" not in repo:
            repo["installation_id"] = installation_id

        repo_name_or_none = bitbucket_dc_ops.download_and_upload_repo(
            org_id=org_id,
            repo=repo,
            access_token=token,
            is_push=True,
        )
        if repo_name_or_none is not None:
            errant_repos.append(repo_name_or_none)


def connect_repos_for_installation(github_installation_id: str) -> None:
    import requests
    from database.db import engine
    from database.models import GithubAppInstallation
    from shared.inspector.onboarding.gh_ops import (
        AccessTokenError,
        fetch_app_access_token,
    )
    from sqlmodel import Session, select

    with Session(engine) as session:
        install = session.exec(
            select(GithubAppInstallation).where(
                GithubAppInstallation.github_app_installation_id
                == github_installation_id
            )
        ).one()
        print(f"Adding repos for installation {install.github_app_installation_id}")

        gh_install_id = install.github_app_installation_id
        try:
            token = fetch_app_access_token(gh_install_id)
        except AccessTokenError:
            print("Github installation not found. ")
            raise

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = requests.get(
            "https://api.github.com/installation/repositories", headers=headers
        )
        response.raise_for_status()

        repos = response.json()["repositories"]

        repos_added = []
        for repo in repos:
            repos_added.append(
                {
                    "id": repo["id"],
                    "name": repo["name"],
                    "full_name": repo["full_name"],
                }
            )
        page_count = 1
        max_pages = 100
        link_header: str = response.headers.get("link")
        while link_header:
            page_count += 1
            if page_count > max_pages:
                print(
                    "Max repository pages reached for Github integration, proceeding with just the first {max_pages} pages."
                )
                break
            parts = response.headers["link"].split(",")
            matches = [
                re.search(r'<([^>]+)>; rel="([^"]+)"', part.strip()) for part in parts
            ]
            has_next = False
            for match in matches:
                next_url, rel = match.groups()
                if rel == "next" and next_url:
                    has_next = True
                    response = requests.get(next_url, headers=headers)
                    response.raise_for_status()
                    current_repos = response.json()["repositories"]
                    for repo in current_repos:
                        repos_added.append(
                            {
                                "id": repo["id"],
                                "name": repo["name"],
                                "full_name": repo["full_name"],
                            }
                        )
            if not has_next:
                break

        handle_github_events(
            gh_install_id,
            install.organization_id,
            repos_added,
            [],
            [],
        )

        print(
            f"Processed for installation {gh_install_id}. Connecting ({len(repos_added)}) repos."
        )
        for repo in repos:
            print(f"=> Repo: {repo['full_name']}")


def connect_unconnected_repos() -> None:
    """This is a migration script to connect unconnected repos

    It will probably only be run once and can likely be deleted by the time you read this :)
    """
    import requests
    from database.db import engine
    from database.models import GithubAppInstallation
    from shared.inspector.onboarding.gh_ops import (
        AccessTokenError,
        fetch_app_access_token,
    )
    from sqlmodel import Session, select

    with Session(engine) as session:
        gh_app_installs = session.exec(select(GithubAppInstallation)).all()
        for install in gh_app_installs:
            print(f"Processing installation {install.github_app_installation_id}")
            gh_install_id = install.github_app_installation_id
            try:
                token = fetch_app_access_token(gh_install_id)
            except AccessTokenError:
                print("Github installation not found. Assuming uninstalled.")
                continue

            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            }
            response = requests.get(
                "https://api.github.com/installation/repositories", headers=headers
            )
            response.raise_for_status()

            repos = response.json()["repositories"]

            repos_added = []
            for repo in repos:
                repos_added.append(
                    {
                        "id": repo["id"],
                        "name": repo["name"],
                        "full_name": repo["full_name"],
                    }
                )
            page_count = 1
            max_pages = 100
            link_header: str = response.headers.get("link")
            while link_header:
                page_count += 1
                if page_count > max_pages:
                    print(
                        "Max repository pages reached for Github integration, proceeding with just the first {max_pages} pages."
                    )
                    break
                parts = response.headers["link"].split(",")
                matches = [
                    re.search(r'<([^>]+)>; rel="([^"]+)"', part.strip())
                    for part in parts
                ]
                has_next = False
                for match in matches:
                    next_url, rel = match.groups()
                    if rel == "next" and next_url:
                        has_next = True
                        response = requests.get(next_url, headers=headers)
                        response.raise_for_status()
                        current_repos = response.json()["repositories"]
                        for repo in current_repos:
                            repos_added.append(
                                {
                                    "id": repo["id"],
                                    "name": repo["name"],
                                    "full_name": repo["full_name"],
                                }
                            )
                if not has_next:
                    break

            handle_github_events(
                gh_install_id,
                install.organization_id,
                repos_added,
                [],
                [],
            )

            print(
                f"Processed for installation {gh_install_id}. Connecting ({len(repos_added)}) repos."
            )
            for repo in repos:
                print(f"=> Repo: {repo['full_name']}")


# Analytics trigger is implemented in analytics_trigger.py
# We import the spawn function here for convenience
def _spawn_analytics_task(
    codebase_id: str,
    organization_id: str,
    codebase_name: str,
    provider: str,
    install_id: str | None = None,
) -> str | None:
    """Spawn analytics task as background Hatchet job. Wrapper around analytics_trigger module."""
    from onboarding.analytics_trigger import spawn_analytics_task

    return spawn_analytics_task(
        codebase_id=codebase_id,
        organization_id=organization_id,
        codebase_name=codebase_name,
        provider=provider,
        install_id=install_id,
    )


def run_codebase_connection(
    presigned_url: str,
    provisional_codebase_name: str,
    org_id: str,  # Not strictly necessary, but we can check that the version belongs to the org.
    version_id: str,
    provider: str = "manual",
) -> None:
    import tempfile
    import time

    from boto3 import client, resource
    from database.db import (
        engine,  # We defer the import since we'll have the secrets set here
    )
    from database.models import (
        DerivedContent,
        GitProviderKind,
        Node,
        PrimaryAsset,
        Version,
        VersionNode,
    )
    from database.models_enums import VersionStatus
    from shared.inspector.onboarding.onboard_utils import (
        calculate_directory_stats,
        create_bucket_if_dne,
        download_file_from_presigned_url,
        parse_presigned_url,
        unpack_archive_to_finalized_path,
    )
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session, select, update

    download_dest = Path(provisional_codebase_name)
    download_file_from_presigned_url(presigned_url, download_dest)

    print(f"Downloaded {provisional_codebase_name} from S3")

    if provider == "github":
        override_codebase_name = provisional_codebase_name
    elif provider == GitProviderKind.GITLAB_ENTERPRISE_SELF_MANAGED.value.lower():
        # TODO: is this actually needed? Does self managed behave differently than enterprise?
        override_codebase_name = re.sub(
            r"-[a-fA-F0-9]{40}-[a-fA-F0-9]{40}", "", provisional_codebase_name
        )
    else:
        override_codebase_name = None

    with tempfile.TemporaryDirectory() as temp_dir:
        # Override so unpack from github doesn't have hash in name.
        extracted_path = unpack_archive_to_finalized_path(
            archive_path=download_dest,
            extraction_root=Path(temp_dir),
            override_codebase_name=override_codebase_name,
        )
        codebase_name = str(extracted_path.relative_to(temp_dir))
        print("Codebase name: ", codebase_name)
        print("Unpacked archive to: ", extracted_path)

        try:
            with Session(engine) as session, session.begin():
                primary_asset = session.exec(
                    select(PrimaryAsset)
                    .join(Version)
                    .where(
                        Version.id == version_id,
                        PrimaryAsset.organization_id == org_id,
                    )
                ).one()
                primary_asset.display_name = codebase_name
                primary_asset_id = primary_asset.id
                session.add(primary_asset)
        except IntegrityError:
            # TODO: send email
            print(
                f"Primary asset with name {codebase_name} already exists for org {org_id}. Setting status to connection failed."
            )
            with Session(engine) as session, session.begin():
                update_stmt = (
                    update(Version)
                    .where(Version.id == version_id)
                    .values(status=VersionStatus.CONNECTION_FAILED)
                )
                session.exec(update_stmt)
            return None

        org_id_bucket = hashlib.sha256(org_id.encode()).hexdigest()[:63]
        create_bucket_if_dne(org_id_bucket)

        version_fragment = f"{primary_asset_id}/{version_id}"
        s3_dest = Path(version_fragment) / f"{version_id}_source.zip"

        all_directories = []
        codebase_stats = {}
        analyzable_bytes = 0

        all_files, all_directories = collect_file_paths(extracted_path)
        ignored_files = collect_ignored_file_paths(extracted_path)
        tasks = [(file_path, extracted_path) for file_path in all_files]
        folder_results = []
        with ProcessPoolExecutor(max_workers=28) as executor:
            futures = {executor.submit(process_file, task): task[0] for task in tasks}
            for idx, future in enumerate(as_completed(futures)):
                path, file_stats = future.result()
                codebase_stats[path] = file_stats
                if (
                    file_stats["is_analyzable"]
                    and not file_stats["is_blacklisted"]
                    and not file_stats.get("is_ignored", False)
                ):
                    analyzable_bytes += file_stats["size"]
                if idx % 100 == 0:
                    print(f"Processed {idx}/{len(tasks)} files...")
                if idx == len(tasks) - 1:
                    print(
                        f"Processed {len(tasks)} files. Analyzable bytes: {analyzable_bytes}."
                    )
        start_time = time.time()

        # O(n) instead of O(n^2) per-dir
        folder_results = calculate_directory_stats(
            all_directories, codebase_stats, Path(temp_dir)
        )

        print(
            f"Processed {len(folder_results)} directories in {time.time() - start_time:.2f} seconds (O(n) algorithm)"
        )
        if analyzable_bytes == 0:
            # TODO: add status_reason to database when available
            print(
                f"Codebase {codebase_name} has no analyzable files. Setting status to connection failed."
            )
            with Session(engine) as session, session.begin():
                update_stmt = (
                    update(Version)
                    .where(Version.id == version_id)
                    .values(status=VersionStatus.CONNECTION_FAILED)
                )
                session.exec(update_stmt)
            return None

        s3_resource = resource("s3", endpoint_url=os.environ.get("AWS_S3_ENDPOINT_URL"))
        s3_bucket = s3_resource.Bucket(org_id_bucket)
        dropzone_bucket, dropzone_key = parse_presigned_url(presigned_url)
        s3 = client("s3")
        response = s3.head_object(Bucket=dropzone_bucket, Key=dropzone_key)

        # Extract metadata from S3 object
        metadata = response.get("Metadata", {})
        install_id = metadata.get("install_id", None)
        full_repo_name = metadata.get(
            "full_repo_name", None
        )  # e.g., "owner/repo" from provider
        if install_id is not None:
            s3_bucket.upload_file(
                Path(provisional_codebase_name),
                str(s3_dest),
                ExtraArgs={"Metadata": {"install_id": install_id}},
            )
        else:
            s3_bucket.upload_file(
                Path(provisional_codebase_name),
                str(s3_dest),
            )
        # s3_bucket.upload_file(Path(provisional_codebase_name), str(s3_dest))
        print(f"Uploaded {provisional_codebase_name} to {s3_dest}")

        # Helper functions for content-based node deduplication
        def _hash_file_content(file_path: Path) -> str:
            """Hash file content using SHA256."""
            with open(file_path, "rb") as f:
                content = f.read()
            return hashlib.sha256(content).hexdigest()

        def _hash_directory_node(children_hashes: list[str]) -> str:
            """Hash directory node based on sorted children content hashes."""
            sorted_hashes = sorted(children_hashes)
            hash_input = "|".join(sorted_hashes)
            return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

        def _find_node_by_hash(
            session: Session, primary_asset_id: str, source_hash: str
        ) -> Node | None:
            """Find existing Node with matching hash for the same PrimaryAsset."""
            return session.exec(
                select(Node)
                .where(Node.primary_asset_id == primary_asset_id)
                .where(Node.source_hash == source_hash)
                .limit(1)
            ).first()

        def _get_directory_children_hashes(
            session: Session, version_id: str, relative_path: str
        ) -> list[str]:
            """Get content hashes for direct children of a directory node."""
            dir_path = (
                relative_path if relative_path.endswith("/") else f"{relative_path}/"
            )
            target_depth = dir_path.count("/") - 1

            children_hashes = session.exec(
                select(Node.source_hash)
                .join(VersionNode, VersionNode.node_id == Node.id)
                .where(VersionNode.version_id == version_id)
                .where(VersionNode.relative_path.startswith(dir_path))
                .where(VersionNode.depth == target_depth + 1)
            ).all()

            return list(children_hashes)

        def _process_file_node(
            session: Session,
            version_id: str,
            primary_asset_id: str,
            file_path: Path,
            relative_path: str,
            file_stats: dict,
        ) -> None:
            """Process a file node: hash content, find/create Node, create VersionNode."""
            # Hash the file content
            source_hash = _hash_file_content(file_path)

            # Check if node with this hash already exists
            existing_node = _find_node_by_hash(session, primary_asset_id, source_hash)

            if existing_node:
                # Reuse existing node
                node_id = existing_node.id
                print(f"Reusing existing node {node_id} for {relative_path}")
            else:
                # Create new node
                new_node = Node(
                    source_hash=source_hash,
                    kind=NodeKind.CODEBASE_FILE,
                    primary_asset_id=primary_asset_id,
                )
                session.add(new_node)
                session.flush()
                node_id = new_node.id

                # Create DerivedContent for this node
                file_dc = DerivedContent(
                    content_kind=ContentKind.CODEBASE_FILE,
                    node_id=node_id,
                    relative_path=relative_path,
                    content=None,
                    content_name=None,
                    misc_metadata=None,
                )
                session.add(file_dc)
                print(f"Created new node {node_id} for {relative_path}")

            # Create VersionNode link
            version_node = VersionNode(
                version_id=version_id,
                relative_path=relative_path,
                primary_asset_id=primary_asset_id,
                node_id=node_id,
                misc_metadata=file_stats,
            )
            session.add(version_node)

        def _process_directory_node(
            session: Session,
            version_id: str,
            primary_asset_id: str,
            relative_path: str,
            directory_stats: dict,
        ) -> None:
            """Process a directory node: hash children, find/create Node, create VersionNode."""
            # Get children hashes
            children_hashes = _get_directory_children_hashes(
                session, version_id, relative_path
            )

            # Hash directory based on children
            source_hash = _hash_directory_node(children_hashes)

            # Check if node with this hash already exists
            existing_node = _find_node_by_hash(session, primary_asset_id, source_hash)

            if existing_node:
                # Reuse existing node
                node_id = existing_node.id
                print(f"Reusing existing directory node {node_id} for {relative_path}")
            else:
                # Create new node
                new_node = Node(
                    source_hash=source_hash,
                    kind=NodeKind.CODEBASE_DIRECTORY,
                    primary_asset_id=primary_asset_id,
                )
                node_id = new_node.id
                session.add(new_node)
                session.flush()
                print(f"Created new directory node {new_node.id} for {relative_path}")

            # Create VersionNode link
            version_node = VersionNode(
                version_id=version_id,
                relative_path=relative_path,
                primary_asset_id=primary_asset_id,
                node_id=node_id,
                misc_metadata=directory_stats,
            )
            session.add(version_node)

        # Process files and directories with content-based deduplication
        with Session(engine) as session, session.begin():
            # Sort files by depth (deepest first) to ensure children are processed before parents
            sorted_file_paths = sorted(
                codebase_stats.keys(),
                key=lambda p: p.as_posix().count("/"),
                reverse=True,
            )

            # Process files first
            file_count = 0
            for file_path in sorted_file_paths:
                if (
                    not codebase_stats[file_path]["is_blacklisted"]
                    and not codebase_stats[file_path]["is_ignored"]
                ):
                    relative_path = str(file_path.relative_to(temp_dir))
                    _process_file_node(
                        session,
                        version_id,
                        primary_asset_id,
                        file_path,
                        relative_path,
                        codebase_stats[file_path],
                    )
                    file_count += 1
                    if file_count % 100 == 0:
                        print(
                            f"Processed {file_count}/{len(sorted_file_paths)} files..."
                        )

            print(f"Processed {file_count} file nodes")

            # Sort directories by depth (deepest first) to ensure children are processed before parents
            sorted_folder_results = sorted(
                folder_results, key=lambda x: x[1].count("/"), reverse=True
            )

            # Process directories after files
            dir_count = 0
            for directory_stats, relative_path in sorted_folder_results:
                if directory_stats is not None:
                    # Add ignored files count to root directory metadata
                    if relative_path == codebase_name + "/":
                        directory_stats["driver_ignored_files"] = len(ignored_files)

                    _process_directory_node(
                        session,
                        version_id,
                        primary_asset_id,
                        relative_path,
                        directory_stats,
                    )
                    dir_count += 1
                    if dir_count % 100 == 0:
                        print(
                            f"Processed {dir_count}/{len(sorted_folder_results)} directories..."
                        )

            print(f"Processed {dir_count} directory nodes")

            # Update version status
            version = session.get(Version, version_id)
            version_status = version.status
            if version_status != VersionStatus.GENERATING:
                version.status = VersionStatus.CONNECTED
                session.add(version)
        # Do this check outside the DB session so that the nodes get committed
        if version_status == VersionStatus.GENERATING:
            inspector_input = InspectorInput(
                version_id=version_id,
            )
            try:
                inspector_task.run_no_wait(inspector_input)
            except Exception as e:
                print(f"Uncaught during inspection: {e}")
                # Note: this is likely redundant setting of error state, but this allows us to handle modal timeout exceptions

                with Session(engine) as session, session.begin():
                    update_stmt = (
                        update(Version)
                        .where(Version.id == version_id)
                        .values(status=VersionStatus.GENERATION_ERROR)
                    )
                    session.exec(update_stmt)
                raise
            print("Inspection complete")

    print(
        f"Codebase connection complete for codebase: {codebase_name} (cb id: {primary_asset_id}). "
        f"Version ID: {version_id}."
    )

    # === ANALYTICS TRIGGER ===
    # Spawn analytics as separate background task (non-blocking)
    # Only trigger for INITIAL connections (no previous version).
    # For PUSH events (has previous_version_id), analytics is triggered by
    # _trigger_incremental_analytics_for_push() in handle_*_events() with incremental=True.
    # Note: We check previous_version_id instead of status because rapid pushes may
    # create versions with CONNECTING status before the previous version completes generation.
    with Session(engine) as session:
        version = session.get(Version, version_id)
        is_push_event = version.previous_version_id is not None

    if not is_push_event:
        analytics_codebase_name = full_repo_name or provisional_codebase_name
        _spawn_analytics_task(
            codebase_id=str(primary_asset_id),
            organization_id=org_id,
            codebase_name=analytics_codebase_name,
            provider=provider,
            install_id=install_id,
        )
        # Note: _spawn_analytics_task never raises, so connection always succeeds
    else:
        print(
            "Skipping analytics in run_codebase_connection (push event handled by incremental trigger)"
        )

    return None
