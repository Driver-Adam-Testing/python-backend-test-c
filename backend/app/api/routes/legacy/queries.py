import logging
import uuid

import strawberry
from app.api.routes.legacy.document_set import DocumentSet, get_document_set
from app.api.routes.legacy.scalars import NodeType
from app.api.routes.legacy.tree import FlatNode, get_codebase_tree
from app.authorization.fastapi import check_asset_action, check_org_action
from app.repositories.github_app_installations_repository import (
    GithubAppInstallationsRepository,
)
from database.models import Version
from graphql import GraphQLError
from sqlmodel import select
from strawberry.types import Info
from strawberry.types.nodes import Selection

logger = logging.getLogger(__name__)


@strawberry.type
class GitProvider:
    display_name: str
    name: str
    logo_url: str


def is_code_content_requested(info: Info) -> bool:
    """Recursively check if 'content' field under 'code' is requested in the query."""

    def has_content_field(fields: list[Selection]) -> bool:
        for field in fields:
            if field.name == "code" and any(
                subfield.name == "content" for subfield in field.selections
            ):
                return True
            # Recursively check nested fields in case of deeply nested selections
            if field.selections and has_content_field(field.selections):
                return True
        return False

    return has_content_field(info.selected_fields)


@strawberry.type
class Query:
    @strawberry.field
    def documentSet(
        self,
        info: Info,
        nodeKind: NodeType,
        path: str | None = None,
        primaryAssetId: strawberry.ID | None = None,
        versionId: strawberry.ID | None = None,
    ) -> DocumentSet:
        if path is None or versionId is None or primaryAssetId is None:
            raise GraphQLError(
                "path, versionId, and codebaseId must not be None",
                extensions={"code": "BAD_REQUEST"},
            )
        session = info.context.session
        user = info.context.user

        decision = check_asset_action(
            db=session,
            user=user,
            asset_id=uuid.UUID(str(primaryAssetId)),
            action_key="codebase.view_versions",
        )
        if not decision.allowed:
            raise GraphQLError("Access denied", extensions={"code": "FORBIDDEN"})

        fetch_code_content = is_code_content_requested(info)
        logger.info("Is code content requested: %s", fetch_code_content)
        return get_document_set(
            nodeKind,
            path,
            str(primaryAssetId),
            user.organization_id,
            session,
            fetch_code_content,
            versionId,
        )

    @strawberry.field
    def tree(
        self,
        info: Info,
        codebaseId: strawberry.ID | None = None,
        workspaceId: strawberry.ID | None = None,
        versionId: strawberry.ID | None = None,
    ) -> list[FlatNode]:
        if versionId is None:
            raise GraphQLError(
                "versionId must not be None",
                extensions={"code": "BAD_REQUEST"},
            )

        session = info.context.session
        user = info.context.user

        version = session.exec(
            select(Version).where(Version.id == str(versionId))
        ).first()
        if not version:
            raise GraphQLError("Version not found", extensions={"code": "NOT_FOUND"})

        decision = check_asset_action(
            db=session,
            user=user,
            asset_id=version.primary_asset_id,
            action_key="codebase.view_versions",
        )
        if not decision.allowed:
            raise GraphQLError("Access denied", extensions={"code": "FORBIDDEN"})

        return get_codebase_tree(
            session=session,
            organization_id=user.organization_id,
            version_id=str(versionId),
        )

    @strawberry.field
    def connectedGitProviders(self, info: Info) -> list[GitProvider]:
        """This endpoint lists which git providers (ie Github, Gitlab, etc)that a user/org has configured. It is polled by the UI."""
        session = info.context.session
        user = info.context.user

        decision = check_org_action(db=session, user=user, action_key="vcs.manage")
        if not decision.allowed:
            raise GraphQLError("Access denied", extensions={"code": "FORBIDDEN"})

        providers = []
        gh_repository = GithubAppInstallationsRepository(session)
        if len(gh_repository.list_by_organization_id(user.organization_id)) > 0:
            providers.append(
                GitProvider(
                    display_name="GitHub",
                    name="github",
                    logo_url="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png",
                )
            )
        return providers
