import base64
import hashlib
import hmac
import json
import logging
from itertools import groupby
from uuid import UUID

from database.models import (
    GithubAppInstallation,
    GitProviderApp,
    GitProviderAppInstallation,
    GitProviderKind,
)
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from hatchet_sdk import Hatchet
from pydantic import BaseModel
from shared.interfaces.aws_client_config import AWSClientConfig
from shared.interfaces.hatchet_interfaces import (
    ConnectReposForInstallationInput,
    HandleAzureDevopsEventsInput,
    HandleBitbucketDCEventsInput,
    HandleBitbucketEventsInput,
    HandleGithubEventsInput,
    HandleGitlabEventsInput,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from app.api.auth import UserToken
from app.api.session import CurrentSession
from app.authorization.fastapi import enforce_org_action
from app.core.config import settings
from app.git_providers.utils.errors import (
    GitProviderAccessTokenError,
)
from app.git_providers.utils.vcs_auto_update import is_update_required
from app.repositories.git_provider_repository import (
    git_provider_app_by_id,
    git_provider_app_installation_by_id,
)
from app.repositories.github_app_installations_repository import (
    GithubAppInstallationsRepository,
)
from app.schemas.git_provider_schema import (
    AccessTokenData,
    CreateGitProviderAppRequest,
    GitRepository,
    WebhookInfo,
)
from app.services.git_provider_service import get_git_provider_service
from app.utils.aws_secrets_manager import format_secret_key, write_secret
from app.utils.gh_ops import (
    exchange_code_for_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()

aws_config = AWSClientConfig(
    region_name=settings.AWS_REGION,
    # TODO: Ensure required IAM role permissions are given to the containers
    aws_access_key_id=settings.S3ADMIN_AWS_ACCESS_KEY_ID
    if not settings.IS_PRIVATE_DEPLOY
    else None,
    aws_secret_access_key=settings.S3ADMIN_AWS_SECRET_ACCESS_KEY
    if not settings.IS_PRIVATE_DEPLOY
    else None,
)

provider_service = get_git_provider_service(aws_config)


class OkResponse(BaseModel):
    """Response model to validate and return when performing a health check."""

    status: str = "OK"


#### APP ###
@router.get(
    "/app",
    summary="Get git provider apps",
    response_model=list[GitProviderApp],
)
def get_apps(
    session: CurrentSession,
    current_user: UserToken,
) -> list[GitProviderApp]:
    enforce_org_action(session, current_user, "vcs.manage")
    return provider_service.list_apps(session, current_user.organization_id)


@router.post(
    "/app",
    summary="Create git provider app.",
    response_model=GitProviderApp,
)
def create_app(
    session: CurrentSession,
    gp_app_input: CreateGitProviderAppRequest,
    current_user: UserToken,
) -> GitProviderApp:
    enforce_org_action(session, current_user, "vcs.manage")
    return provider_service.create_app(session, gp_app_input.model_dump(by_alias=True))


@router.delete(
    "/app/{application_id}",
    summary="Delete git provider app.",
)
def delete_git_provider_app(
    session: CurrentSession,
    current_user: UserToken,
    application_id: str,
) -> JSONResponse:
    enforce_org_action(session, current_user, "vcs.manage")
    provider_service.delete_app(session, current_user.organization_id, application_id)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": "App deleted."},
    )


@router.get(
    "/app/{application_id}/installations",
    summary="Get app install for logged.",
    response_model=list[GitProviderAppInstallation],
)
def get_app_installation(
    session: CurrentSession,
    current_user: UserToken,
    application_id: str,
) -> list[GitProviderAppInstallation]:
    enforce_org_action(session, current_user, "vcs.manage")
    return provider_service.list_app_installations(
        session,
        current_user.organization_id,
        application_id,
    )


@router.post(
    "/app/{application_id}/token",
    summary="Add access token to the app.",
    response_model=GitProviderAppInstallation,
)
def add_access_token(
    session: CurrentSession,
    current_user: UserToken,
    application_id: str,
    gat: AccessTokenData,
) -> GitProviderAppInstallation:
    enforce_org_action(session, current_user, "vcs.manage")
    try:
        install = provider_service.install_access_token(
            session, current_user.organization_id, application_id, gat.model_dump()
        )
    except GitProviderAccessTokenError:
        logger.exception("Error adding token")
        raise HTTPException(status_code=500, detail="Invalid token")

    if not install:
        raise HTTPException(status_code=404, detail="Installation not found.")

    return install


@router.get(
    "/app/{application_id}/installations/{installation_id}/webhook",
    summary="Get details for setting up a webhook.",
    response_model=WebhookInfo,
)
def get_app_installation_webhook_info(
    session: CurrentSession,
    current_user: UserToken,
    application_id: UUID,
    installation_id: UUID,
) -> WebhookInfo:
    enforce_org_action(session, current_user, "vcs.manage")
    webhook_info = provider_service.get_webhook_info(
        session,
        current_user.organization_id,
        application_id,
        installation_id,
    )
    return webhook_info


@router.post(
    "/app/{application_id}/installations/{installation_id}/webhook/register",
    summary="Register a webhook for the git provider installation.",
)
def register_webhook(
    session: CurrentSession,
    current_user: UserToken,
    application_id: UUID,
    installation_id: UUID,
) -> JSONResponse:
    enforce_org_action(session, current_user, "vcs.manage")
    try:
        result = provider_service.register_webhook(
            session,
            current_user.organization_id,
            str(application_id),
            str(installation_id),
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=result,
        )
    except (KeyError, ValueError, SQLAlchemyError) as e:
        logger.exception(f"Webhook registration failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error during webhook registration.",
        )


@router.delete(
    "/app/{application_id}/installations/{installation_id}/webhook/register",
    summary="Deregister a webhook for the git provider installation.",
)
def deregister_webhook(
    session: CurrentSession,
    current_user: UserToken,
    application_id: UUID,
    installation_id: UUID,
) -> JSONResponse:
    enforce_org_action(session, current_user, "vcs.manage")
    try:
        result = provider_service.deregister_webhook(
            session,
            current_user.organization_id,
            str(application_id),
            str(installation_id),
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=result,
        )
    except (KeyError, ValueError, SQLAlchemyError) as e:
        logger.exception(f"Webhook deregistration failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error during webhook deregistration.",
        )


@router.delete(
    "/app/{application_id}/installations/{installation_id}",
    summary="Delete app install",
)
def delete_app_installation(
    session: CurrentSession,
    current_user: UserToken,
    application_id: str,
    installation_id: str,
) -> JSONResponse:
    enforce_org_action(session, current_user, "vcs.manage")
    provider_service.revoke_access_token(
        session, current_user.organization_id, application_id, installation_id
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": "Installation deleted."},
    )


@router.get(
    "/app/{application_id}/repos/{installation_id}",
    response_model=list[GitRepository],
)
def get_repositories_by_installation_id(
    session: CurrentSession,
    current_user: UserToken,
    application_id: str,
    installation_id: str,
) -> list[GitRepository]:
    enforce_org_action(session, current_user, "vcs.manage")
    try:
        return provider_service.list_repositories(
            session,
            current_user.organization_id,
            application_id,
            installation_id,
        )
    except GitProviderAccessTokenError as e:
        logger.error(f"Error fetching repositories: {e}")
        raise HTTPException(status_code=403, detail="Invalid or expired token")


@router.put(
    "/app/{application_id}/repos/{installation_id}/token",
    response_model=GitProviderAppInstallation,
)
def update_git_provider_group_access_token(
    session: CurrentSession,
    current_user: UserToken,
    application_id: str,
    installation_id: str,
    new_gat: AccessTokenData,
) -> GitProviderAppInstallation:
    enforce_org_action(session, current_user, "vcs.manage")
    try:
        install = provider_service.update_access_token(
            session,
            current_user.organization_id,
            application_id,
            installation_id,
            new_gat.model_dump(by_alias=True),
        )
    except GitProviderAccessTokenError:
        raise HTTPException(status_code=500, detail="Invalid token")

    return install


@router.post(
    "/app/{application_id}/connect-repos",
)
def connect_git_provider_repo(
    session: CurrentSession,
    current_user: UserToken,
    application_id: UUID,
    repos: list[GitRepository],
) -> JSONResponse:
    enforce_org_action(session, current_user, "vcs.manage")
    # Get the app to determine provider type
    app = git_provider_app_by_id(
        session, current_user.organization_id, str(application_id)
    )
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")

    # Determine which handler to use based on provider type
    hatchet = Hatchet()
    if app.provider_kind == GitProviderKind.GITLAB_ENTERPRISE_SELF_MANAGED:
        handle_events_task = hatchet.stubs.task(
            name="handle-gitlab-events-workflow",
            input_validator=HandleGitlabEventsInput,
        )
        input_type = HandleGitlabEventsInput
    elif app.provider_kind == GitProviderKind.BITBUCKET:
        handle_events_task = hatchet.stubs.task(
            name="handle-bitbucket-events-workflow",
            input_validator=HandleBitbucketEventsInput,
        )
        input_type = HandleBitbucketEventsInput
    elif app.provider_kind == GitProviderKind.AZURE_DEVOPS_CLOUD:
        handle_events_task = hatchet.stubs.task(
            name="handle-azure-devops-events-workflow",
            input_validator=HandleAzureDevopsEventsInput,
        )
        input_type = HandleAzureDevopsEventsInput
    elif app.provider_kind == GitProviderKind.BITBUCKET_DATA_CENTER:
        handle_events_task = hatchet.stubs.task(
            name="handle-bitbucket-dc-events-workflow",
            input_validator=HandleBitbucketDCEventsInput,
        )
        input_type = HandleBitbucketDCEventsInput
    else:
        raise HTTPException(
            status_code=400, detail=f"Unsupported provider kind: {app.provider_kind}"
        )

    repos.sort(key=lambda x: x.installation_id)
    installation_groups = {
        k: list(v) for k, v in groupby(repos, key=lambda x: x.installation_id)
    }

    for installation_id, repo_group in installation_groups.items():
        app_install = git_provider_app_installation_by_id(session, installation_id)
        if (
            app_install.git_provider_app_id != application_id
            or app_install.organization_id != current_user.organization_id
        ):
            raise HTTPException(status_code=404, detail="Installation not found.")

        handle_events_task.run_no_wait(
            input_type(
                installation_id=installation_id,
                org_id=current_user.organization_id,
                repos_added=[repo.model_dump() for repo in repo_group],
                repos_deleted=[],
                repos_pushed=[],
            )
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"message": "Connecting"},
    )


