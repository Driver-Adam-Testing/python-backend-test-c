import hashlib
import hmac
import json
import logging
import secrets
from typing import Any, ClassVar

from app.git_providers.core.config import GitProviderConfig
from app.git_providers.core.config_loader import load_provider_config
from app.git_providers.interfaces.provider_interface import (
    GitProviderInterface,
    WebhookConfig,
    WebhookEventContext,
)
from app.git_providers.resources.bitbucket_dc_api_resources import (
    BitbucketDCAPIResources,
)
from app.git_providers.utils.branch_tracking import get_tracked_branch_or_none
from app.git_providers.utils.vcs_auto_update import is_update_required
from app.schemas.git_provider_schema import (
    BitbucketDCTokenData,
    GitRepository,
)
from app.schemas.secret_management_schema import APP_INSTALL_BBDC_HTTP_NAME_PREFIX
from database.models import GitProviderApp, GitProviderAppInstallation
from hatchet_sdk import Hatchet
from shared.interfaces.aws_client_config import AWSClientConfig
from shared.interfaces.hatchet_interfaces import HandleBitbucketDCEventsInput
from shared.secret_management.aws_secret_management import (
    AWSSecretManagementStrategy,
    format_secret_name,
)

logger = logging.getLogger(__name__)


class BitbucketDCProvider(GitProviderInterface):
    """Bitbucket Data Center/Server provider implementation.

    Key differences from Bitbucket Cloud:
    - Uses HTTP Access Tokens (not Workspace Access Tokens)
    - API base: {instance}/rest/api/1.0
    - Clone URL requires actual username (not x-token-auth)
    - Push event: repo:refs_changed
    - PR merged event: pr:merged
    - May use self-signed SSL certificates
    """

    _TRIGGER_MAP: ClassVar[dict[str, str | list[str]]] = {
        "push events": "repo:refs_changed",
        "pull request events": ["pr:opened", "pr:modified"],
        "merge events": "pr:merged",
    }

    def __init__(
        self,
        config: GitProviderConfig,
        secrets_manager: AWSSecretManagementStrategy,
    ) -> None:
        self.config = config
        self.secrets_manager = secrets_manager
        self.api_resources: BitbucketDCAPIResources | None = None

    def _get_api_resources(
        self,
        base_url: str,
        ca_bundle_path: str | None = None,
        disable_ssl_verify: bool = False,
    ) -> BitbucketDCAPIResources:
        if not self.api_resources:
            self.api_resources = BitbucketDCAPIResources(
                base_url=base_url,
                ca_bundle_path=ca_bundle_path,
                disable_ssl_verify=disable_ssl_verify,
            )

        return self.api_resources

    @classmethod
    def from_config(
        cls, app: GitProviderApp, aws_config: AWSClientConfig
    ) -> "BitbucketDCProvider":
        """Create BitbucketDCProvider from app configuration."""
        secrets_manager = AWSSecretManagementStrategy(aws_config)
        config = load_provider_config(app, client_secret=None)

        return cls(config, secrets_manager)

    def validate_access_token(
        self, token_data: dict[str, Any]
    ) -> tuple[bool, str | None]:  # TODO: Revisit with a pydantic model.
        """Validate token using Bearer auth (no username needed for Project/Repository tokens)."""
        try:
            token = token_data.get("token")
            if not token:
                return False, "Token is required"

            base_url = self.config.base_url
            if not base_url:
                return False, "base_url is required"

            api = self._get_api_resources(
                base_url=base_url,
                ca_bundle_path=token_data.get("ca_bundle_path"),
                disable_ssl_verify=token_data.get("disable_ssl_verify", False),
            )

            # Validate token using Bearer auth (no username needed)
            is_valid, message = api.validate_token(token)
            return (is_valid, None) if is_valid else (False, message)
        except ValueError as e:
            return False, str(e)

        except Exception as e:
            logger.error(f"Token validation failed: {e}")
            return False, str(e)

    def create_installation(
        self, organization_id: str, app_id: str, token_data: dict[str, Any]
    ) -> GitProviderAppInstallation:
        dc_token = BitbucketDCTokenData(**token_data)

        metadata = {
            "kind": dc_token.token_type.value,  # project_access_token or repository_access_token
            "name": dc_token.name,
        }  # TODO: Revisit with a pydantic model.

        # Store scope information based on token type
        if dc_token.project_key:
            metadata["project_key"] = dc_token.project_key
        if dc_token.repo_slug:
            metadata["repo_slug"] = dc_token.repo_slug

        return GitProviderAppInstallation(
            git_provider_app_id=app_id,
            organization_id=organization_id,
            misc_metadata=metadata,
        )

    def store_secrets(
        self, installation: GitProviderAppInstallation, token_data: dict[str, Any]
    ) -> None:
        dc_token = BitbucketDCTokenData(**token_data)

        # Generate webhook secret
        webhook_secret = secrets.token_urlsafe(32)

        secret_key = format_secret_name(
            APP_INSTALL_BBDC_HTTP_NAME_PREFIX, str(installation.id)
        )

        secret_data = {
            "token": dc_token.token,
            "token_type": dc_token.token_type.value,
            "secret_token": webhook_secret,
            "ca_bundle_path": dc_token.ca_bundle_path,
            "disable_ssl_verify": dc_token.disable_ssl_verify,
        }

        # Store scope for webhook registration
        if dc_token.project_key:
            secret_data["project_key"] = dc_token.project_key
        if dc_token.repo_slug:
            secret_data["repo_slug"] = dc_token.repo_slug

        self.secrets_manager.write_secret(secret_key, json.dumps(secret_data))
        logger.info(
            f"Stored HTTP Access Token for Bitbucket DC installation {installation.id}"
        )

    def update_secrets(
        self, installation: GitProviderAppInstallation, token_data: dict[str, Any]
    ) -> None:
        dc_token = BitbucketDCTokenData(**token_data)

        secret_key = format_secret_name(
            APP_INSTALL_BBDC_HTTP_NAME_PREFIX, str(installation.id)
        )

        # Fetch existing secrets to preserve webhook secret and token metadata
        existing_secrets = self.secrets_manager.read_secret(secret_key)
        if not existing_secrets or "secret_token" not in existing_secrets:
            raise ValueError(
                f"No existing webhook secret found for installation {installation.id}"
            )

        # Start with existing secrets to preserve token_type, project_key, repo_slug
        updated_secrets = dict(existing_secrets)

        # Update with new token data (only overwrite fields that are provided)
        updated_secrets["token"] = dc_token.token
        updated_secrets["ca_bundle_path"] = dc_token.ca_bundle_path
        updated_secrets["disable_ssl_verify"] = dc_token.disable_ssl_verify

        # Update token_type only if explicitly provided in token_data (check original dict,
        # not parsed model, since model has a default value)
        if "token_type" in token_data:
            updated_secrets["token_type"] = dc_token.token_type.value

        # Update scope fields only if explicitly provided in token_data
        if "project_key" in token_data:
            updated_secrets["project_key"] = dc_token.project_key
        if "repo_slug" in token_data:
            updated_secrets["repo_slug"] = dc_token.repo_slug

        self.secrets_manager.write_secret(secret_key, json.dumps(updated_secrets))
        logger.info(
            f"Updated HTTP Access Token for Bitbucket DC installation {installation.id}"
        )

    def fetch_secrets(self, installation: GitProviderAppInstallation) -> dict[str, Any]:
        secret_key = format_secret_name(
            APP_INSTALL_BBDC_HTTP_NAME_PREFIX, str(installation.id)
        )
        secret_value = self.secrets_manager.read_secret(secret_key)

        if not secret_value:
            raise ValueError(
                f"Access token not found for installation: {installation.id}"
            )

        return secret_value

    def fetch_secrets_by_id(self, installation_id: str) -> dict[str, Any]:
        secret_key = format_secret_name(
            APP_INSTALL_BBDC_HTTP_NAME_PREFIX, installation_id
        )
        secret_value = self.secrets_manager.read_secret(secret_key)

        if not secret_value:
            raise ValueError(
                f"Access token not found for installation: {installation_id}"
            )

        return secret_value

    def fetch_repositories(
        self, installation: GitProviderAppInstallation
    ) -> list[GitRepository]:
        logger.info(f"Fetching repositories for installation: {installation.id}")

        secrets = self.fetch_secrets(installation)
        access_token = secrets["token"]
        base_url = installation.git_provider_app.base_url
        api = self._get_api_resources(
            base_url=base_url,
            ca_bundle_path=secrets.get("ca_bundle_path"),
            disable_ssl_verify=secrets.get("disable_ssl_verify", False),
        )

        try:
            repos_data = api.list_repositories(access_token)
        except Exception as e:
            e.add_note("API call to list repositories failed.")
            raise

        repos = []
        for repo in repos_data:
            project_key = repo.get("project", {}).get("key")
            repo_slug = repo.get("slug")

            default_branch = None
            try:
                default_branch = api.get_default_branch(
                    project_key, repo_slug, access_token
                )
            except Exception as e:
                logger.warning(
                    f"Failed to fetch default branch for {project_key}/{repo_slug}: {e}"
                )

            if not default_branch:
                logger.warning(
                    f"Skipping {project_key}/{repo_slug}: no default branch available"
                )
                continue

            repos.append(
                GitRepository(
                    provider_name=str(installation.git_provider_app.provider_kind),
                    provider_kind=installation.git_provider_app.provider_kind,
                    org=project_key,
                    installation_id=str(installation.id),
                    repo_name=repo.get("name"),
                    last_updated=None,
                    default_branch=default_branch,
                    latest_commit=None,
                    metadata={
                        "id": repo.get("id"),
                        "project_key": project_key,
                        "project_name": repo.get("project", {}).get("name"),
                        "slug": repo_slug,
                        "is_public": repo.get("public", False),
                        "clone_url": repo.get("links", {})
                        .get("clone", [{}])[0]
                        .get("href")
                        if repo.get("links", {}).get("clone")
                        else None,
                        "instance_url": base_url,
                    },
                )
            )

        logger.info(
            f"Fetched {len(repos)} repositories for installation {installation.id}"
        )
        return repos

    def _verify_webhook_signature(
        self,
        raw_body: bytes,
        secret_token: str,
        signature_header: str | None,
    ) -> bool:
        """Verify HMAC-SHA256 signature. Expects X-Hub-Signature header format: sha256=<hex_digest>."""
        if not signature_header:
            logger.warning("Missing X-Hub-Signature header for Bitbucket DC webhook")
            return False

        if not signature_header.startswith("sha256="):
            logger.warning(f"Invalid signature format: {signature_header}")
            return False

        provided_signature = signature_header[7:]  # Remove "sha256=" prefix

        expected_signature = hmac.new(
            secret_token.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()

        # Constant-time comparison to prevent timing attacks
        return hmac.compare_digest(expected_signature, provided_signature)

    def handle_webhook_event(
        self,
        headers: dict[str, Any],
        payload: dict[str, Any],
        webhook_event_ctx: WebhookEventContext,
    ) -> dict[str, Any]:
        """Handle Bitbucket DC webhook events (repo:refs_changed for push, pr:merged for PR merge)."""
        installation_id = webhook_event_ctx.installation_id

        # Verify webhook signature (HMAC-SHA256)
        secrets_data = self.fetch_secrets_by_id(installation_id)
        secret_token = secrets_data.get("secret_token")

        if not secret_token:
            logger.error(f"No webhook secret found for installation {installation_id}")
            raise PermissionError("Webhook secret not configured")

        raw_body = webhook_event_ctx.raw_body
        if raw_body is None:
            logger.error("Raw body not available for signature verification")
            raise PermissionError("Unable to verify webhook signature")

        signature_header = headers.get("x-hub-signature")
        if not self._verify_webhook_signature(raw_body, secret_token, signature_header):
            logger.error(
                f"Webhook signature verification failed for installation {installation_id}"
            )
            raise PermissionError("Invalid webhook signature")

        event_type = headers.get("x-event-key", "")
        logger.info(
            f"Handling Bitbucket DC webhook: {event_type} for installation {installation_id}"
        )

        if event_type == "repo:refs_changed":
            return self._handle_push_event(payload, webhook_event_ctx)
        elif event_type == "pr:merged":
            return self._handle_pr_merged_event(payload, webhook_event_ctx)
        else:
            logger.info(f"Ignoring Bitbucket DC event type: {event_type}")
            return {"message": "Event ignored"}

    def _handle_push_event(
        self, payload: dict[str, Any], webhook_event_ctx: WebhookEventContext
    ) -> dict[str, Any]:
        installation_id = webhook_event_ctx.installation_id
        organization_id = webhook_event_ctx.organization_id

        installation = webhook_event_ctx.session.get(
            GitProviderAppInstallation, installation_id
        )
        if not installation:
            logger.error(f"Installation not found: {installation_id}")
            return {"message": "Failed to process push event: installation not found"}

        repo_info = self._extract_repo_info(payload.get("repository", {}))
        changes = payload.get("changes", [])

        context = self._build_push_context(
            installation_id, installation.git_provider_app.base_url
        )
        if not context:
            return {"message": "Failed to process push event: missing access token"}

        tracked_branch = self._resolve_tracked_branch(
            webhook_event_ctx, organization_id, repo_info, context
        )
        if not tracked_branch:
            logger.warning(
                f"Cannot process push event for {repo_info['project_key']}/{repo_info['repo_slug']}: "
                "no tracked branch configured and unable to determine default branch"
            )
            return {"message": "Push event ignored: unable to determine tracked branch"}

        return self._process_branch_changes(
            changes,
            tracked_branch,
            repo_info,
            context,
            installation_id,
            organization_id,
            webhook_event_ctx,
        )

    def _extract_repo_info(self, repository: dict) -> dict[str, Any]:
        return {
            "project_key": repository.get("project", {}).get("key"),
            "repo_slug": repository.get("slug"),
            "repo_name": repository.get("name"),
            "repo_id": repository.get("id"),
        }

    def _build_push_context(
        self, installation_id: str, base_url: str
    ) -> dict[str, Any] | None:
        try:
            secrets = self.fetch_secrets_by_id(installation_id)
        except Exception as e:
            logger.error(
                f"Failed to fetch access token for installation {installation_id}: {e}"
            )
            return None

        api = self._get_api_resources(
            base_url=base_url,
            ca_bundle_path=secrets.get("ca_bundle_path"),
            disable_ssl_verify=secrets.get("disable_ssl_verify", False),
        )

        return {
            "secrets": secrets,
            "api": api,
            "access_token": secrets["token"],
            "base_url": base_url,
        }

    def _resolve_tracked_branch(
        self,
        webhook_event_ctx: WebhookEventContext,
        organization_id: str,
        repo_info: dict[str, Any],
        context: dict[str, Any],
    ) -> str | None:
        tracked_branch = get_tracked_branch_or_none(
            session=webhook_event_ctx.session,
            org_id=organization_id,
            repo_name=repo_info["repo_name"],
        )
        if tracked_branch:
            return tracked_branch

        try:
            return context["api"].get_default_branch(
                repo_info["project_key"],
                repo_info["repo_slug"],
                context["access_token"],
            )
        except Exception as e:
            logger.warning(
                f"Failed to get default branch for {repo_info['project_key']}/{repo_info['repo_slug']}: {e}"
            )
            return None

    def _process_branch_changes(
        self,
        changes: list[dict[str, Any]],
        tracked_branch: str,
        repo_info: dict[str, Any],
        context: dict[str, Any],
        installation_id: str,
        organization_id: str,
        webhook_event_ctx: WebhookEventContext,
    ) -> dict[str, str]:
        message: dict[str, str] = {"message": ""}

        for change in changes:
            ref = change.get("ref", {})
            if ref.get("type") != "BRANCH":
                continue

            branch_name = ref.get("displayId")
            if branch_name != tracked_branch:
                logger.info(
                    f"Push event ignored: Not tracked branch. Project: {repo_info['project_key']}, "
                    f"Repo: {repo_info['repo_name']}, Branch: {branch_name}, Tracked: {tracked_branch}"
                )
                message = {"message": "Push event ignored (not tracked branch)"}
                continue

            logger.info(
                f"Push event on tracked branch. Project: {repo_info['project_key']}, "
                f"Repo: {repo_info['repo_name']}, Branch: {branch_name}"
            )

            result = self._maybe_dispatch_push_update(
                change,
                tracked_branch,
                repo_info,
                context,
                installation_id,
                organization_id,
                webhook_event_ctx,
            )
            if result:
                return result
            message = (
                {"message": result} if isinstance(result, str) else message
            )  # TODO: Remove dead code.

        return message

    def _maybe_dispatch_push_update(
        self,
        change: dict[str, Any],
        tracked_branch: str,
        repo_info: dict[str, Any],
        context: dict[str, Any],
        installation_id: str,
        organization_id: str,
        webhook_event_ctx: WebhookEventContext,
    ) -> dict[str, str]:
        process_update, update_msg = is_update_required(
            session=webhook_event_ctx.session,
            org_id=organization_id,
            repo_name=repo_info["repo_name"],
        )

        if not process_update:
            return {"message": update_msg}

        repos_pushed = [
            self._build_repo_pushed_entry(
                change.get("toHash"),
                tracked_branch,
                repo_info,
                context["base_url"],
                installation_id,
            )
        ]
        self._dispatch_bitbucket_dc_event(
            installation_id, organization_id, repos_pushed
        )
        return {"message": "Push event processed"}

    def _build_repo_pushed_entry(
        self,
        commit_hash: str | None,
        tracked_branch: str,
        repo_info: dict[str, Any],
        base_url: str,
        installation_id: str,
    ) -> dict[str, Any]:
        return {
            "repo_id": repo_info["repo_id"],
            "repo_name": repo_info["repo_name"],
            "project_key": repo_info["project_key"],
            "repo_slug": repo_info["repo_slug"],
            "commit": commit_hash,
            "tracked_branch": tracked_branch,
            "installation_id": installation_id,
            "metadata": {
                "id": repo_info["repo_id"],
                "project_key": repo_info["project_key"],
                "slug": repo_info["repo_slug"],
                "instance_url": base_url,
            },
        }

    def _handle_pr_merged_event(
        self, payload: dict[str, Any], webhook_event_ctx: WebhookEventContext
    ) -> dict[str, Any]:
        installation_id = webhook_event_ctx.installation_id
        organization_id = webhook_event_ctx.organization_id

        installation = webhook_event_ctx.session.get(
            GitProviderAppInstallation, installation_id
        )
        if not installation:
            logger.error(f"Installation not found: {installation_id}")
            return {
                "message": "Failed to process PR merged event: installation not found"
            }

        pull_request = payload.get("pullRequest", {})
        to_ref = pull_request.get("toRef", {})
        repo_info = self._extract_repo_info(to_ref.get("repository", {}))
        target_branch = to_ref.get("displayId")

        merge_commit = self._extract_merge_commit(pull_request)
        if not merge_commit:
            logger.warning("PR merged event missing merge commit ID")
            return {"message": "PR merged event ignored: no merge commit"}

        base_url = installation.git_provider_app.base_url

        tracked_branch = get_tracked_branch_or_none(
            session=webhook_event_ctx.session,
            org_id=organization_id,
            repo_name=repo_info["repo_name"],
        )

        if tracked_branch and target_branch != tracked_branch:
            logger.info(f"PR merged to non-tracked branch: {target_branch}")
            return {"message": "PR merged to non-tracked branch"}

        process_update, update_msg = is_update_required(
            session=webhook_event_ctx.session,
            org_id=organization_id,
            repo_name=repo_info["repo_name"],
        )

        if not process_update:
            return {"message": update_msg}

        repos_pushed = [
            self._build_repo_pushed_entry(
                merge_commit, target_branch, repo_info, base_url, installation_id
            )
        ]
        self._dispatch_bitbucket_dc_event(
            installation_id, organization_id, repos_pushed
        )
        return {"message": "PR merged event processed"}

    def _extract_merge_commit(self, pull_request: dict) -> str | None:
        return pull_request.get("properties", {}).get("mergeCommit", {}).get("id")

    def _dispatch_bitbucket_dc_event(
        self,
        installation_id: str,
        organization_id: str,
        repos_pushed: list[dict[str, Any]],
    ) -> None:
        hatchet = Hatchet()
        handle_dc_events_task = hatchet.stubs.task(
            name="handle-bitbucket-dc-events-workflow",
            input_validator=HandleBitbucketDCEventsInput,
        )
        handle_dc_events_task.run_no_wait(
            HandleBitbucketDCEventsInput(
                installation_id=installation_id,
                org_id=organization_id,
                repos_added=[],
                repos_deleted=[],
                repos_pushed=repos_pushed,
            )
        )

    def revoke_access(self, installation: GitProviderAppInstallation) -> None:
        logger.info(f"Revoking access for Bitbucket DC installation {installation.id}")

        secret_key = format_secret_name(
            APP_INSTALL_BBDC_HTTP_NAME_PREFIX, str(installation.id)
        )
        self.secrets_manager.delete_secret(secret_key)
        logger.info(f"Deleted access token secret for installation {installation.id}")

    def discover_token_scope(
        self,
        installation: GitProviderAppInstallation,
    ) -> dict[str, str]:
        secrets = self.fetch_secrets(installation)
        token_type = installation.misc_metadata["kind"]

        api = self._get_api_resources(
            base_url=installation.git_provider_app.base_url,
            ca_bundle_path=secrets.get("ca_bundle_path"),
            disable_ssl_verify=secrets.get("disable_ssl_verify", False),
        )

        repos = api.list_repositories(secrets["token"], limit=1)

        if not repos:
            raise ValueError("Token has no accessible repositories")

        project_key = repos[0]["project"]["key"]

        if token_type == "repository_access_token":
            return {
                "type": "repository",
                "project_key": project_key,
                "repo_slug": repos[0]["slug"],
            }
        else:
            return {
                "type": "project",
                "project_key": project_key,
            }

    def register_webhook(
        self,
        installation: GitProviderAppInstallation,
        config: WebhookConfig,
        scope: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Register a Bitbucket DC webhook.

        Supports both project-level and repository-level webhooks based on scope:
        - scope["type"] == "project": Creates webhook for all repos in project
        - scope["type"] == "repository": Creates webhook for specific repo

        If scope is None, auto-discovers from token type using discover_token_scope().

        The token_type in secrets determines what's allowed:
        - project_access_token: Can create both project and repo webhooks
        - repository_access_token: Can only create repo webhooks

        Persists webhook scope info to installation.misc_metadata for later deregistration.
        """
        logger.info(
            f"Registering webhook for Bitbucket DC installation {installation.id}"
        )

        secrets = self.fetch_secrets(installation)
        access_token = secrets["token"]
        token_type = secrets.get("token_type", "project_access_token")
        secret_token = config.secret_token or secrets.get("secret_token")

        api = self._get_api_resources(
            base_url=installation.git_provider_app.base_url,
            ca_bundle_path=secrets.get("ca_bundle_path"),
            disable_ssl_verify=secrets.get("disable_ssl_verify", False),
        )

        # Auto-discover scope if not provided
        if scope is None:
            scope = self.discover_token_scope(installation)

        callback_url = self._build_webhook_callback_url(
            config.callback_url, installation.id
        )
        webhook_config = self._build_webhook_config(config, callback_url, secret_token)
        scope_type = scope.get("type")

        webhook_data = self._create_webhook_by_scope(
            api,
            scope_type,
            scope,
            token_type,
            webhook_config,
            access_token,
            installation.id,
        )

        # Extract scope info for persistence
        webhook_id = webhook_data["id"]
        project_key = scope.get("project_key")
        repo_slug = scope.get("slug") or scope.get("repo_slug")

        # Persist webhook info to installation metadata for deregistration
        metadata = dict(installation.misc_metadata or {})
        metadata["webhook_id"] = webhook_id
        metadata["webhook_project_key"] = project_key
        metadata["webhook_repo_slug"] = repo_slug
        metadata["webhook_scope_type"] = scope_type
        installation.misc_metadata = metadata

        return {
            "id": webhook_id,
            "callback_url": callback_url,
            "triggers": config.triggers,
            "scope_type": scope_type,
            "active": webhook_data.get("active", True),
            "provider_specific": webhook_data,
        }

    def _build_webhook_callback_url(self, base_url: str, installation_id: Any) -> str:
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}installation_id={installation_id}"

    def _build_webhook_config(
        self, config: WebhookConfig, callback_url: str, secret_token: str | None
    ) -> dict[str, Any]:
        dc_events = self._map_triggers_to_events(config.triggers)
        return {
            "url": callback_url,
            "events": dc_events,
            "secret": secret_token,
            "active": True,
            "description": config.description or "Driver AI Webhook",
        }

    def _create_webhook_by_scope(
        self,
        api: BitbucketDCAPIResources,
        scope_type: str | None,
        scope: dict[str, str] | None,
        token_type: str,
        webhook_config: dict[str, Any],
        access_token: str,
        installation_id: Any,
    ) -> dict[str, Any]:
        if scope_type == "project":
            return self._create_project_webhook(
                api, scope, token_type, webhook_config, access_token, installation_id
            )
        elif scope_type == "repository":
            return self._create_repository_webhook(
                api, scope, webhook_config, access_token, installation_id
            )
        else:
            raise ValueError(
                f"Invalid scope type: {scope_type}. Must be 'project' or 'repository'"
            )

    def _create_project_webhook(
        self,
        api: BitbucketDCAPIResources,
        scope: dict[str, str] | None,
        token_type: str,  # TODO: Revisit type.
        webhook_config: dict[str, Any],
        access_token: str,
        installation_id: Any,
    ) -> dict[str, Any]:  # T
        if token_type == "repository_access_token":
            raise ValueError(
                "Repository access tokens cannot create project-level webhooks. "
                "Use a project access token or create repository-level webhooks."
            )

        project_key = scope.get("project_key") if scope else None
        if not project_key:
            raise ValueError("project_key is required for project webhooks")

        try:
            webhook_data = api.create_project_webhook(
                project_key=project_key,
                config=webhook_config,
                access_token=access_token,
            )
        except Exception as e:
            e.add_note("API call to create project webhook failed")
            raise

        logger.info(
            f"Created project webhook for {project_key} on installation {installation_id}"
        )
        return webhook_data

    def _create_repository_webhook(
        self,
        api: BitbucketDCAPIResources,
        scope: dict[str, str] | None,
        webhook_config: dict[str, Any],
        access_token: str,
        installation_id: Any,
    ) -> dict[str, Any]:
        project_key = scope.get("project_key") if scope else None
        repo_slug = (scope.get("slug") or scope.get("repo_slug")) if scope else None

        if not project_key or not repo_slug:
            raise ValueError(
                "project_key and slug/repo_slug are required for repository webhooks"
            )

        try:
            webhook_data = api.create_repository_webhook(
                project_key=project_key,
                repo_slug=repo_slug,
                config=webhook_config,
                access_token=access_token,
            )
        except Exception as e:
            e.add_note("API call to create repository webhook failed")
            raise

        logger.info(
            f"Created repo webhook for {project_key}/{repo_slug} on installation {installation_id}"
        )
        return webhook_data

    def deregister_webhook(
        self,
        installation: GitProviderAppInstallation,
        webhook_id: str,
    ) -> None:
        """Deregister a webhook by ID.

        Reads scope info from installation.misc_metadata (set during registration).
        """
        logger.info(
            f"Deregistering webhook {webhook_id} for installation {installation.id}"
        )

        secrets = self.fetch_secrets(installation)
        access_token = secrets["token"]

        api = self._get_api_resources(
            base_url=installation.git_provider_app.base_url,
            ca_bundle_path=secrets.get("ca_bundle_path"),
            disable_ssl_verify=secrets.get("disable_ssl_verify", False),
        )

        # Read scope from installation metadata (persisted during registration)
        metadata = installation.misc_metadata or {}
        scope_type = metadata.get("webhook_scope_type")
        project_key = metadata.get("webhook_project_key")
        repo_slug = metadata.get("webhook_repo_slug")

        if not scope_type or not project_key:
            raise ValueError(
                "Webhook scope info not found in installation metadata. "
                "Was the webhook registered through this system?"
            )

        if scope_type == "project":
            try:
                api.delete_project_webhook(
                    project_key=project_key,
                    webhook_id=int(webhook_id),
                    access_token=access_token,
                )
            except Exception as e:
                e.add_note("API call to delete project webhook failed")
                raise
            logger.info(f"Deleted project webhook {webhook_id} for {project_key}")

        elif scope_type == "repository":
            if not repo_slug:
                raise ValueError(
                    "repo_slug not found in metadata for repository-level webhook"
                )
            try:
                api.delete_repository_webhook(
                    project_key=project_key,
                    repo_slug=repo_slug,
                    webhook_id=int(webhook_id),
                    access_token=access_token,
                )
            except Exception as e:
                e.add_note("API call to delete repository webhook failed")
                raise
            logger.info(
                f"Deleted repo webhook {webhook_id} for {project_key}/{repo_slug}"
            )
        else:
            raise ValueError(f"Invalid scope type: {scope_type}")

    def _map_triggers_to_events(self, triggers: list[str]) -> list[str]:
        dc_event_names = {"repo:refs_changed", "pr:merged", "pr:opened", "pr:modified"}
        events = []

        for trigger in triggers:
            if trigger in dc_event_names:
                events.append(trigger)
            else:
                mapped = self._TRIGGER_MAP.get(trigger, trigger)
                if isinstance(mapped, list):
                    events.extend(mapped)
                else:
                    events.append(mapped)

        return list(set(events))
