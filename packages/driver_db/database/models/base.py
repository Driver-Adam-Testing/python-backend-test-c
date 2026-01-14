import enum
import secrets
import string
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

import sqlalchemy.dialects.postgresql
import strawberry
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    UUID as SaUuid,
)
from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    Connection,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    desc,
    event,
    func,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSON, JSONB
from sqlalchemy.orm import Mapper
from sqlmodel import Field, Relationship, SQLModel, select

from ..custom_types import TSVector
from ..models_enums import (
    AutoDocStatusMessageKind,
    ContentKind,
    LlmPipelineKind,
    NodeKind,
    OrgRole,
    PrimaryAssetKind,
    PrimaryAssetProvider,
    SourceVisibility,
    VcsAutoUpdatePolicy,
    VersionStatus,
)

if TYPE_CHECKING:
    from .acl import PrimaryAssetRoleGrant


class RuntimeLogAgentInstance(SQLModel, table=True):  # type: ignore
    __tablename__ = "runtime_log_agent_instance"
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    id: UUID | None = Field(default_factory=uuid.uuid4, primary_key=True)
    model: str
    messages: list["RuntimeLogAgentMessage"] = Relationship(
        back_populates="agent_instance"
    )
    organization_id: str | None


class RuntimeLogAgentMessage(SQLModel, table=True):  # type: ignore
    __tablename__ = "runtime_log_agent_message"
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    id: UUID | None = Field(default_factory=uuid.uuid4, primary_key=True)
    message: dict = Field(default={}, sa_column=Column(JSON, nullable=False))  # type: ignore
    order: int = Field(default=None, sa_column=Column(Integer, autoincrement=True))
    agent_instance_id: UUID = Field(
        foreign_key="runtime_log_agent_instance.id", nullable=False
    )
    agent_instance: RuntimeLogAgentInstance = Relationship(back_populates="messages")


# TODO: DELETE THIS TABLE
@strawberry.enum
class Enum_Derived_Content_Status(str, enum.Enum):
    generating = "generating"
    generation_complete = "generation-complete"
    generation_error = "generation-error"


class DocumentSource(SQLModel, table=True):
    __tablename__ = "document_source"
    """Link table between documents and their sources."""

    source_version_node_id: uuid.UUID = Field(
        foreign_key="version_node.id",
        nullable=False,
        primary_key=True,
        ondelete="CASCADE",
    )
    page_version_node_id: uuid.UUID = Field(
        foreign_key="version_node.id",
        nullable=False,
        primary_key=True,
        ondelete="CASCADE",
    )
    source_version_node: "VersionNode" = Relationship(
        back_populates="document_sources",
        sa_relationship_kwargs={
            "foreign_keys": "DocumentSource.source_version_node_id"
        },
    )
    page_version_node: "VersionNode" = Relationship(
        back_populates="page_sources",
        sa_relationship_kwargs={"foreign_keys": "DocumentSource.page_version_node_id"},
    )