### GIT PROVIDER ###


@router.get("/github/callback", response_model=OkResponse)
def github_callback(
    session: CurrentSession,
    code: str,
    state: str,
    installation_id: str,
    request: Request,
    response: Response,
) -> OkResponse:
    if not code:
        raise HTTPException(status_code=400, detail="Bad request")

    state_bytes = base64.b64decode(state)
    state_str = state_bytes.decode("utf-8")
    state_dict = json.loads(state_str)
    # TODO: validate state_dict
    org_id, user_id = state_dict["org_id"], state_dict["user_id"]
    secret_key = format_secret_key(org_id, user_id, "github")
    token_data = exchange_code_for_token(code)
    secret_value = json.dumps(token_data)
    write_secret(
        secret_key, secret_value
    )  # TODO make sure these are unused and stop saving them to avoid confusion.

    existing_installation = session.exec(
        select(GithubAppInstallation).where(
            GithubAppInstallation.organization_id == org_id,
            GithubAppInstallation.github_app_installation_id == installation_id,
        )
    ).first()

    if existing_installation is None:
        logging.info(
            f"Creating github app installation for org = {org_id}, installation = {installation_id}"
        )
        gh_app_install = GithubAppInstallation(
            organization_id=org_id, github_app_installation_id=installation_id
        )
        session.add(gh_app_install)
        session.commit()
        hatchet = Hatchet()
        connect_repos_task = hatchet.stubs.task(
            name="connect-repos-for-installation-workflow",
            input_validator=ConnectReposForInstallationInput,
        )
        connect_repos_task.run_no_wait(
            ConnectReposForInstallationInput(github_installation_id=installation_id)
        )

    content = "<html><body><script>window.close();</script></body></html>"
    return Response(content=content, media_type="text/html")


