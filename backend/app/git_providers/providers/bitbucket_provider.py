import json
import logging
from enum import Enum
from typing import Any

from app.git_providers.core.config import GitProviderConfig
from app.git_providers.interfaces.provider_interface import (
    GitProviderInterface,
    WebhookConfig,
    WebhookEventContext,
)
from app.git_providers.resources.bitbucket_api_resources import BitbucketAPIResources
from app.git_providers.utils.branch_tracking import get_tracked_branch_or_none
from app.git_providers.utils.vcs_auto_update import is_update_required
from app.schemas.git_provider_schema import (
    AccessTokenData,
    GitProviderAppTokenSecret,
    GitRepository,
)
from app.schemas.secret_management_schema import APP_INSTALL_WAT_NAME_PREFIX
from database.models import GitProviderApp, GitProviderAppInstallation
from hatchet_sdk import Hatchet
from shared.interfaces.aws_client_config import AWSClientConfig
from shared.interfaces.hatchet_interfaces import HandleBitbucketEventsInput
from shared.secret_management.aws_secret_management import (
    AWSSecretManagementStrategy,
    format_secret_name,
)

logger = logging.getLogger(__name__)


class BitbucketTokenType(str, Enum):
    """Bitbucket-specific token types"""

    WORKSPACE = "workspace_access_token"
    PROJECT = "project_access_token"
    REPOSITORY = "repository_access_token"