# TODO add indexes back
class DerivedContent(SQLModel, table=True):  # type: ignore
    __tablename__ = "derived_content"

    # TODO:
    # Point tags at node or primary asset
    # remove derived_content_type
    # remove workspace
    # remove codebase
    id: UUID | None = Field(
        sa_column=Column(
            SaUuid(as_uuid=True),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        default=None,
    )

    # Removing FKs from the following:
    content_kind: ContentKind | None = Field(
        sa_column=Column(
            String,
            nullable=True,
            index=True,
        ),
        default=None,
    )
    # Content doesn't need to be associated with a codebase in our flat asset design. But for now, we keep
    # all source contents and derived contents for a codebase associated with the codebase. PDFs and other docs,
    # however, won't have a codebase ID -- just a workspace ID, since we are keeping workspaces for now.

    node_id: None | UUID = Field(
        default=None,
        foreign_key="node.id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )

    content: None | str = Field(
        sa_column=Column(sqlalchemy.Text, nullable=True), default=None
    )

    # TODO: Get rid of this?
    content_name: None | str = Field(
        sa_column=Column(sqlalchemy.Text, nullable=True), default=None
    )
    misc_metadata: dict | None = Field(  # type: ignore
        sa_column=Column("metadata", JSONB, nullable=True), default=None
    )

    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )

    order: int | None = Field(
        sa_column=Column(Integer, nullable=True, server_default=text("0"))
    )

    chunks_and_embeds: list["ChunkAndEmbedding"] = Relationship(
        back_populates="content"
    )

    node: "Node" = Relationship(
        back_populates="contents",
        sa_relationship_kwargs={"foreign_keys": "DerivedContent.node_id"},
    )


def update_primary_asset_content_timestamp(
    mapper: Mapper[Any], connection: Connection, target: DerivedContent
) -> None:
    # TODO node_id is nullable today, but once that changes, this should be updated
    if not target.node_id:
        return

    primary_asset = (
        select(PrimaryAsset.id)
        .join(Version)
        .join(Node)
        .where(Node.id == target.node_id)
    )

    stmt = (
        update(PrimaryAsset)
        .where(PrimaryAsset.id.in_(primary_asset))
        .values(related_content_last_updated=func.now())
    )
    connection.execute(stmt)


event.listen(DerivedContent, "after_update", update_primary_asset_content_timestamp)
event.listen(DerivedContent, "after_insert", update_primary_asset_content_timestamp)


class ChunkAndEmbedding(SQLModel, table=True):  # type: ignore
    __tablename__ = "chunk_and_embedding"
    id: UUID | None = Field(default_factory=uuid.uuid4, primary_key=True)
    content_id: UUID = Field(
        foreign_key="derived_content.id",
        nullable=False,
        index=True,
        ondelete="CASCADE",
    )
    content: DerivedContent | None = Relationship(back_populates="chunks_and_embeds")
    text: str
    text_embedding_3_small: list[float] = Field(
        sa_column=Column(
            Vector(1536), nullable=True
        )  # TODO this column will need to be indexed ONCE POPULATED1
    )
    chunk_number: int
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    organization_id: str = Field(sa_column=Column(String, nullable=True))
    __ts_vector__: Any = Column(
        "__ts_vector__",
        TSVector(),
        Computed("to_tsvector('english', text)", persisted=True),
        index=Index("ix_chunkandembedding___ts_vector__", postgresql_using="gin"),
    )


class UsageSessionStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class UsageSession(SQLModel, table=True):
    __tablename__ = "usage_session"
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    status: UsageSessionStatus = Field(default=UsageSessionStatus.RUNNING, index=True)
    organization_id: str
    user_id: str
    session_metadata: dict | None = Field(
        sa_column=Column("metadata", JSONB, nullable=True), default=None
    )
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )

    usage_events: list["UsageEvent"] = Relationship(
        back_populates="session", cascade_delete=True
    )


class UsageEventType(enum.IntEnum):
    AGENT_PIPELINE_USAGE_DEBIT = 1
    INSPECTOR_TECH_DOC_USAGE_DEBIT = 2
    INSPECTOR_CODE_DIFF_USAGE_DEBIT = 3
    ONBOARDING_USAGE_DEBIT = 4
    SUMMARIZATION_USAGE_DEBIT = 5
    BASE_PLATFORM_USAGE_CREDIT = 6
    ADDITIONAL_PLATFORM_USAGE_CREDIT = 7
    USER_SEAT_USAGE_DEBIT = 8
    USER_SEAT_USAGE_CREDIT = 9

    def __str__(self) -> str:
        # This will return a more human-readable version of the enum name
        return self.name.replace("_", " ").title()