def verify_signature(
    payload_body: bytes, secret_token: str, signature_header: str
) -> None:
    """Verify that the payload was sent from GitHub by validating SHA256.

    Raise and return 403 if not authorized.

    Args:
        payload_body: original request body to verify (request.body())
        secret_token: GitHub app webhook token (WEBHOOK_SECRET)
        signature_header: header received from GitHub (x-hub-signature-256)
    """
    if not signature_header:
        raise HTTPException(
            status_code=403, detail="x-hub-signature-256 header is missing!"
        )
    hash_object = hmac.new(
        secret_token.encode("utf-8"), msg=payload_body, digestmod=hashlib.sha256
    )
    expected_signature = "sha256=" + hash_object.hexdigest()
    if not hmac.compare_digest(expected_signature, signature_header):
        raise HTTPException(status_code=403, detail="Request signatures didn't match!")


async def _extract_body_and_headers(request: Request) -> dict:
    body_bytes = await request.body()
    body_json = json.loads(body_bytes)
    return {"raw_body": body_bytes, "json_body": body_json, "headers": request.headers}


def verify_github_signature(
    body_bytes: bytes, secret_token: str, signature_header: str
) -> None:
    try:
        verify_signature(body_bytes, secret_token, signature_header)
    except HTTPException as e:
        logger.warning("Signature verification failed")
        raise e


