import json
import logging
from typing import Any

from app.git_providers.core.config import GitProviderConfig
from app.git_providers.interfaces.provider_interface import (
    GitProviderInterface,
    WebhookConfig,
    WebhookEventContext,
)
from app.git_providers.oauth.gitlab_oauth_strategy import GitLabOAuthStrategy
from app.git_providers.resources.gitlab_resources import GitLabAPIResources
from app.git_providers.utils.vcs_auto_update import is_update_required
from app.schemas.git_provider_schema import (
    AccessTokenData,
    GitProviderAppTokenSecret,
    GitRepository,
    TokenType,
)
from app.schemas.secret_management_schema import (
    APP_INSTALL_GAT_NAME_PREFIX,
)
from database.models import GitProviderApp, GitProviderAppInstallation
from hatchet_sdk import Hatchet
from shared.interfaces.aws_client_config import AWSClientConfig
from shared.interfaces.hatchet_interfaces import HandleGitlabEventsInput
from shared.secret_management.aws_secret_management import (
    AWSSecretManagementStrategy,
    format_secret_name,
)

logger = logging.getLogger(__name__)


class GitLabProvider(GitProviderInterface):
    """GitLab provider implementation supporting Group Access Tokens only"""

    def __init__(
        self, config: GitProviderConfig, secrets_manager: AWSSecretManagementStrategy
    ) -> None:
        self.config = config
        self.secrets_manager = secrets_manager
        self.auth_strategy = GitLabOAuthStrategy(
            config
        )  # Still used for token validation
        self.api_strategy = GitLabAPIResources(config.base_url, config.provider_kind)

    @classmethod
    def from_config(
        cls, app: GitProviderApp, aws_config: AWSClientConfig
    ) -> "GitLabProvider":
        """Create GitLabProvider from app configuration"""
        from app.git_providers.core.config_loader import load_provider_config

        secrets_manager = AWSSecretManagementStrategy(aws_config)

        # GitLab with GAT doesn't need app-level secrets
        config = load_provider_config(app, client_secret=None)

        return cls(config, secrets_manager)

    # TODO: revisit this throw error vs return False
    def validate_access_token(self, token_data: dict) -> tuple[bool, str | None]:
        """Validate GitLab Group Access Token"""
        access_token = AccessTokenData(**token_data)

        if access_token.token_type != TokenType.GROUP_ACCESS_TOKEN:
            return False, "GitLab only supports Group Access Tokens"

        # Validate GAT by attempting to get user info
        try:
            user = self.auth_strategy.token_user(access_token.token)
            return (True, None) if user else (False, "Invalid token")
        except Exception as e:
            logger.error(f"GAT validation failed: {e}")
            return False, str(e)

    def create_installation(
        self, organization_id: str, app_id: str, token_data: dict
    ) -> GitProviderAppInstallation:
        """Create GitLab installation record"""
        access_token = AccessTokenData(**token_data)
        return GitProviderAppInstallation(
            git_provider_app_id=app_id,
            organization_id=organization_id,
            misc_metadata={
                "kind": token_data["token_type"],
                "name": access_token.name,
            },
        )

    def store_secrets(
        self, installation: GitProviderAppInstallation, token_data: dict
    ) -> None:
        """Store GitLab GAT in AWS Secrets Manager"""
        access_token = AccessTokenData(**token_data)

        # Generate webhook secret
        import secrets

        webhook_secret = secrets.token_urlsafe(32)

        secret_key = format_secret_name(
            APP_INSTALL_GAT_NAME_PREFIX, str(installation.id)
        )
        secret_value = json.dumps(
            GitProviderAppTokenSecret(
                token=access_token.token, secret_token=webhook_secret
            ).model_dump()
        )
        self.secrets_manager.write_secret(secret_key, secret_value)
        logger.info(f"Stored GAT for GitLab installation {installation.id}")

    def update_secrets(
        self, installation: GitProviderAppInstallation, token_data: dict
    ) -> None:
        """Update GitLab GAT while preserving webhook secret"""
        access_token = AccessTokenData(**token_data)

        secret_key = format_secret_name(
            APP_INSTALL_GAT_NAME_PREFIX, str(installation.id)
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
            f"Updated GAT for GitLab installation {installation.id}, webhook secret preserved"
        )

    def fetch_secrets(self, installation: GitProviderAppInstallation) -> dict:
        secret_key = format_secret_name(APP_INSTALL_GAT_NAME_PREFIX, installation.id)
        secret_value = self.secrets_manager.read_secret(secret_key)
        if not secret_value:
            raise ValueError(f"GAT not found for installation: {installation.id}")
        return secret_value

    def fetch_repositories(
        self, installation: GitProviderAppInstallation
    ) -> list[GitRepository]:
        """Fetch GitLab repositories using GAT"""
        logger.info(f"Fetching repositories for installation: {installation.id}")

        try:
            access_token = self._fetch_group_access_token(str(installation.id))

            # Use the API strategy to fetch repos
            repos = self.api_strategy.fetch_repos(str(installation.id), access_token)

            return repos

        except Exception as e:
            logger.error(f"Failed to fetch repositories: {e}")
            raise

    def handle_webhook_event(
        self,
        headers: dict,
        payload: dict,
        webhook_event_ctx: WebhookEventContext,
    ) -> dict:
        """Handle GitLab webhook events"""
        installation_id = webhook_event_ctx.installation_id

        event_type = payload["object_kind"]
        logger.info(
            f"Handling GitLab webhook: {event_type} for installation {installation_id}"
        )
        incoming_secret_token = headers.get("x-gitlab-token")

        # Validate secret
        secret_key = format_secret_name(APP_INSTALL_GAT_NAME_PREFIX, installation_id)
        secret = self.secrets_manager.read_secret(secret_key)

        if not secret.get("secret_token"):
            logger.error(f"Secret not found for installation ID {installation_id}")
            raise PermissionError("Insufficient permissions")

        secret_token = secret["secret_token"]
        if secret_token != incoming_secret_token:
            logger.error(f"Secret token mismatch for installation ID {installation_id}")
            raise PermissionError("Insufficient permissions")

        if event_type == "push":
            logger.info("GitLab push event")
            return self._handle_push_event(payload, webhook_event_ctx)
        else:
            logger.warning(
                f"Unhandled GitLab event type: {event_type} for installation {installation_id}"
            )
        return {"message": "Event ignored"}

    def revoke_access(self, installation: GitProviderAppInstallation) -> None:
        """Revoke access for a GitLab installation"""
        logger.info(f"Revoking access for GitLab installation {installation.id}")

        # Delete GAT secret
        secret_key = format_secret_name(
            APP_INSTALL_GAT_NAME_PREFIX, str(installation.id)
        )
        self.secrets_manager.delete_secret(secret_key)
        logger.info(f"Deleted GAT secret for installation {installation.id}")

    def register_webhook(
        self,
        installation: GitProviderAppInstallation,
        config: WebhookConfig,
        scope: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Register webhook - not implemented for GitLab"""
        raise NotImplementedError(
            "Webhook registration is not yet implemented for GitLab. "
            "Please create webhooks manually through the GitLab UI."
        )

    def deregister_webhook(
        self,
        installation: GitProviderAppInstallation,
        webhook_id: str,
    ) -> None:
        """Deregister webhook - not implemented for GitLab"""
        raise NotImplementedError(
            "Webhook deregistration is not yet implemented for GitLab. "
            "Please delete webhooks manually through the GitLab UI."
        )

    # Private helper methods

    def _fetch_group_access_token(self, install_id: str) -> str:
        """Fetch Group Access Token from secrets"""
        secret_key = format_secret_name(APP_INSTALL_GAT_NAME_PREFIX, install_id)
        secret_value = self.secrets_manager.read_secret(secret_key)

        if not secret_value:
            raise ValueError(f"GAT not found for installation: {install_id}")

        return secret_value["token"]

    def _handle_push_event(
        self, body: dict, webhook_event_ctx: WebhookEventContext
    ) -> dict:
        """Handle GitLab push event - moved from routes"""

        installation_id = webhook_event_ctx.installation_id
        organization_id = webhook_event_ctx.organization_id

        repository = body["repository"]
        project = body["project"]
        repo_name = repository["name"]
        repo_id = str(project["id"])
        full_name = project["path_with_namespace"]
        default_branch = project["default_branch"]
        pushed_ref = body["ref"]
        commit_hash = body["after"]

        if pushed_ref != f"refs/heads/{default_branch}":
            logger.info(
                "Push event ignored: Not the default branch. Org: %s, Repo: %s, Ref: %s, Install ID: %s",
                organization_id,
                repo_name,
                pushed_ref,
                installation_id,
            )
            return {"message": "Push event ignored: Not the default branch."}

        logger.info(
            "Push event on default branch. Org: %s, Repo: %s, Branch: %s, Install ID: %s",
            organization_id,
            repo_name,
            default_branch,
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
                    "id": repo_id,
                    "name": repo_name,
                    "repo_name": repo_name,
                    "full_name": full_name,
                    "commit": commit_hash,
                    "metadata": project,
                    "installation_id": installation_id,
                    "latest_commit": {
                        "commit": {
                            "id": commit_hash,
                        },
                    },
                }
            ]
            hatchet = Hatchet()
            handle_gitlab_events_task = hatchet.stubs.task(
                name="handle-gitlab-events-workflow",
                input_validator=HandleGitlabEventsInput,
            )
            handle_gitlab_events_task.run_no_wait(
                HandleGitlabEventsInput(
                    installation_id=installation_id,
                    org_id=organization_id,
                    repos_added=[],
                    repos_deleted=[],
                    repos_pushed=repos_pushed,
                )
            )

        return message