class UsageEvent(SQLModel, table=True):
    __tablename__ = "usage_event"
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    event_type: UsageEventType = Field(sa_column=Column(Integer, nullable=False))
    session_id: UUID = Field(
        foreign_key="usage_session.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    organization_id: str
    user_id: str
    event_source: str
    bytes_in: int = Field(default=0, nullable=False)
    bytes_out: int = Field(default=0, nullable=False)
    tokens_in: int = Field(default=0, nullable=False)
    tokens_out: int = Field(default=0, nullable=False)
    timestamp: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    event_metadata: dict | None = Field(
        sa_column=Column("metadata", JSONB, nullable=True), default=None
    )
    # Relationships
    session: UsageSession | None = Relationship(back_populates="usage_events")


class GithubAppInstallation(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: str = Field(index=True)
    github_app_installation_id: str = Field(index=True)
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    __tablename__ = "github_app_installation"
    __table_args__ = (
        UniqueConstraint(
            "github_app_installation_id",
            "organization_id",
            name="uq_github_app_installation_id_organization_id",
        ),
    )


class PlanType(str, enum.Enum):
    FREE = "free"
    CORE = "core"
    ADVANCED = "advanced"
    ENTERPRISE = "enterprise"


class BillingFrequency(str, enum.Enum):
    NEVER = "never"
    MONTHLY = "monthly"
    ANNUAL = "annual"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    CANCELED = "canceled"
    SUSPENDED = "suspended"


class Subscription(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: str = Field(index=True)
    plan_type: PlanType = Field(nullable=False, index=True)
    status: SubscriptionStatus = Field(default=SubscriptionStatus.ACTIVE)
    billing_frequency: BillingFrequency = Field(nullable=False)
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    __table_args__ = (
        # Create a partial unique index that enforces uniqueness for active subscriptions only.
        Index(
            "unique_active_subscription_per_org",
            "organization_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class GitProviderKind(str, enum.Enum):
    # GitLab
    GITLAB = "GITLAB"
    GITLAB_ENTERPRISE_SELF_MANAGED = "GITLAB_ENTERPRISE_SELF_MANAGED"
    # Bitbucket
    BITBUCKET = "BITBUCKET"
    BITBUCKET_DATA_CENTER = "BITBUCKET_DATA_CENTER"
    BITBUCKET_SERVER = "BITBUCKET_SERVER"
    # Azure DevOps
    AZURE_DEVOPS_CLOUD = "AZURE_DEVOPS_CLOUD"

    def __str__(self) -> str:
        return self.name


#
class GitProviderApp(SQLModel, table=True):
    __tablename__ = "git_provider_app"
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    provider_kind: GitProviderKind = Field(nullable=False, index=True)
    shared_provider: bool
    owner_organization_id: str = Field(index=True)
    name: str
    base_url: str
    ## This group of fields is used for OAuth
    client_id: str | None = Field(index=True, unique=True)
    redirect_uri: str | None
    scopes: str | None
    ##
    # Provider-specific metadata
    provider_metadata: dict | None = Field(
        sa_column=Column("metadata", JSONB, nullable=True, default=None),
        default_factory=dict,
    )

    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    # relationships
    app_installations: list["GitProviderAppInstallation"] = Relationship(
        back_populates="git_provider_app", cascade_delete=True
    )


class GitProviderAppInstallation(SQLModel, table=True):
    __tablename__ = "git_provider_app_installation"
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    git_provider_app_id: UUID = Field(
        foreign_key="git_provider_app.id",
        nullable=False,
        index=True,
        ondelete="CASCADE",
    )
    organization_id: str
    ## This group of fields is used for OAuth
    user_id: str | None
    ##
    misc_metadata: dict | None = Field(
        sa_column=Column("metadata", JSONB, nullable=True), default=None
    )
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    # relationships
    git_provider_app: GitProviderApp | None = Relationship(
        back_populates="app_installations"
    )
    # unique constraint
    __table_args__ = (
        UniqueConstraint(
            "git_provider_app_id",
            "organization_id",
            "user_id",
            name="uq_git_provider_app_id_organization_id_user_id",
        ),
    )


class PrimaryAsset(SQLModel, table=True):  # type: ignore
    __tablename__ = "primary_asset"
    __table_args__ = (
        Index(
            "ix_v2_primary_asset_organization_id_display_name",
            "organization_id",
            "display_name",
            unique=True,
        ),
    )

    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    display_name: str = Field(index=True)
    repository_id: str | None
    organization_id: str = Field(
        index=True
    )  # TODO: make this a foreign key to Organization
    kind: PrimaryAssetKind = Field(index=True)
    installation_id: UUID | None = Field(
        foreign_key="git_provider_app_installation.id",
        nullable=True,
        ondelete="SET NULL",
        default=None,
    )
    codebase_settings_auto_commit_docs: bool | None = Field(
        default=None, nullable=True
    )  # TODO: a temporary place until we have a RepoSettings table (or similar)
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        default=None,
    )
    provider: PrimaryAssetProvider = Field(
        index=True,
    )
    related_content_last_updated: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    most_recent_version: Optional["Version"] = Relationship(
        sa_relationship_kwargs={
            "primaryjoin": "PrimaryAsset.id == Version.primary_asset_id",
            "uselist": False,
            "order_by": "desc(Version.updated_at)",
            "cascade": "all, delete-orphan",
        },
    )
    most_recent_completed_version: Optional["Version"] = Relationship(
        sa_relationship_kwargs={
            "primaryjoin": "and_(PrimaryAsset.id == Version.primary_asset_id, Version.status == 'GENERATION_COMPLETE')",
            "uselist": False,
            "order_by": "desc(Version.updated_at)",
            "cascade": "all, delete-orphan",
            "overlaps": "most_recent_version, versions",
        },
    )
    versions: list["Version"] = Relationship(
        back_populates="primary_asset",
        sa_relationship_kwargs={
            "passive_deletes": True,
            "cascade": "all, delete-orphan",
            "foreign_keys": "[Version.primary_asset_id]",
            "order_by": "desc(Version.updated_at)",
            "primaryjoin": "PrimaryAsset.id == Version.primary_asset_id",
            "overlaps": "most_recent_version, most_recent_completed_version",
        },
    )
    nodes: list["Node"] = Relationship(
        back_populates="primary_asset",
        sa_relationship_kwargs={
            "passive_deletes": True,
            "cascade": "all, delete-orphan",
        },
    )
    tags: list["Tag"] = Relationship(
        back_populates="primary_assets",
        sa_relationship_kwargs={"secondary": "primary_asset_tag"},
    )
    grants: list["PrimaryAssetRoleGrant"] = Relationship(
        back_populates="primary_asset",
        sa_relationship_kwargs={
            "passive_deletes": True,
            "cascade": "all, delete-orphan",
        },
    )
    vcs_auto_update_policy: VcsAutoUpdatePolicy | None = Field(
        sa_column=Column(String, nullable=True)
    )
    vcs_tracked_branch: str | None = Field(nullable=True, default=None)


class Version(SQLModel, table=True):  # type: ignore
    __tablename__ = "version"
    __table_args__ = (
        UniqueConstraint(
            "primary_asset_id",
            "vcs_hash",
            name="ix_v2_version_primary_asset_id_vcs_hash",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "ix_version_primary_asset_id_updated_at_desc",
            "primary_asset_id",
            desc("updated_at"),
        ),
    )

    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    primary_asset_id: UUID = Field(
        foreign_key="primary_asset.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    status: VersionStatus = Field(index=True)
    previous_version_id: UUID | None = Field(
        default=None,
        nullable=True,
        foreign_key="version.id",
        ondelete="SET NULL",
    )
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
            index=True,
        ),
        default=None,
    )
    vcs_hash: str | None
    vcs_metadata: dict | None = Field(  # type: ignore
        sa_column=Column(JSONB, nullable=True),
    )
    primary_asset: "PrimaryAsset" = Relationship(
        back_populates="versions",
        sa_relationship_kwargs={
            "foreign_keys": "[Version.primary_asset_id]",
            "overlaps": "most_recent_version, most_recent_completed_version",
        },
    )
    creator: "UserCache" = Relationship(
        back_populates="created_versions",
        sa_relationship_kwargs={"secondary": "version_creator"},
    )
    root_version_node: Optional["VersionNode"] = Relationship(
        sa_relationship_kwargs={
            "primaryjoin": "and_(Version.id == VersionNode.version_id, VersionNode.depth == 0)",
            "uselist": False,
            "viewonly": True,
        }
    )
    inspector_runs: list["InspectorRun"] = Relationship(
        back_populates="version",
        passive_deletes="all",
        sa_relationship_kwargs={
            "order_by": "desc(InspectorRun.updated_at)",
        },
    )
    version_nodes: list["VersionNode"] = Relationship(
        back_populates="version",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "passive_deletes": True,
        },
    )

    @property
    def browsable(self) -> bool:
        return self.status in {
            VersionStatus.GENERATING,
            VersionStatus.GENERATION_ERROR,
            VersionStatus.GENERATION_COMPLETE,
            VersionStatus.INSUFFICIENT_BALANCE,
        }


class VersionNode(SQLModel, table=True):
    __tablename__ = "version_node"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "relative_path",
            name="ix_version_node_version_id_relative_path",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "ix_version_node_version_id_relative_path_length",
            "version_id",
            func.length("relative_path"),
        ),
        Index(
            "ix_version_node_version_id_relative_path_pattern_ops",
            "version_id",
            postgresql_ops={"relative_path": "text_pattern_ops"},
        ),
    )
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    version_id: uuid.UUID = Field(
        foreign_key="version.id", ondelete="CASCADE", index=True
    )
    relative_path: str = Field(index=True)
    node_id: uuid.UUID = Field(foreign_key="node.id", nullable=False, index=True)
    depth: int = Field(
        sa_column=Column(
            Integer,
            Computed(
                "length(trim(trailing '/' from relative_path)) - length(replace(trim(trailing '/' from relative_path), '/', ''))",
                persisted=True,
            ),
            index=True,
        )
    )
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        default=None,
    )
    total_files: int | None = Field(
        sa_column=Column(
            Integer,
            Computed("(misc_metadata->>'total_files')::INTEGER", persisted=True),
            index=True,
            nullable=True,
        ),
        default=None,
    )

    misc_metadata: dict | None = Field(  # type: ignore
        sa_column=Column(JSONB, nullable=True), default=None
    )
    version: "Version" = Relationship(back_populates="version_nodes")
    node: "Node" = Relationship(back_populates="version_nodes")

    document_sources: list["DocumentSource"] = Relationship(
        back_populates="source_version_node",
        sa_relationship_kwargs={
            "foreign_keys": "DocumentSource.source_version_node_id",
            "cascade": "all, delete-orphan",
            "passive_deletes": True,
        },
    )
    page_sources: list["DocumentSource"] = Relationship(
        back_populates="page_version_node",
        sa_relationship_kwargs={
            "foreign_keys": "DocumentSource.page_version_node_id",
            "cascade": "all, delete-orphan",
            "passive_deletes": True,
        },
    )