def handle_installation_delete_event(
    session: CurrentSession, body: dict
) -> JSONResponse:
    installation_id = str(body["installation"]["id"])
    installation_record = session.exec(
        select(GithubAppInstallation).where(
            GithubAppInstallation.github_app_installation_id == installation_id
        )
    ).first()
    org_id = installation_record.organization_id
    session.delete(installation_record)
    session.commit()

    repositories = body["repositories"]
    repos_added = []
    repos_deleted = []
    repos_pushed = []
    for repo in repositories:
        repos_deleted.append(
            {
                "id": repo["id"],
                "name": repo["name"],
                "full_name": repo["full_name"],
            }
        )

    hatchet = Hatchet()
    handle_github_events_task = hatchet.stubs.task(
        name="handle-github-events-workflow",
        input_validator=HandleGithubEventsInput,
    )

    handle_github_events_task.run_no_wait(
        HandleGithubEventsInput(
            installation_id=installation_id,
            org_id=org_id,
            repos_added=repos_added,
            repos_deleted=repos_deleted,
            repos_pushed=repos_pushed,
        )
    )

    logger.info(
        f"Installation delete event processed for {installation_id}. Disconnecting repos."
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"message": ""},
    )


def handle_installation_modified_event(
    session: CurrentSession, body: dict
) -> JSONResponse:
    installation_id = str(body["installation"]["id"])
    gh_app_install = GithubAppInstallationsRepository(session).list_by_installation_id(
        installation_id
    )[0]
    if not gh_app_install:
        logger.warning(
            "Installation ID not found in the database; need a row for this app install"
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content={"message": ""}
        )

    body_repos_added = body["repositories_added"]
    body_repos_removed = body["repositories_removed"]

    repos_added = []
    repos_deleted = []
    repos_pushed = []

    for repo in body_repos_added:
        # This repo may already be connected, okay to have integrityerror in modal?
        repos_added.append(
            {
                "id": repo["id"],
                "name": repo["name"],
                "full_name": repo["full_name"],
            }
        )

    for repo in body_repos_removed:
        repos_deleted.append(
            {
                "id": repo["id"],
                "name": repo["name"],
                "full_name": repo["full_name"],
            }
        )

    hatchet = Hatchet()
    handle_github_events_task = hatchet.stubs.task(
        name="handle-github-events-workflow",
        input_validator=HandleGithubEventsInput,
    )
    handle_github_events_task.run_no_wait(
        HandleGithubEventsInput(
            installation_id=installation_id,
            org_id=gh_app_install.organization_id,
            repos_added=repos_added,
            repos_deleted=repos_deleted,
            repos_pushed=repos_pushed,
        )
    )

    logger.info(
        f"Installation modified event processed for {installation_id}. Connecting repos."
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"message": ""},
    )


