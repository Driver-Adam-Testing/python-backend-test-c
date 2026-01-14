from enum import Enum

from database.models import GitProviderKind
from pydantic import BaseModel, Field


class TokenType(str, Enum):
    GROUP_ACCESS_TOKEN = "group_access_token"  # GitLab
    WORKSPACE_ACCESS_TOKEN = "workspace_access_token"  # Bitbucket Cloud
    PROJECT_ACCESS_TOKEN = "project_access_token"  # Bitbucket Cloud & Data Center
    REPOSITORY_ACCESS_TOKEN = "repository_access_token"  # Bitbucket Cloud & Data Center
    PERSONAL_ACCESS_TOKEN = "personal_access_token"  # Azure DevOps

    def __str__(self) -> str:
        return self.name


class AccessTokenData(BaseModel):
    """Unified access token model"""

    token_type: TokenType = TokenType.GROUP_ACCESS_TOKEN
    token: str
    workspace_or_group: str | None = None
    name: str | None = None
    metadata: dict = {}
    # Bitbucket DC specific fields (optional, will be populated from app config if not provided)
    username: str | None = None
    instance_url: str | None = None

    def is_access_token(self) -> bool:
        return self.token_type in [
            TokenType.GROUP_ACCESS_TOKEN,
            TokenType.WORKSPACE_ACCESS_TOKEN,
            TokenType.PERSONAL_ACCESS_TOKEN,
        ]


class GitProvider(BaseModel):
    display_name: str
    name: str
    logo_url: str


class GitRepository(BaseModel):
    provider_name: str
    provider_kind: GitProviderKind | None = None
    repo_name: str
    org: str
    last_updated: str | None = None
    metadata: dict
    latest_commit: dict | None = None
    default_branch: str | None = None
    installation_id: str | None = None


class GitProviderAppConfig(BaseModel):
    base_url: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str | None = None


class GroupAccessToken(AccessTokenData):
    name: str | None = None
    token: str
    token_type: TokenType = TokenType.GROUP_ACCESS_TOKEN


class CreateGitProviderAppRequest(BaseModel):
    organization_id: str = Field(serialization_alias="owner_organization_id")
    name: str
    provider_kind: GitProviderKind
    shared_provider: bool = False
    base_url: str
    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None
    scopes: list[str] | None = Field(default_factory=list)


class GitProviderAppSecret(BaseModel):
    client_secret: str | None = None


class GitProviderAppTokenSecret(BaseModel):
    token: str
    secret_token: str | None = None


class WebhookInfo(BaseModel):
    callback_url: str
    custom_headers: dict | list[str]
    secret_token: str
    ssl_verification: bool
    triggers: list[str]


# Azure DevOps specific models
class AzureDevOpsProject(BaseModel):
    """Azure DevOps project information"""

    id: str
    name: str
    lastUpdateTime: str | None = None


class AzureDevOpsRepository(BaseModel):
    """Azure DevOps repository information"""

    id: str
    name: str
    url: str | None = None
    defaultBranch: str | None = None
    creationDate: str | None = None
    project: AzureDevOpsProject


class AzureDevOpsTokenData(BaseModel):
    """Azure DevOps Personal Access Token data structure"""

    token: str
    project: str
    secret_token: str | None = None


class AzureDevOpsWebhookResource(BaseModel):
    """Azure DevOps webhook resource structure"""

    repository: dict[str, str] | None = None
    refUpdates: list[dict[str, str]] | None = None


class AzureDevOpsWebhookPayload(BaseModel):
    """Azure DevOps webhook payload structure"""

    eventType: str
    resource: AzureDevOpsWebhookResource | None = None


# Bitbucket Data Center specific models
class BitbucketDCTokenData(BaseModel):
    """Bitbucket Data Center HTTP Access Token data structure.

    For Project/Repository tokens, we use Bearer auth only (no username needed).
    Clone URLs use x-token-auth as the username placeholder.

    token_type determines the scope of the token:
    - PROJECT_ACCESS_TOKEN: Can access all repos in a project, create project webhooks
    - REPOSITORY_ACCESS_TOKEN: Can only access a specific repo, create repo webhooks
    """

    token: str = Field(..., description="HTTP Access Token")
    name: str | None = Field(None, description="Token name for identification")
    token_type: TokenType = Field(
        default=TokenType.PROJECT_ACCESS_TOKEN,
        description="Type of token (project_access_token or repository_access_token)",
    )
    # For repository tokens, store the project/repo scope
    project_key: str | None = Field(
        None,
        description="Project key (required for project tokens, optional for repo tokens)",
    )
    repo_slug: str | None = Field(
        None, description="Repository slug (required for repository tokens)"
    )
    secret_token: str | None = Field(
        None, description="Webhook secret for signature verification"
    )
    ca_bundle_path: str | None = Field(
        None, description="Path to CA bundle for self-signed certificates"
    )
    disable_ssl_verify: bool = Field(
        False, description="Disable SSL verification (dev/test only)"
    )