class Node(SQLModel, table=True):  # type: ignore
    __tablename__ = "node"
    __table_args__ = (
        UniqueConstraint(
            "primary_asset_id", "source_hash", name="unique_primary_asset_source_hash"
        ),
        CheckConstraint(
            "NOT (kind IN ('CODEBASE_FILE', 'CODEBASE_DIRECTORY') AND source_hash IS NULL)",
            name="check_source_hash_not_null_for_codebase_kinds",
        ),
    )
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_hash: str | None = Field(nullable=True, index=True)
    kind: NodeKind
    primary_asset_id: None | UUID = Field(
        foreign_key="primary_asset.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        default=None,
    )
    primary_asset: Optional["PrimaryAsset"] = Relationship(back_populates="nodes")
    contents: list["DerivedContent"] = Relationship(
        back_populates="node",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "passive_deletes": True,
        },
    )
    version_nodes: list["VersionNode"] = Relationship(back_populates="node")


class PrimaryAssetTag(SQLModel, table=True):
    __tablename__ = "primary_asset_tag"
    tag_id: UUID = Field(
        index=True, primary_key=True, ondelete="CASCADE", foreign_key="tag.id"
    )
    primary_asset_id: UUID = Field(
        index=True,
        primary_key=True,
        ondelete="CASCADE",
        foreign_key="primary_asset.id",
    )