def handle_push_event(session: CurrentSession, body: dict) -> JSONResponse:
    repository = body["repository"]
    org_name = repository.get("owner", {}).get("login", "unknown")
    repo_name = repository["name"]
    repo_id = str(repository["id"])
    default_branch = repository["default_branch"]
    pushed_ref = body["ref"]
    installation_id = str(body["installation"]["id"])
    commit_hash = body["after"]

    if pushed_ref != f"refs/heads/{default_branch}":
        logger.info(
            "Push event ignored: Not the default branch. Org: %s, Repo: %s, Ref: %s, Install ID: %s",
            org_name,
            repo_name,
            pushed_ref,
            installation_id,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"message": "Push event ignored (not default branch)"},
        )

    logger.info(
        "Push event on default branch. Org: %s, Repo: %s, Branch: %s, Install ID: %s",
        org_name,
        repo_name,
        default_branch,
        installation_id,
    )

    # We assume the installation ID is only into one org...
    gh_app_install = GithubAppInstallationsRepository(session).list_by_installation_id(
        installation_id
    )[0]

    if not gh_app_install:
        logger.warning(
            "Installation ID not found in the database; need a row for this app install"
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content={"message": ""}
        )

    process_update, message = is_update_required(
        session=session, org_id=gh_app_install.organization_id, repo_name=repo_name
    )

    if process_update:
        repos_added = []
        repos_deleted = []
        repos_pushed = [
            {
                "id": repo_id,
                "name": repo_name,
                "full_name": repository["full_name"],
                "commit": commit_hash,
            }
        ]
        hatchet = Hatchet()
        handle_github_events_task = hatchet.stubs.task(
            name="handle-github-events-workflow",
            input_validator=HandleGithubEventsInput,
        )
        handle_github_events_task.run_no_wait(
            HandleGithubEventsInput(
                installation_id=installation_id,
                org_id=gh_app_install.organization_id,
                repos_added=repos_added,
                repos_deleted=repos_deleted,
                repos_pushed=repos_pushed,
            )
        )

        logger.info(
            f"Push event processed for repo: {repo_name}. Processing in background job."
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=message,
    )


def handle_ping_event() -> JSONResponse:
    logger.info("Ping event received from GitHub.")
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED, content={"message": "Ping received"}
    )


@router.post("/github/webhook")
def webhook(
    session: CurrentSession, body_data: dict = Depends(_extract_body_and_headers)
) -> JSONResponse:
    body_bytes = body_data["raw_body"]
    body = body_data["json_body"]
    headers = body_data["headers"]

    github_event = headers.get("x-github-event", "")
    signature_header = headers.get("x-hub-signature-256", "")

    secret_token = settings.GH_WEBHOOK_SECRET

    verify_github_signature(body_bytes, secret_token, signature_header)

    if github_event == "push":
        return handle_push_event(session, body)
    elif github_event == "ping":
        return handle_ping_event()
    elif github_event == "installation":
        # TODO handle installation suspension;
        if body["action"] == "deleted":
            return handle_installation_delete_event(session, body)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content={"message": ""}
        )
    elif github_event == "installation_repositories":
        # Add or remove event. An edit causes two github events
        return handle_installation_modified_event(session, body)

    logger.info("Unhandled event type: %s", github_event)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED, content={"message": "Event ignored"}
    )


@router.post("/app/webhook")
def git_provider_webhook(
    session: CurrentSession,
    body_data: dict = Depends(_extract_body_and_headers),
    installation_id: str | None = Query(None),  # For Bitbucket query param
) -> JSONResponse:
    """Generic webhook handler for GitLab, Bitbucket, and Azure DevOps"""
    body = body_data["json_body"]
    headers = body_data["headers"]
    raw_body = body_data["raw_body"]  # For HMAC signature verification
    logger.info("Received webhook event: %s", body)

    # Get installation_id from query param OR header
    if not installation_id:
        installation_id = headers.get("x-driver-token")  # GitLab and Azure DevOps

    if not installation_id:
        logger.error("Installation ID not found in headers or query params")
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        # Delegate everything to the service
        content = provider_service.handle_webhook_event(
            session=session,
            installation_id=installation_id,
            headers=headers,
            body=body,
            raw_body=raw_body,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=content,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