class BitbucketProvider(GitProviderInterface):
    """Bitbucket provider implementation supporting multiple access token types"""

    def __init__(
        self, config: GitProviderConfig, secrets_manager: AWSSecretManagementStrategy
    ) -> None:
        self.config = config
        self.secrets_manager = secrets_manager
        self.api_strategy = (
            BitbucketAPIResources()
        )  # config.base_url) < TODO this class has a hardcoded base URL...

    @classmethod
    def from_config(
        cls, app: GitProviderApp, aws_config: AWSClientConfig
    ) -> "BitbucketProvider":
        """Create BitbucketProvider from app configuration"""
        from app.git_providers.core.config_loader import load_provider_config

        secrets_manager = AWSSecretManagementStrategy(aws_config)
        config = load_provider_config(app, client_secret=None)

        return cls(config, secrets_manager)

    def validate_access_token(self, token_data: dict) -> tuple[bool, str | None]:
        """Validate Bitbucket Access Token (Workspace, Project, or Repository)"""
        access_token = AccessTokenData(**token_data)

        try:
            workspace_name = self.config.name
            is_valid, message = self.api_strategy.validate_workspace_access(
                workspace_name, access_token.token
            )
            return (is_valid, None) if is_valid else (False, message)

        except Exception as e:
            logger.error(f"Token validation failed: {e}")
            return False, str(e)

    def create_installation(
        self, organization_id: str, app_id: str, token_data: dict
    ) -> GitProviderAppInstallation:
        access_token = AccessTokenData(**token_data)

        metadata = {
            "kind": token_data["token_type"],
            "name": access_token.name,
        }

        return GitProviderAppInstallation(
            git_provider_app_id=app_id,
            organization_id=organization_id,
            misc_metadata=metadata,
        )

    def store_secrets(
        self, installation: GitProviderAppInstallation, token_data: dict
    ) -> None:
        access_token = AccessTokenData(**token_data)

        # Generate webhook secret
        import secrets

        webhook_secret = secrets.token_urlsafe(32)

        secret_key = format_secret_name(
            APP_INSTALL_WAT_NAME_PREFIX, str(installation.id)
        )
        secret_value = json.dumps(
            GitProviderAppTokenSecret(
                token=access_token.token, secret_token=webhook_secret
            ).model_dump()
        )

        self.secrets_manager.write_secret(secret_key, secret_value)
        logger.info(f"Stored WAT for Bitbucket installation {installation.id}")

    def update_secrets(
        self, installation: GitProviderAppInstallation, token_data: dict
    ) -> None:
        access_token = AccessTokenData(**token_data)

        secret_key = format_secret_name(
            APP_INSTALL_WAT_NAME_PREFIX, str(installation.id)
        )

        # Fetch existing secrets to preserve webhook secret
        existing_secrets = self.secrets_manager.read_secret(secret_key)
        if not existing_secrets or "secret_token" not in existing_secrets:
            raise ValueError(
                f"No existing webhook secret found for installation {installation.id}"
            )

        webhook_secret = existing_secrets["secret_token"]

        # Update only the token, preserve webhook secret
        secret_value = json.dumps(
            GitProviderAppTokenSecret(
                token=access_token.token, secret_token=webhook_secret
            ).model_dump()
        )
        self.secrets_manager.write_secret(secret_key, secret_value)
        logger.info(
            f"Updated WAT for Bitbucket installation {installation.id}, webhook secret preserved"
        )

    def fetch_secrets(self, installation: GitProviderAppInstallation) -> dict:
        secret_key = format_secret_name(
            APP_INSTALL_WAT_NAME_PREFIX, str(installation.id)
        )
        secret_value = self.secrets_manager.read_secret(secret_key)

        if not secret_value:
            raise ValueError(
                f"Access token not found for installation: {installation.id}"
            )

        return secret_value

    def fetch_repositories(
        self, installation: GitProviderAppInstallation
    ) -> list[GitRepository]:
        """Fetch Bitbucket repositories based on token type"""
        logger.info(f"Fetching repositories for installation: {installation.id}")

        try:
            # Get token and metadata from secrets
            secrets = self.fetch_secrets(installation)
            access_token = secrets["token"]
            workspace = installation.git_provider_app.name
            repos_data = self.api_strategy.list_repositories(workspace, access_token)
            repos = []
            for repo in repos_data:
                repos.append(
                    GitRepository(
                        provider_name=str(installation.git_provider_app.provider_kind),
                        provider_kind=installation.git_provider_app.provider_kind,
                        org=repo["workspace"]["name"],
                        installation_id=str(installation.id),
                        repo_name=repo["name"],
                        last_updated=repo.get("updated_on"),
                        default_branch=repo["mainbranch"]["name"],  #
                        latest_commit=None,  # To be fetched on-demand
                        metadata={
                            "id": repo["uuid"],  # Store repo ID in metadata
                            "workspace": repo["workspace"]["name"],
                            "slug": repo["slug"],
                            "project_key": repo.get("project", {}).get("key"),
                            "project_name": repo.get("project", {}).get("name"),
                            "is_private": repo.get("is_private", True),
                            "language": repo.get("language"),
                            "created_on": repo.get("created_on"),
                            "updated_on": repo.get("updated_on"),
                            "full_name": repo["full_name"],
                            "links": repo.get("links", {}),
                        },
                    )
                )

            logger.info(
                f"Fetched {len(repos)} repositories for installation {installation.id}"
            )
            return repos

        except Exception as e:
            logger.error(f"Failed to fetch repositories: {e}")
            raise

    # https://support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/
    def handle_webhook_event(
        self,
        headers: dict,
        payload: dict,
        webhook_event_ctx: WebhookEventContext,
    ) -> dict:
        """Handle Bitbucket webhook events"""
        installation_id = webhook_event_ctx.installation_id

        event_type = headers["x-event-key"]
        logger.info(
            f"Handling Bitbucket webhook: {event_type} for installation {installation_id}"
        )

        # Bitbucket event types are prefixed (e.g., "repo:push")
        if event_type == "repo:push":
            return self._handle_push_event(payload, webhook_event_ctx)
        elif event_type == "pullrequest:created" or event_type == "pullrequest:updated":
            # TODO: Handle pull request events
            return self._handle_pull_request_event()
        else:
            logger.info(f"Ignoring Bitbucket event type: {event_type}")
        return {"message": "Event ignored"}

    def revoke_access(self, installation: GitProviderAppInstallation) -> None:
        """Revoke access for a Bitbucket installation"""
        logger.info(f"Revoking access for Bitbucket installation {installation.id}")

        # Delete access token secret
        secret_key = format_secret_name(
            APP_INSTALL_WAT_NAME_PREFIX, str(installation.id)
        )
        self.secrets_manager.delete_secret(secret_key)
        logger.info(f"Deleted access token secret for installation {installation.id}")

    def register_webhook(
        self,
        installation: GitProviderAppInstallation,
        config: WebhookConfig,
        scope: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Register a Bitbucket webhook with installation ID as query param"""
        logger.info(f"Registering webhook for Bitbucket installation {installation.id}")

        try:
            # Add installation_id as query parameter
            callback_url = config.callback_url
            separator = "&" if "?" in callback_url else "?"
            callback_url = f"{callback_url}{separator}installation_id={installation.id}"

            # Use stored or provided secret token
            secrets = self.fetch_secrets(installation)
            secret_token = config.secret_token or secrets.get("secret_token")

            # Map triggers to Bitbucket events
            bitbucket_events = self._map_triggers_to_events(config.triggers)

            # Get access token
            access_token = secrets["token"]

            # Create webhook configuration
            webhook_config = {
                "url": callback_url,
                "events": bitbucket_events,
                "secret": secret_token,
                "active": True,
                "description": config.description or "DriverAI Webhook",
            }

            # Create webhook based on scope
            if scope and scope.get("type") == "repository":
                webhook_data = self.api_strategy.create_repository_webhook(
                    workspace=installation.git_provider_app.name,
                    repo_slug=scope["slug"],
                    config=webhook_config,
                    access_token=access_token,
                )
            else:
                # Workspace-level webhook
                webhook_data = self.api_strategy.create_workspace_webhook(
                    workspace=installation.git_provider_app.name,
                    config=webhook_config,
                    access_token=access_token,
                )

            # Store webhook metadata
            self._store_webhook_metadata(
                installation,
                {
                    "webhook_id": webhook_data["uuid"],
                    "callback_url": callback_url,
                    "custom_headers": config.custom_headers,  # Stored for reference
                    "ssl_verification": config.ssl_verification,
                    "scope": scope,
                },
            )

            logger.info(
                f"Successfully registered webhook {webhook_data['uuid']} for installation {installation.id}"
            )

            return {
                "id": webhook_data["uuid"],
                "callback_url": callback_url,
                "triggers": config.triggers,
                "active": webhook_data["active"],
                "created_at": webhook_data.get("created_at"),
                "provider_specific": webhook_data,
            }

        except Exception as e:
            logger.error(f"Failed to register webhook: {e}")
            raise

    def deregister_webhook(
        self,
        installation: GitProviderAppInstallation,
        webhook_id: str,
    ) -> None:
        """Deregister webhook - not implemented for Bitbucket Cloud"""
        raise NotImplementedError(
            "Webhook deregistration is not yet implemented for Bitbucket Cloud. "
            "Please delete webhooks manually through the Bitbucket UI."
        )

    def fetch_secrets_by_id(self, installation_id: str) -> dict:
        """Fetch secrets by installation ID"""
        secret_key = format_secret_name(APP_INSTALL_WAT_NAME_PREFIX, installation_id)
        secret_value = self.secrets_manager.read_secret(secret_key)

        if not secret_value:
            raise ValueError(
                f"Access token not found for installation: {installation_id}"
            )

        # Handle both old format (direct token) and new format (dict)
        if isinstance(secret_value, str):
            # Legacy format - assume workspace token
            return {
                "token": secret_value,
                "token_type": BitbucketTokenType.WORKSPACE.value,
            }

        return secret_value

    # Private helper methods

    def _get_default_branch(
        self,
        workspace: str,
        repo_slug: str,
        access_token: str,
        repository_data: dict | None = None,
    ) -> str:
        """Get default branch name for a repository, fetching from API if needed"""
        # First check if it's in the provided repository data
        if repository_data:
            default_branch = repository_data.get("mainbranch", {}).get("name")
            if default_branch:
                return default_branch

        # If not, fetch from API
        try:
            repo_data = self.api_strategy.get_repository(
                workspace, repo_slug, access_token
            )
            if repo_data:
                return repo_data.get("mainbranch", {}).get("name", "main")
        except Exception as e:
            logger.warning(f"Failed to fetch default branch for {repo_slug}: {e}")

        # Fallback to 'main'
        return "main"

    def _handle_push_event(
        self, body: dict, webhook_event_ctx: WebhookEventContext
    ) -> dict:
        """Handle Bitbucket push event - moved from routes"""
        installation_id = webhook_event_ctx.installation_id
        organization_id = webhook_event_ctx.organization_id

        push = body.get("push", {})
        repository = body.get("repository", {})
        changes = push.get("changes", [])

        # Extract repository info
        repo_name = repository.get("name")
        repo_id = repository.get("uuid")
        workspace = repository["workspace"]["name"]
        full_name = repository.get("full_name")
        repo_slug = repository.get("slug", repo_name)  # Use name as fallback

        message = {"message": ""}

        # Get access token for API calls
        try:
            secrets = self.fetch_secrets_by_id(installation_id)
            access_token = secrets["token"]
        except Exception as e:
            logger.error(
                f"Failed to fetch access token for installation {installation_id}: {e}"
            )
            return {"message": "Failed to process push event: missing access token"}

        tracked_branch = get_tracked_branch_or_none(
            session=webhook_event_ctx.session,
            org_id=organization_id,
            repo_name=repo_name,
        )
        if not tracked_branch:
            tracked_branch = self._get_default_branch(
                workspace, repo_slug, access_token, repository
            )

        # Process all branch changes
        for change in changes:
            if change.get("new", {}).get("type") == "branch":
                branch_name = change["new"]["name"]
                commit_hash = change["new"]["target"]["hash"]

                if branch_name != tracked_branch:
                    logger.info(
                        "Push event ignored: Not the default branch. Workspace: %s, Repo: %s, Branch: %s, Install ID: %s",
                        workspace,
                        repo_name,
                        branch_name,
                        installation_id,
                    )
                    message = ({"message": "Push event ignored (not default branch)"},)
                    continue

                logger.info(
                    "Push event on default branch. Workspace: %s, Repo: %s, Branch: %s, Install ID: %s",
                    workspace,
                    repo_name,
                    branch_name,
                    installation_id,
                )

                process_update, message = is_update_required(
                    session=webhook_event_ctx.session,
                    org_id=organization_id,
                    repo_name=repo_name,
                )

                if process_update:
                    repos_pushed = [
                        {
                            "repo_id": repo_id,
                            "repo_name": repo_name,
                            "full_name": full_name,
                            "commit": commit_hash,
                            "metadata": {
                                "id": repo_id,  # Add the repo UUID to metadata
                                "uuid": repo_id,  # Also add as uuid for compatibility
                                "workspace": workspace,
                                "slug": repo_name,
                            },
                            "installation_id": installation_id,
                            "latest_commit": {
                                "id": commit_hash,
                            },
                            "workspace": workspace,  # Add workspace at top level too
                            "tracked_branch": tracked_branch,
                        }
                    ]
                    hatchet = Hatchet()
                    handle_bitbucket_events_task = hatchet.stubs.task(
                        name="handle-bitbucket-events-workflow",
                        input_validator=HandleBitbucketEventsInput,
                    )

                    handle_bitbucket_events_task.run_no_wait(
                        HandleBitbucketEventsInput(
                            installation_id=installation_id,
                            org_id=organization_id,
                            repos_added=[],
                            repos_deleted=[],
                            repos_pushed=repos_pushed,
                        )
                    )

        # NOTE: This message will pertain only to the last 'change' processed
        return message

    def _handle_pull_request_event(
        self, payload: dict, webhook_event_ctx: WebhookEventContext
    ) -> dict:
        """Handle pull request webhook event"""
        # TODO: Implement pull request event handling
        # TODO: in push tech docs cancel open pull requests before creating new ones
        return {"message": "Event ignored: pull request events not implemented"}

    def _map_triggers_to_events(self, triggers: list[str]) -> list[str]:
        """Map generic triggers to Bitbucket-specific events"""
        TRIGGER_MAP = {
            "push events": "repo:push",
            "pull request events": ["pullrequest:created", "pullrequest:updated"],
            "merge events": "pullrequest:fulfilled",
            "fork events": "repo:fork",
        }

        events = []
        for trigger in triggers:
            # Use exact match if it's already a Bitbucket event
            if trigger in [
                "repo:push",
                "pullrequest:created",
                "pullrequest:updated",
                "pullrequest:fulfilled",
                "repo:fork",
            ]:
                events.append(trigger)
            else:
                # Map generic trigger
                mapped = TRIGGER_MAP.get(trigger, trigger)
                if isinstance(mapped, list):
                    events.extend(mapped)
                else:
                    events.append(mapped)

        return list(set(events))  # Remove duplicates

    def _store_webhook_metadata(
        self, installation: GitProviderAppInstallation, webhook_info: dict[str, Any]
    ) -> None:
        """Store webhook metadata in installation"""
        from datetime import datetime

        metadata = installation.misc_metadata or {}
        webhooks = metadata.get("webhooks", [])

        webhooks.append(
            {
                **webhook_info,
                "created_at": datetime.utcnow().isoformat(),
            }
        )

        metadata["webhooks"] = webhooks
        # Note: In a real implementation, you would update the installation
        # record in the database here
        logger.info(
            f"Stored webhook metadata for installation {installation.id}: {webhook_info['webhook_id']}"
        )