class UserCache(SQLModel, table=True):
    __tablename__ = "user_cache"
    id: str = Field(
        primary_key=True
    )  # This is the auth0 ID in the form "auth0|1234567890"?
    full_name: str
    email: str
    created_versions: list["Version"] = Relationship(
        back_populates="creator",
        sa_relationship_kwargs={"secondary": "version_creator"},
    )


class VersionCreator(SQLModel, table=True):
    __tablename__ = "version_creator"
    version_id: UUID = Field(
        index=True, primary_key=True, ondelete="CASCADE", foreign_key="version.id"
    )
    user_id: str = Field(index=True, ondelete="CASCADE", foreign_key="user_cache.id")


class RuntimeLlmSession(SQLModel, table=True):
    __tablename__ = "runtime_llm_session"
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    organization_id: str = Field(index=True)
    source_node_ids_str: str | None = Field(
        nullable=True, default=None
    )  # drop this column
    page_node_id: UUID | None = Field(nullable=True, default=None)  # drop this column
    source_version_node_ids_str: str | None = Field(nullable=True, default=None)
    page_version_node_id: UUID | None = Field(nullable=True, default=None)
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        default=None,
    )
    message_histories: list["RuntimeLlmMessageHistory"] = Relationship(
        back_populates="llm_session",
    )


