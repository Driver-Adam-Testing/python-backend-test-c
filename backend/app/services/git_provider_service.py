import logging
from typing import Any, ClassVar

import httpx
from botocore.exceptions import ClientError
from database.models import (
    GitProviderApp,
    GitProviderAppInstallation,
    GitProviderKind,
)
from pydantic import ValidationError
from shared.interfaces.aws_client_config import AWSClientConfig
from shared.secret_management.aws_secret_management import (
    AWSSecretManagementStrategy,
    format_secret_name,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session

from app.core.config import settings
from app.git_providers.interfaces.provider_interface import (
    GitProviderInterface,
    WebhookConfig,
    WebhookEventContext,
)
from app.git_providers.providers.azure_devops_provider import AzureDevOpsProvider
from app.git_providers.providers.bitbucket_dc_provider import BitbucketDCProvider
from app.git_providers.providers.bitbucket_provider import BitbucketProvider
from app.git_providers.providers.gitlab_provider import GitLabProvider
from app.git_providers.utils.errors import (
    GitProviderAccessTokenError,
    GitProviderAppRevokeError,
)
from app.repositories.git_provider_repository import (
    git_provider_app_by_id,
    git_provider_app_installation_by_id,
    git_provider_app_installation_by_org_id,
    git_provider_apps_by_org_id,
)
from app.schemas.git_provider_schema import GitRepository, WebhookInfo
from app.schemas.secret_management_schema import (
    APP_INSTALL_WAT_NAME_PREFIX,
)

logger = logging.getLogger(__name__)


class GitProviderService:
    """Unified service for all Git provider operations"""

    # Provider registry
    PROVIDERS: ClassVar[dict[GitProviderKind, type[GitProviderInterface]]] = {
        GitProviderKind.GITLAB_ENTERPRISE_SELF_MANAGED: GitLabProvider,
        GitProviderKind.BITBUCKET: BitbucketProvider,
        GitProviderKind.BITBUCKET_DATA_CENTER: BitbucketDCProvider,
        GitProviderKind.AZURE_DEVOPS_CLOUD: AzureDevOpsProvider,
    }

    def __init__(self, aws_config: AWSClientConfig) -> None:
        self.aws_config = aws_config
        self.secrets_manager = AWSSecretManagementStrategy(aws_config)

    def get_provider(self, app: GitProviderApp) -> GitProviderInterface:
        provider_class = self.PROVIDERS.get(app.provider_kind)
        if not provider_class:
            raise ValueError(f"Unsupported provider kind: {app.provider_kind}")

        return provider_class.from_config(app, self.aws_config)

    # App Management

    def create_app(self, session: Session, app_data: dict[str, Any]) -> GitProviderApp:
        app = GitProviderApp(**app_data)

        if app.provider_kind in [GitProviderKind.BITBUCKET]:
            # Bitbucket Cloud requires a workspace for apps
            app.provider_metadata = {"workspace": app.name}
        elif app.provider_kind == GitProviderKind.BITBUCKET_DATA_CENTER:
            # Bitbucket DC stores instance URL in provider_metadata
            app.provider_metadata = {"instance_url": app.base_url}

        session.add(app)

        try:
            session.commit()
        except SQLAlchemyError as e:
            logger.error(f"Failed to create app: {e}")
            session.rollback()
            raise

        session.refresh(app)
        logger.info(f"Created {app.provider_kind} app: {app.id}")
        return app

    def list_apps(
        self,
        session: Session,
        organization_id: str,
        provider_kind: GitProviderKind | None = None,
    ) -> list[GitProviderApp]:
        apps = git_provider_apps_by_org_id(session, organization_id)

        if provider_kind:
            apps = [app for app in apps if app.provider_kind == provider_kind]

        return apps

    def list_app_installations(
        self, session: Session, organization_id: str, app_id: str
    ) -> list[GitProviderAppInstallation]:
        return git_provider_app_installation_by_org_id(session, organization_id, app_id)

    def delete_app(self, session: Session, organization_id: str, app_id: str) -> None:
        app = git_provider_app_by_id(session, organization_id, app_id)
        provider = self.get_provider(app)

        # Delete all installations
        installations = git_provider_app_installation_by_org_id(
            session, organization_id, app_id
        )
        for installation in installations:
            provider.revoke_access(installation)
            session.delete(installation)

        # Delete app
        session.delete(app)
        session.commit()
        logger.info(f"Deleted app {app_id} and {len(installations)} installations")

    # Access Token Management

    def install_access_token(
        self,
        session: Session,
        organization_id: str,
        app_id: str,
        token_data: dict[str, Any],  # TODO: define concrete type
    ) -> GitProviderAppInstallation:
        app = git_provider_app_by_id(session, organization_id, app_id)
        provider = self.get_provider(app)

        is_valid, error = provider.validate_access_token(token_data)
        if not is_valid:
            raise GitProviderAccessTokenError(error or "Invalid access token")

        installation = provider.create_installation(organization_id, app_id, token_data)

        app.app_installations.append(installation)
        session.add(installation)

        try:
            session.commit()
        except SQLAlchemyError as e:
            logger.error(f"Failed to install access token: {e}")
            session.rollback()
            raise

        session.refresh(installation)

        try:
            provider.store_secrets(installation, token_data)
        except (ClientError, ValidationError, ValueError) as e:
            logger.error(f"Failed to store secrets, removing installation record: {e}")
            session.delete(installation)
            session.commit()
            raise

        logger.info(f"Installed access token for app {app_id}: {installation.id}")
        return installation

    def update_access_token(
        self,
        session: Session,
        organization_id: str,
        app_id: str,
        installation_id: str,
        token_data: dict[str, Any],  # TODO: define concrete type
    ) -> GitProviderAppInstallation:
        installation = git_provider_app_installation_by_id(session, installation_id)
        if (
            not installation
            or installation.organization_id != organization_id
            or str(installation.git_provider_app_id) != app_id
        ):
            raise ValueError("Installation not found or doesn't match app")

        provider = self.get_provider(installation.git_provider_app)

        is_valid, error = provider.validate_access_token(token_data)
        if not is_valid:
            raise GitProviderAccessTokenError(error or "Invalid access token")

        provider.update_secrets(installation, token_data)

        logger.info(f"Updated access token for installation {installation_id}")
        return installation

    def revoke_access_token(
        self, session: Session, organization_id: str, app_id: str, installation_id: str
    ) -> None:
        installation = git_provider_app_installation_by_id(session, installation_id)
        if (
            not installation
            or installation.organization_id != organization_id
            or str(installation.git_provider_app_id) != app_id
        ):
            raise ValueError("Installation not found or doesn't match app")

        provider = self.get_provider(installation.git_provider_app)
        provider.revoke_access(installation)

        session.delete(installation)

        try:
            session.commit()
        except SQLAlchemyError as e:
            logger.error(f"Failed to revoke access token: {e}")
            session.rollback()
            raise

        logger.info(f"Revoked access token for installation {installation_id}")

    # Repository Operations

    def list_repositories(
        self, session: Session, organization_id: str, app_id: str, installation_id: str
    ) -> list[GitRepository]:
        installation = git_provider_app_installation_by_id(session, installation_id)
        if (
            not installation
            or installation.organization_id != organization_id
            or str(installation.git_provider_app_id) != app_id
        ):
            raise ValueError("Installation not found or doesn't match app")

        provider = self.get_provider(installation.git_provider_app)

        try:
            return provider.fetch_repositories(installation)
        except GitProviderAppRevokeError:
            logger.error(f"Access revoked for installation {installation_id}")
            self.handle_access_revoked(
                session, organization_id, app_id, installation_id
            )
            raise

    def get_webhook_info(
        self, session: Session, organization_id: str, app_id: str, installation_id: str
    ) -> WebhookInfo:
        installation = git_provider_app_installation_by_id(session, installation_id)
        if (
            not installation
            or installation.organization_id != organization_id
            or str(installation.git_provider_app_id) != str(app_id)
        ):
            raise ValueError("Installation not found or doesn't match app")

        provider = self.get_provider(installation.git_provider_app)
        secret = provider.fetch_secrets(installation)

        if installation.git_provider_app.provider_kind == GitProviderKind.BITBUCKET:
            # Bitbucket requires a specific webhook structure
            webhook_info = WebhookInfo(
                callback_url=f"{settings.AUTH0_AUDIENCE}/git-provider/app/webhook?installation_id={installation_id}",
                custom_headers={},
                secret_token=secret["secret_token"],
                ssl_verification=True,
                triggers=["push events"],
            )
        elif (
            installation.git_provider_app.provider_kind
            == GitProviderKind.GITLAB_ENTERPRISE_SELF_MANAGED
        ):
            # TODO: move this to provider interface
            webhook_info = WebhookInfo(
                callback_url=f"{settings.AUTH0_AUDIENCE}/git-provider/app/webhook",
                custom_headers={"x-driver-token": installation_id},
                secret_token=secret["secret_token"],
                ssl_verification=True,
                triggers=["push events", "Project or group access token events"],
            )
        elif (
            installation.git_provider_app.provider_kind
            == GitProviderKind.AZURE_DEVOPS_CLOUD
        ):
            # Azure DevOps service hooks (manually configured)
            webhook_info = WebhookInfo(
                callback_url=f"{settings.AUTH0_AUDIENCE}/git-provider/app/webhook",
                custom_headers=[
                    f"x-driver-token: {installation_id}",
                    f"x-webhook-token: {secret["secret_token"]}",
                ],
                secret_token="",  # Not used in Azure DevOps
                ssl_verification=True,
                triggers=["git.push"],
            )
        elif (
            installation.git_provider_app.provider_kind
            == GitProviderKind.BITBUCKET_DATA_CENTER
        ):
            # Bitbucket DC webhooks (manually configured via Bitbucket UI)
            webhook_info = WebhookInfo(
                callback_url=f"{settings.AUTH0_AUDIENCE}/git-provider/app/webhook?installation_id={installation_id}",
                custom_headers={},
                secret_token=secret["secret_token"],
                ssl_verification=not secret.get("disable_ssl_verify", False),
                triggers=["repo:refs_changed", "pr:merged"],
            )
        else:
            raise ValueError(
                f"Unsupported provider kind: {installation.git_provider_app.provider_kind}"
            )
        return webhook_info

    def register_webhook(
        self,
        session: Session,
        organization_id: str,
        app_id: str,
        installation_id: str,
        triggers: list[str] | None = None,
    ) -> dict:
        """Register a webhook for an installation.

        Provider handles scope discovery internally.

        Args:
            session: Database session
            organization_id: Organization ID
            app_id: Git provider app ID
            installation_id: Installation ID
            triggers: Optional list of triggers (defaults to push and merge events)

        Returns:
            Webhook registration result with id, callback_url, etc.
        """
        installation = git_provider_app_installation_by_id(session, installation_id)
        if (
            not installation
            or installation.organization_id != organization_id
            or str(installation.git_provider_app_id) != app_id
        ):
            raise ValueError("Installation not found or doesn't match app")

        if (
            installation.git_provider_app.provider_kind
            != GitProviderKind.BITBUCKET_DATA_CENTER
        ):
            raise ValueError(
                f"Webhook registration not supported for {installation.git_provider_app.provider_kind}"
            )

        provider = self.get_provider(installation.git_provider_app)

        # Build webhook config
        callback_url = f"{settings.AUTH0_AUDIENCE}/git-provider/app/webhook"
        default_triggers = ["repo:refs_changed", "pr:merged"]

        config = WebhookConfig(
            callback_url=callback_url,
            triggers=triggers or default_triggers,
            description="Driver AI Webhook",
        )

        # Register webhook - provider handles scope discovery internally
        result = provider.register_webhook(installation, config)

        # Save webhook state in installation metadata
        # Provider already persists scope info; service only tracks registration state
        metadata = dict(installation.misc_metadata or {})
        metadata["webhook_registered"] = True
        metadata["webhook_id"] = result["id"]
        installation.misc_metadata = metadata
        flag_modified(installation, "misc_metadata")
        session.add(installation)

        try:
            session.commit()
        except SQLAlchemyError as e:
            logger.error(f"Failed to commit webhook metadata: {e}")
            session.rollback()
            # Attempt to clean up the orphaned webhook in Bitbucket DC
            try:
                provider.deregister_webhook(installation, str(result["id"]))
                logger.info(
                    f"Cleaned up orphaned webhook {result['id']} after commit failure"
                )
            except (
                httpx.HTTPStatusError,
                httpx.RequestError,
                NotImplementedError,
                ValueError,
            ) as cleanup_error:
                logger.error(
                    f"Failed to clean up orphaned webhook {result['id']}: {cleanup_error}. "
                    f"Manual cleanup may be required in Bitbucket DC."
                )
            raise

        session.refresh(installation)

        logger.info(f"Registered webhook for installation {installation_id}")

        return result

    def deregister_webhook(
        self,
        session: Session,
        organization_id: str,
        app_id: str,
        installation_id: str,
    ) -> dict:
        """Deregister a webhook for an installation.

        Provider handles scope lookup internally using installation metadata.

        Args:
            session: Database session
            organization_id: Organization ID
            app_id: Git provider app ID
            installation_id: Installation ID

        Returns:
            Status message
        """
        installation = git_provider_app_installation_by_id(session, installation_id)
        if (
            not installation
            or installation.organization_id != organization_id
            or str(installation.git_provider_app_id) != app_id
        ):
            raise ValueError("Installation not found or doesn't match app")

        if (
            installation.git_provider_app.provider_kind
            != GitProviderKind.BITBUCKET_DATA_CENTER
        ):
            raise ValueError(
                f"Webhook deregistration not supported for {installation.git_provider_app.provider_kind}"
            )

        metadata = installation.misc_metadata or {}
        webhook_id = metadata.get("webhook_id")
        if webhook_id is None:
            raise ValueError("No webhook registered for this installation")

        # Provider handles scope lookup internally from installation metadata
        provider = self.get_provider(installation.git_provider_app)
        provider.deregister_webhook(installation, str(webhook_id))

        # Update metadata to mark webhook as deregistered
        # Note: If commit fails here, the webhook is already deleted from Bitbucket DC
        # but our metadata will be stale. This is logged but we re-raise to inform the caller.
        metadata = dict(installation.misc_metadata or {})
        metadata["webhook_registered"] = False
        metadata.pop("webhook_id", None)
        # Clean up provider-specific webhook metadata
        metadata.pop("webhook_scope_type", None)
        metadata.pop("webhook_project_key", None)
        metadata.pop("webhook_repo_slug", None)
        installation.misc_metadata = metadata
        flag_modified(installation, "misc_metadata")
        session.add(installation)

        try:
            session.commit()
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to commit webhook deregistration metadata: {e}. "
                f"Webhook {webhook_id} was already deleted from Bitbucket DC. "
                f"Metadata may be inconsistent until next successful operation."
            )
            session.rollback()
            raise

        session.refresh(installation)

        logger.info(f"Deregistered webhook for installation {installation_id}")

        return {"message": "Webhook deregistered successfully"}

    # Helper Methods

    def handle_access_revoked(
        self, session: Session, organization_id: str, app_id: str, installation_id: str
    ) -> None:
        logger.info(f"Handling access revocation for installation {installation_id}")

        try:
            self.revoke_access_token(session, organization_id, app_id, installation_id)
        except (ValueError, SQLAlchemyError) as e:
            logger.error(f"Error during revocation cleanup: {e}")

    # Webhook Event Handling

    def handle_webhook_event(
        self,
        session: Session,
        installation_id: str,
        headers: dict,
        body: dict,
        raw_body: bytes | None = None,
    ) -> dict:
        app_install = git_provider_app_installation_by_id(session, installation_id)
        if not app_install:
            logger.error(f"Installation not found for ID {installation_id}")
            raise ValueError("Installation not found")

        git_provider = self.get_provider(app_install.git_provider_app)
        return git_provider.handle_webhook_event(
            headers,
            body,
            WebhookEventContext(
                app_id=str(app_install.git_provider_app_id),
                installation_id=installation_id,
                organization_id=app_install.organization_id,
                session=session,
                raw_body=raw_body,
            ),
        )

    def _handle_bitbucket_webhook(
        self,
        session: Session,
        app_install: GitProviderAppInstallation,
        headers: dict,
        body: dict,
    ) -> dict:
        """Handle Bitbucket webhook with security validation"""
        installation_id = str(app_install.id)
        event_key = headers.get("x-event-key")
        incoming_secret = headers.get("x-hub-signature")
        # webhook_id = headers.get(
        #     "x-hook-uuid"
        # )  # store this during registration to identify the webhook event target

        # For Bitbucket, validate the webhook secret if provided
        secret = self.secrets_manager.read_secret(
            format_secret_name(APP_INSTALL_WAT_NAME_PREFIX, installation_id)
        )
        if (
            secret
            and secret.get("secret_token")
            and incoming_secret
            and secret["secret_token"] != incoming_secret
        ):
            logger.error(
                f"Secret token mismatch for Bitbucket installation ID {installation_id}"
            )
            raise PermissionError("Insufficient permissions")

        # Get provider and handle event
        provider = self.get_provider(app_install.git_provider_app)

        if event_key == "repo:push":
            logger.info("Bitbucket push event")
            return provider.handle_push_event(
                session, str(app_install.git_provider_app_id), installation_id, body
            )

        return {"message": "Event ignored"}


# Create a singleton instance
_service_instance = None


def get_git_provider_service(aws_config: AWSClientConfig) -> GitProviderService:
    """Get or create the GitProviderService singleton"""
    global _service_instance
    if _service_instance is None:
        _service_instance = GitProviderService(aws_config)
    return _service_instance
