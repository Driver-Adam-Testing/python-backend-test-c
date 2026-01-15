"""Service for Team business logic."""

import logging
from datetime import datetime
from uuid import UUID, uuid4

from database.models import PrimaryAssetRoleGrant, Team, TeamMembership
from database.models import User as DbUser
from database.models_enums import TeamRole
from fastapi import HTTPException, status
from shared.authorization.helpers import is_super_admin
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth.models import User
from app.repositories import org_membership_repository, team_repository
from app.schemas.team_schema import (
    CreateTeamRequest,
    TeamDetailResponse,
    TeamMemberInput,
    TeamResponse,
    TeamsResponse,
    UpdateTeamRequest,
)

logger = logging.getLogger(__name__)


def get_user_by_id(session: Session, user_id: str) -> DbUser | None:
    query = select(DbUser).where(DbUser.id == user_id)
    return session.exec(query).first()


def team_dict_to_response(team_dict: dict) -> TeamResponse:
    """Expects dict with 'team', 'admins', 'members', 'sources' keys, the user's effective role, and optional access flags."""
    team = team_dict["team"]

    # Handle created_at and updated_at
    # For now, use defaults if fields don't exist (until migration is run)
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

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        admins=team_dict["admins"],
        members=team_dict["members"],
        sources=team_dict["sources"],
        created_at=created_at,
        updated_at=updated_at,
        has_user_access=team_dict.get("has_user_access", False),
        has_source_access=team_dict.get("has_source_access", False),
    )