class RuntimeLlmMessageHistory(SQLModel, table=True):
    __tablename__ = "runtime_llm_message_history"
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    llm_session_id: UUID = Field(
        index=True,
        nullable=False,
        ondelete="CASCADE",
        foreign_key="runtime_llm_session.id",
    )
    pipeline_kind: str = Field(
        index=True, nullable=False, default=LlmPipelineKind.DEFAULT
    )
    messages: list["RuntimeLlmMessage"] = Relationship(
        back_populates="message_history",
        sa_relationship_kwargs={"order_by": "RuntimeLlmMessage.created_at"},
    )
    llm_session: "RuntimeLlmSession" = Relationship(
        back_populates="message_histories",
    )


class RuntimeLlmMessage(SQLModel, table=True):
    __tablename__ = "runtime_llm_messages"
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    message_history_id: UUID = Field(
        index=True,
        nullable=False,
        ondelete="CASCADE",
        foreign_key="runtime_llm_message_history.id",
    )
    llm_message_hash: str
    llm_message_json: dict | None = Field(
        sa_column=Column(JSONB, nullable=True), default=None
    )

    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        default=None,
    )
    message_history: "RuntimeLlmMessageHistory" = Relationship(
        back_populates="messages",
    )


class AutoDocStatusHistory(SQLModel, table=True):
    __tablename__ = "autodoc_status_history"
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_version_node_id: UUID = Field(
        index=True,
        nullable=False,
        foreign_key="version_node.id",
        ondelete="CASCADE",
    )
    status_kind: AutoDocStatusMessageKind = Field(
        nullable=False,
    )
    content: str | None
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    call_id: str | None


def gen_drv_key() -> str:
    PREFIX = "drv"
    ENTROPY = 32
    ALPHABET = string.ascii_letters + string.digits
    random_part = "".join(secrets.choice(ALPHABET) for _ in range(ENTROPY))
    return f"{PREFIX}-{random_part}"


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_key"

    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    key: str = Field(
        default_factory=gen_drv_key,
        sa_column=Column(String, unique=True, nullable=False, index=True),
    )
    organization_id: str = Field(nullable=False)
    user_id: str = Field(nullable=False)
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        default=None,
    )
    last_used_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=True,
        ),
        default=None,
    )


