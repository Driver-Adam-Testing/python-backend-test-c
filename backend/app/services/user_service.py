"""Service for User business logic."""

import logging
from datetime import datetime
from uuid import UUID

from database.models import PrimaryAsset, PrimaryAssetRoleGrant, TeamMembership
from database.models_enums import (
    OrgRole,
    PrimaryAssetKind,
    PrimaryAssetRole,
    PrincipalKind,
    TeamRole,
)
from fastapi import HTTPException, status
from sqlmodel import Session

from app.auth.models import User
from app.repositories import team_repository, user_repository
from app.schemas.user_schema import (
    AddUserSourcesRequest,
    AddUserTeamsRequest,
    AssignmentType,
    RemoveUserSourcesRequest,
    RemoveUserTeamsRequest,
    UpdateUserSourcesRequest,
    UpdateUserTeamsRequest,
    UserSourceResponse,
    UserSourcesResponse,
    UserTeamResponse,
    UserTeamsResponse,
)

logger = logging.getLogger(__name__)


class UserService:
    """Service for User operations."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_user_teams(
        self,
        user: User,
        user_id: str,
        roles: list[TeamRole] | None = None,
        search: str | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> UserTeamsResponse:
        """
        Get teams for a user.

        Args:
            user: Authenticated user making the request
            user_id: User ID
            roles: Optional list of roles to filter by
            search: Optional search query for team name
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            UserTeamsResponse with teams and total count
        """
        organization_id = user.organization_id
        team_data = user_repository.get_user_teams_with_details(
            session=self.session,
            user_id=user_id,
            organization_id=organization_id,
            roles=roles,
            search=search,
            limit=limit,
            offset=offset,
        )

        total = user_repository.count_user_teams(
            session=self.session,
            user_id=user_id,
            organization_id=organization_id,
            roles=roles,
            search=search,
        )

        teams = [self._build_user_team_response(data) for data in team_data]

        return UserTeamsResponse(
            teams=teams,
            total=total,
        )

    def add_user_teams(
        self,
        user: User,
        user_id: str,
        request: AddUserTeamsRequest,
    ) -> None:
        """
        Add user to teams.

        Args:
            user_id: User ID
            organization_id: Organization ID
            request: Request containing teams to add

        Raises:
            HTTPException: If team not found or user already in team
        """
        organization_id = user.organization_id
        for team_input in request.teams:
            team_id = UUID(team_input.team_id)

            # Verify team exists in organization
            team = team_repository.get_team_by_id(
                session=self.session,
                team_id=team_id,
                organization_id=organization_id,
            )
            if not team:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Team {team_input.team_id} not found",
                )

            # Check if membership already exists
            existing = user_repository.get_user_team_membership(
                session=self.session,
                user_id=user_id,
                team_id=team_id,
            )
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"User {user_id} already in team {team_input.team_id}",
                )

            # Create membership
            membership = TeamMembership(
                team_id=team_id,
                user_id=user_id,
                role=team_input.role,
            )
            self.session.add(membership)

        self.session.commit()
        teams_info = [
            {"team_id": t.team_id, "role": t.role.value} for t in request.teams
        ]
        logger.info(
            "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, target_user_id=%s, teams=%s",
            "user.team.add",
            user.user_id,
            organization_id,
            user.organization_display_name,
            user_id,
            teams_info,
        )

    def update_user_teams(
        self,
        user: User,
        user_id: str,
        request: UpdateUserTeamsRequest,
    ) -> None:
        """
        Update user's team roles.

        Args:
            user_id: User ID
            organization_id: Organization ID
            request: Request containing teams with updated roles

        Raises:
            HTTPException: If team not found or user not in team
        """
        organization_id = user.organization_id
        changes: list[dict] = []
        for team_input in request.teams:
            team_id = UUID(team_input.team_id)

            # Verify team exists in organization
            team = team_repository.get_team_by_id(
                session=self.session,
                team_id=team_id,
                organization_id=organization_id,
            )
            if not team:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Team {team_input.team_id} not found",
                )

            # Get existing membership
            membership = user_repository.get_user_team_membership(
                session=self.session,
                user_id=user_id,
                team_id=team_id,
            )
            if not membership:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User {user_id} not in team {team_input.team_id}",
                )

            # Capture old role for logging
            old_role = membership.role.value
            # Update role
            membership.role = team_input.role
            self.session.add(membership)
            changes.append(
                {
                    "team_id": team_input.team_id,
                    "old_role": old_role,
                    "new_role": team_input.role.value,
                }
            )

        self.session.commit()
        logger.info(
            "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, target_user_id=%s, changes=%s",
            "user.team.update",
            user.user_id,
            organization_id,
            user.organization_display_name,
            user_id,
            changes,
        )

    def remove_user_teams(
        self,
        user: User,
        user_id: str,
        request: RemoveUserTeamsRequest,
    ) -> None:
        """
        Remove user from teams.

        Args:
            user_id: User ID
            organization_id: Organization ID
            request: Request containing team IDs to remove

        Raises:
            HTTPException: If team not found or user not in team
        """
        organization_id = user.organization_id
        for team_id_str in request.team_ids:
            team_id = UUID(team_id_str)

            # Verify team exists in organization
            team = team_repository.get_team_by_id(
                session=self.session,
                team_id=team_id,
                organization_id=organization_id,
            )
            if not team:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Team {team_id_str} not found",
                )

            # Get existing membership
            membership = user_repository.get_user_team_membership(
                session=self.session,
                user_id=user_id,
                team_id=team_id,
            )
            if not membership:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User {user_id} not in team {team_id_str}",
                )

            # Delete membership
            self.session.delete(membership)

        self.session.commit()
        logger.info(
            "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, target_user_id=%s, removed_team_ids=%s",
            "user.team.remove",
            user.user_id,
            organization_id,
            user.organization_display_name,
            user_id,
            request.team_ids,
        )

    def get_user_sources(
        self,
        user: User,
        user_id: str,
        roles: list[PrimaryAssetRole] | None = None,
        search: str | None = None,
        assignment_type: AssignmentType | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> UserSourcesResponse:
        organization_id = user.organization_id
        source_data = user_repository.get_user_sources_with_details(
            session=self.session,
            user_id=user_id,
            organization_id=organization_id,
            roles=roles,
            search=search,
            assignment_type=assignment_type,
            limit=limit,
            offset=offset,
        )

        total = user_repository.count_user_sources(
            session=self.session,
            user_id=user_id,
            organization_id=organization_id,
            roles=roles,
            search=search,
            assignment_type=assignment_type,
        )

        asset_ids = [data["asset"].id for data in source_data]
        team_grants = user_repository.get_user_team_grants_for_assets(
            session=self.session,
            user_id=user_id,
            organization_id=organization_id,
            asset_ids=asset_ids,
        )

        sources = [
            self._build_user_source_response(data, user_id, team_grants)
            for data in source_data
        ]

        return UserSourcesResponse(sources=sources, total=total)

    def add_user_sources(
        self,
        user: User,
        user_id: str,
        request: AddUserSourcesRequest,
    ) -> None:
        """
        Grant user direct access to sources.

        Args:
            user_id: User ID
            organization_id: Organization ID
            request: Request containing sources to grant access to

        Raises:
            HTTPException: If source not found or user already has access
        """
        organization_id = user.organization_id
        for source_input in request.sources:
            source_id = UUID(source_input.source_id)

            # Verify source exists
            asset = self.session.get(PrimaryAsset, source_id)
            if not asset or asset.organization_id != organization_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Source {source_input.source_id} not found",
                )

            if asset.kind not in (PrimaryAssetKind.CODEBASE, PrimaryAssetKind.FILE):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Only codebases and files can have user/team grants. "
                    f"Asset {source_input.source_id} is of type {asset.kind.value}.",
                )

            # Check if grant already exists
            existing = user_repository.get_user_source_grant(
                session=self.session,
                user_id=user_id,
                primary_asset_id=source_id,
            )
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"User {user_id} already has access to source {source_input.source_id}",
                )

            # Create grant
            grant = PrimaryAssetRoleGrant(
                organization_id=organization_id,
                primary_asset_id=source_id,
                principal_kind=PrincipalKind.user,
                user_id=user_id,
                role=source_input.role,
            )
            self.session.add(grant)

        self.session.commit()
        sources_info = [
            {"source_id": s.source_id, "role": s.role.value} for s in request.sources
        ]
        logger.info(
            "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, target_user_id=%s, sources=%s",
            "user.source.add",
            user.user_id,
            organization_id,
            user.organization_display_name,
            user_id,
            sources_info,
        )

    def update_user_sources(
        self,
        user: User,
        user_id: str,
        request: UpdateUserSourcesRequest,
    ) -> None:
        """
        Update user's source roles.

        Args:
            user_id: User ID
            organization_id: Organization ID
            request: Request containing sources with updated roles

        Raises:
            HTTPException: If source not found or user doesn't have access
        """
        organization_id = user.organization_id
        changes: list[dict] = []
        for source_input in request.sources:
            source_id = UUID(source_input.source_id)

            # Verify source exists
            asset = self.session.get(PrimaryAsset, source_id)
            if not asset or asset.organization_id != organization_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Source {source_input.source_id} not found",
                )

            # Get existing grant
            grant = user_repository.get_user_source_grant(
                session=self.session,
                user_id=user_id,
                primary_asset_id=source_id,
            )
            if not grant:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User {user_id} does not have access to source {source_input.source_id}",
                )

            # Capture old role for logging
            old_role = grant.role.value
            # Update role
            grant.role = source_input.role
            self.session.add(grant)
            changes.append(
                {
                    "source_id": source_input.source_id,
                    "old_role": old_role,
                    "new_role": source_input.role.value,
                }
            )

        self.session.commit()
        logger.info(
            "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, target_user_id=%s, changes=%s",
            "user.source.update",
            user.user_id,
            organization_id,
            user.organization_display_name,
            user_id,
            changes,
        )

    def remove_user_sources(
        self,
        user: User,
        user_id: str,
        request: RemoveUserSourcesRequest,
    ) -> None:
        """
        Remove user's direct access to sources.

        Args:
            user_id: User ID
            organization_id: Organization ID
            request: Request containing source IDs to remove

        Raises:
            HTTPException: If source not found or user doesn't have access
        """
        organization_id = user.organization_id
        for source_id_str in request.source_ids:
            source_id = UUID(source_id_str)

            # Verify source exists
            asset = self.session.get(PrimaryAsset, source_id)
            if not asset or asset.organization_id != organization_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Source {source_id_str} not found",
                )

            # Get existing grant
            grant = user_repository.get_user_source_grant(
                session=self.session,
                user_id=user_id,
                primary_asset_id=source_id,
            )
            if not grant:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User {user_id} does not have access to source {source_id_str}",
                )

            # Delete grant
            self.session.delete(grant)

        self.session.commit()
        logger.info(
            "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, target_user_id=%s, removed_source_ids=%s",
            "user.source.remove",
            user.user_id,
            organization_id,
            user.organization_display_name,
            user_id,
            request.source_ids,
        )

    def _build_user_team_response(self, data: dict) -> UserTeamResponse:
        """Build UserTeamResponse from repository data."""
        team = data["team"]
        membership = data["membership"]

        now_iso = datetime.utcnow().isoformat()
        created_at = (
            team.created_at.isoformat()
            if hasattr(team, "created_at") and team.created_at
            else now_iso
        )
        updated_at = (
            team.updated_at.isoformat()
            if hasattr(team, "updated_at") and team.updated_at
            else now_iso
        )

        return UserTeamResponse(
            id=str(team.id),
            name=team.name,
            admins=data["admins"],
            members=data["members"],
            sources=data["sources"],
            created_at=created_at,
            updated_at=updated_at,
            role=membership.role,
        )

    def _build_user_source_response(
        self, data: dict, user_id: str, team_grants: dict[UUID, list[dict]]
    ) -> UserSourceResponse:
        """Build UserSourceResponse from repository data."""
        asset = data["asset"]
        effective_role = data["effective_role"]
        asset_org_role = data["asset_org_role"]
        source_role = data["source_role"]
        user_org_role = data["user_org_role"]

        is_browsable = (
            asset.most_recent_version.browsable if asset.most_recent_version else False
        )

        is_super_admin = user_org_role == OrgRole.org_super_admin

        # Get teams for this asset
        teams = team_grants.get(asset.id, [])

        return UserSourceResponse(
            id=str(asset.id),
            organization_id=asset.organization_id,
            kind=asset.kind.value if hasattr(asset.kind, "value") else str(asset.kind),
            display_name=asset.display_name,
            provider=(
                asset.provider.value
                if asset.provider and hasattr(asset.provider, "value")
                else None
            ),
            created_at=asset.created_at.isoformat(),
            updated_at=asset.updated_at.isoformat(),
            effective_role=PrimaryAssetRole(effective_role),
            source_role=PrimaryAssetRole(source_role) if source_role else None,
            asset_org_role=PrimaryAssetRole(asset_org_role) if asset_org_role else None,
            user_org_role=OrgRole(user_org_role),
            is_super_admin=is_super_admin,
            teams=[
                {
                    "team_id": str(t["team_id"]),
                    "display_name": t["display_name"],
                    "team_role": t["team_role"],
                    "source_role": t["source_role"],
                }
                for t in teams
            ],
            user_id=user_id,
            is_browsable=is_browsable,
        )