class TeamService:
    """Service for Team operations."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_team(
        self,
        user: User,
        request: CreateTeamRequest,
    ) -> TeamResponse:
        """Raises HTTPException if team name already exists or validation fails."""
        organization_id = user.organization_id

        # Create team
        team = Team(
            id=uuid4(),
            organization_id=organization_id,
            name=request.name.strip(),
        )

        try:
            created_team = team_repository.create_team(self.session, team)
        except IntegrityError as e:
            self.session.rollback()
            if "duplicate key value violates unique constraint" in str(e.orig):
                logger.error(f"Team name '{request.name}' already exists")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Team name '{request.name}' already exists "
                        f"in this organization"
                    ),
                )
            logger.error(f"Unexpected error creating team: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create team",
            )

        # Add members if provided
        if request.members:
            try:
                self._add_team_members(
                    created_team.id, organization_id, request.members
                )
            except ValueError as e:
                # Rollback team creation if member validation fails
                self.session.rollback()
                logger.error(f"Member validation failed: {e}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(e),
                )
            except Exception as e:
                # Rollback team creation if member addition fails
                self.session.rollback()
                logger.error(f"Failed to add members to team: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to add team members",
                )

        # Get team with counts
        team_with_counts = team_repository.get_team_with_counts(
            self.session,
            created_team.id,
            organization_id,
        )

        if not team_with_counts:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve created team",
            )

        initial_members = [
            {"user_id": m.user_id, "role": m.role.value}
            for m in (request.members or [])
        ]
        logger.info(
            "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, team_id=%s, team_name=%s, initial_members=%s",
            "team.create",
            user.user_id,
            organization_id,
            user.organization_display_name,
            str(created_team.id),
            request.name,
            initial_members,
        )
        return team_dict_to_response(team_with_counts)

    def get_teams(
        self,
        user: User,
        limit: int = 30,
        offset: int = 0,
        search: str | None = None,
        check_user_id: str | None = None,
        check_source_id: UUID | None = None,
    ) -> TeamsResponse:
        """Super admins see all teams. Regular users only see teams they are members of."""
        organization_id = user.organization_id
        user_id = user.user_id
        logger.info(
            f"Getting teams for organization {organization_id} by user {user_id} "
            f"(limit={limit}, offset={offset}, search={search}, check_user_id={check_user_id}, check_source_id={check_source_id})"
        )
        is_admin = is_super_admin(self.session, user_id, organization_id)
        filter_user_id = None if is_admin else user_id

        # Get teams with optional search filter
        if search and search.strip():
            teams_with_counts = team_repository.search_teams_with_counts(
                session=self.session,
                organization_id=organization_id,
                query=search,
                limit=limit,
                offset=offset,
                user_id=filter_user_id,
                check_user_id=check_user_id,
                check_source_id=check_source_id,
            )
            total = team_repository.count_teams_by_search(
                session=self.session,
                organization_id=organization_id,
                search_query=search,
                user_id=filter_user_id,
            )
        else:
            teams_with_counts = team_repository.get_teams_with_counts(
                session=self.session,
                organization_id=organization_id,
                limit=limit,
                offset=offset,
                user_id=filter_user_id,
                check_user_id=check_user_id,
                check_source_id=check_source_id,
            )
            total = team_repository.count_teams(
                session=self.session,
                organization_id=organization_id,
                user_id=filter_user_id,
            )

        teams = [team_dict_to_response(team_dict) for team_dict in teams_with_counts]

        logger.info(f"Found {len(teams)} teams (total: {total}) (is_admin={is_admin})")
        return TeamsResponse(teams=teams, total=total)

    def get_team(
        self,
        user: User,
        team_id: UUID,
    ) -> TeamDetailResponse:
        """Raises HTTPException if team not found."""
        organization_id = user.organization_id
        user_id = user.user_id
        logger.info(
            f"Getting team {team_id} for organization {organization_id} by user {user_id}"
        )

        team_with_counts = team_repository.get_team_with_counts(
            session=self.session,
            team_id=team_id,
            organization_id=organization_id,
        )

        if not team_with_counts:
            logger.error(f"Team {team_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Team not found",
            )

        # Note: enforce_team_action already verified user is a member or super admin
        if is_super_admin(self.session, user_id, organization_id):
            effective_role: TeamRole = TeamRole.team_admin
        else:
            role = team_repository.get_user_team_role(
                self.session, team_id, user_id, organization_id
            )
            effective_role = role

        base_response = team_dict_to_response(team_with_counts)

        return TeamDetailResponse(
            **base_response.model_dump(),
            effective_team_role=effective_role,
        )

    def update_team(
        self,
        user: User,
        team_id: UUID,
        request: UpdateTeamRequest,
    ) -> TeamResponse:
        """Raises HTTPException if team not found or name already exists."""
        organization_id = user.organization_id

        # Get existing team
        team = team_repository.get_team_by_id(
            session=self.session,
            team_id=team_id,
            organization_id=organization_id,
        )

        if not team:
            logger.error(f"Team {team_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Team not found",
            )

        # Capture old name for logging
        old_name = team.name

        # Update team
        team.name = request.name.strip()

        try:
            self.session.add(team)
            self.session.commit()
            self.session.refresh(team)
        except IntegrityError as e:
            self.session.rollback()
            if "duplicate key value violates unique constraint" in str(e.orig):
                logger.error(f"Team name '{request.name}' already exists")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Team name '{request.name}' already exists "
                        f"in this organization"
                    ),
                )
            logger.error(f"Unexpected error updating team: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update team",
            )

        # Get updated team with counts
        team_with_counts = team_repository.get_team_with_counts(
            session=self.session,
            team_id=team_id,
            organization_id=organization_id,
        )

        if not team_with_counts:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve updated team",
            )

        logger.info(
            "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, team_id=%s, old_name=%s, new_name=%s",
            "team.update",
            user.user_id,
            organization_id,
            user.organization_display_name,
            str(team_id),
            old_name,
            request.name.strip(),
        )
        return team_dict_to_response(team_with_counts)

    def delete_team(
        self,
        user: User,
        team_id: UUID,
    ) -> None:
        """Also deletes memberships and source grants. Raises HTTPException if not found."""
        organization_id = user.organization_id

        # Verify team exists and belongs to organization
        team = team_repository.get_team_by_id(
            session=self.session,
            team_id=team_id,
            organization_id=organization_id,
        )

        if not team:
            logger.error(f"Team {team_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Team not found",
            )

        # Capture team name for logging before deletion
        team_name = team.name

        try:
            # Delete source grants for this team
            acl_grants = self.session.exec(
                select(PrimaryAssetRoleGrant).where(
                    PrimaryAssetRoleGrant.team_id == team_id
                )
            ).all()
            for grant in acl_grants:
                self.session.delete(grant)
            # Delete team memberships
            team_memberships = self.session.exec(
                select(TeamMembership).where(TeamMembership.team_id == team_id)
            ).all()
            for membership in team_memberships:
                self.session.delete(membership)

            self.session.flush()

            # Delete team
            self.session.delete(team)
            self.session.commit()
            logger.info(
                "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, team_id=%s, team_name=%s",
                "team.delete",
                user.user_id,
                organization_id,
                user.organization_display_name,
                str(team_id),
                team_name,
            )
        except Exception as e:
            self.session.rollback()
            logger.error(f"Failed to delete team {team_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete team",
            )

    def _add_team_members(
        self,
        team_id: UUID,
        organization_id: str,
        members: list[TeamMemberInput],
    ) -> None:
        """Raises ValueError if user not found or not in organization."""
        for member in members:
            # Validate user exists
            user = get_user_by_id(self.session, member.user_id)
            if not user:
                raise ValueError(f"User {member.user_id} not found")

            # Validate user belongs to organization
            if not org_membership_repository.check_user_in_organization(
                self.session, member.user_id, organization_id
            ):
                raise ValueError(
                    f"User {member.user_id} is not a member of this organization"
                )

            membership = TeamMembership(
                id=uuid4(),
                team_id=team_id,
                user_id=member.user_id,
                role=member.role,
            )
            self.session.add(membership)

        self.session.commit()