class Tag(SQLModel, table=True):  # type: ignore
    __tablename__ = "tag"
    __table_args__ = (
        UniqueConstraint("name", "organization_id", name="unique_tag_name_per_org_id"),
    )
    id: UUID | None = Field(
        sa_column=Column(
            SaUuid(as_uuid=True),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        default=None,
    )
    name: str = Field(
        max_length=255,
        sa_column=sqlalchemy.Column(sqlalchemy.String(255), nullable=False),
    )
    hex_color: str = Field(
        max_length=7,
        sa_column=sqlalchemy.Column(sqlalchemy.String(7), nullable=False),
    )
    organization_id: str
    type: str = Field(
        max_length=255,
        sa_column=sqlalchemy.Column(
            sqlalchemy.String(255),
            nullable=False,
            index=True,
        ),
    )
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    created_by: str = Field(
        sa_column=sqlalchemy.Column(sqlalchemy.String(128), nullable=False),
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        default=None,
    )
    updated_by: str = Field(
        sa_column=sqlalchemy.Column(sqlalchemy.String(128), nullable=False),
    )
    # content_links: list["TagContent"] = Relationship(
    #     back_populates="tag",
    #     sa_relationship_kwargs={"foreign_keys": "TagContent.tag_id"},
    # )
    primary_assets: list["PrimaryAsset"] = Relationship(
        back_populates="tags",
        sa_relationship_kwargs={"secondary": "primary_asset_tag"},
    )


class InspectorRun(SQLModel, table=True):
    __tablename__ = "inspector_run"
    id: UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    version_id: UUID = Field(
        foreign_key="version.id",
        nullable=False,
        ondelete="CASCADE",
    )
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    call_id: str | None
    version: "Version" = Relationship(back_populates="inspector_runs")


class AboutYouSurvey(SQLModel, table=True):
    __tablename__ = "about_you_survey"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", name="uq_about_you_survey_org_user"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: str = Field(index=True, nullable=False)
    user_id: str = Field(index=True, nullable=False)

    skipped: bool = Field(nullable=False)

    plans_for_driver: list[str] | None = Field(
        sa_column=Column(ARRAY(String), nullable=True)
    )
    team_size: str | None
    type_of_work: str | None
    type_of_work_other: str | None

    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )


class OnboardingChecklist(SQLModel, table=True):
    __tablename__ = "onboarding_checklist"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", name="uq_onboarding_checklist_org_user"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: str = Field(index=True, nullable=False)
    user_id: str = Field(index=True, nullable=False)

    # Timestamps for each step
    connect_codebase_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    generate_codebase_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    setup_mcp_started_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    setup_mcp_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    enable_export_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    generate_autodoc_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    invite_teammate_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    configured_rbac_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    teams_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    scim_provisioning_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    sso_sync_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )
    checklist_completed_at: None | datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True), default=None
    )

    # Standard timestamps
    created_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
        default=None,
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class Organization(SQLModel, table=True):
    __tablename__ = "organization"

    id: str = Field(primary_key=True)  # Auth0 org ID (e.g., "org_xxxxx")
    name: str = Field(index=True)  # Unique organization name
    display_name: str | None = Field(default=None)
    org_metadata: dict = Field(default={}, sa_column=Column(JSONB, nullable=False))
    default_source_visibility: SourceVisibility = Field(
        default=SourceVisibility.private
    )

    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        )
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    auth0_updated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class User(SQLModel, table=True):
    __tablename__ = "user"
    id: str = Field(primary_key=True)  # Auth0 user ID (e.g., "auth0|xxxxx")
    email: str | None = Field(default=None, index=True)
    name: str | None = Field(default=None, index=True)
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        )
    )
    updated_at: None | datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    auth0_updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class OrgMembership(SQLModel, table=True):
    """Link table for organization-user memberships"""

    __tablename__ = "org_membership"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_org_member"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: str = Field(foreign_key="organization.id", index=True, ondelete="CASCADE")
    user_id: str = Field(foreign_key="user.id", index=True, ondelete="CASCADE")
    role: OrgRole


class Auth0SyncRun(SQLModel, table=True):
    """Track Auth0 sync runs for incremental syncing"""

    __tablename__ = "auth0_sync_run"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    timestamp: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    status: str = Field(index=True)  # 'syncing' or 'synced'
