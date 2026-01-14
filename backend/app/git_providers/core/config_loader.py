from app.git_providers.core.config import GitProviderConfig
from database.models import GitProviderApp, GitProviderKind


def load_provider_config(
    app: GitProviderApp, client_secret: str | None = None
) -> GitProviderConfig:
    if app.provider_kind in [
        GitProviderKind.GITLAB_ENTERPRISE_SELF_MANAGED,
    ]:
        return GitProviderConfig(
            application_id=app.id,
            name=app.name,
            provider_kind=app.provider_kind,
            base_url=app.base_url,
            client_id=app.client_id,
            client_secret=client_secret,
            redirect_uri=app.redirect_uri,
            scope=app.scopes,
            authorize_endpoint="oauth/authorize",
            token_endpoint="oauth/token",
            token_info_endpoint="oauth/token/info",
            user_endpoint="api/v4/user",
        )
    elif app.provider_kind == GitProviderKind.BITBUCKET:
        return GitProviderConfig(
            application_id=app.id,
            name=app.name,
            provider_kind=app.provider_kind,
            base_url="https://bitbucket.org",
            client_id=None,
            client_secret=None,
            redirect_uri=None,
            token_endpoint=None,
            user_endpoint=None,
            authorize_endpoint=None,
            scope=None,
        )
    elif app.provider_kind == GitProviderKind.AZURE_DEVOPS_CLOUD:
        return GitProviderConfig(
            application_id=app.id,
            name=app.name,
            provider_kind=app.provider_kind,
            base_url=app.base_url or "https://dev.azure.com",
            client_id=None,
            client_secret=None,
            redirect_uri=None,
            # Azure DevOps doesn't use OAuth endpoints
            token_endpoint=None,
            user_endpoint=None,
            authorize_endpoint=None,
            token_info_endpoint=None,
            scope=app.scopes,
        )
    elif app.provider_kind == GitProviderKind.BITBUCKET_DATA_CENTER:
        return GitProviderConfig(
            application_id=app.id,
            name=app.name,
            provider_kind=app.provider_kind,
            base_url=app.base_url,  # Customer's self-hosted instance URL
            client_id=None,
            client_secret=None,
            redirect_uri=None,
            # Bitbucket DC uses HTTP Access Tokens, not OAuth
            token_endpoint=None,
            user_endpoint=None,
            authorize_endpoint=None,
            token_info_endpoint=None,
            scope=None,
        )
    raise ValueError(f"Unsupported provider: {app.provider_kind}")
