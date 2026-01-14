"""Service for Team Member business logic."""

import logging
from uuid import UUID, uuid4

from database.models import TeamMembership
from database.models import User as DbUser
from database.models_enums import TeamRole
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth.models import User
from app.repositories import (
    org_membership_repository,
    team_member_repository,
    team_repository,
)
from app.schemas.team_member_schema import (
    AddTeamMembersRequest,
    RemoveTeamMembersRequest,
    TeamMemberAddInput,
    TeamMemberResponse,
    TeamMembersResponse,
    UpdateTeamMembersRequest,
)

logger = logging.getLogger(__name__)


def member_dict_to_response(member_dict: dict) -> TeamMemberResponse:
    """
    Convert member dictionary to TeamMemberResponse.

    Args:
        member_dict: Dictionary with 'membership', 'user', 'team' keys

    Returns:
        TeamMemberResponse object
    """
    membership = member_dict["membership"]
    user = member_dict["user"]
    team = member_dict["team"]

    # Use user's updated_at as proxy for last_active
    # TODO: Implement proper last_active tracking
    last_active = (
        user.updated_at.isoformat() if user.updated_at else user.created_at.isoformat()
    )

    return TeamMemberResponse(
        user_id=user.id,
        name=user.name or "",
        email=user.email or "",
        picture="",  # TODO: Fetch from Auth0 or add to User model
        team_id=str(team.id),
        team_name=team.name,
        role=membership.role,
        created_at=membership.created_at.isoformat()
        if hasattr(membership, "created_at") and membership.created_at
        else last_active,
        last_active=last_active,
    )


def get_user_by_id(session: Session, user_id: str) -> DbUser | None:
    """Get a user by ID."""
    query = select(DbUser).where(DbUser.id == user_id)
    return session.exec(query).first()


class TeamMemberService:
    """Service for Team Member operations."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_team_members(
        self,
        user: User,
        team_id: UUID,
        roles: list[TeamRole] | None = None,
        search: str | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> TeamMembersResponse:
        """
        Get paginated list of team members with optional filtering.

        Args:
            user: Authenticated user making the request
            team_id: Team ID
            roles: Optional list of roles to filter by
            search: Optional search query for name or email
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of team members with total count

        Raises:
            HTTPException: If team not found
        """
        organization_id = user.organization_id
        logger.info(
            f"Getting members for team {team_id} by user {user.user_id} (roles={roles}, search={search}, "
            f"limit={limit}, offset={offset})"
        )

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

        members_with_details = team_member_repository.get_team_members_with_details(
            session=self.session,
            team_id=team_id,
            organization_id=organization_id,
            roles=roles,
            search=search,
            limit=limit,
            offset=offset,
        )

        total = team_member_repository.count_team_members(
            session=self.session,
            team_id=team_id,
            organization_id=organization_id,
            roles=roles,
            search=search,
        )

        members = [
            member_dict_to_response(member_dict) for member_dict in members_with_details
        ]

        logger.info(f"Found {len(members)} members (total: {total})")
        return TeamMembersResponse(members=members, total=total)

    def add_team_members(
        self,
        user: User,
        team_id: UUID,
        request: AddTeamMembersRequest,
    ) -> None:
        """
        Add members to a team.

        Args:
            user: Authenticated user making the request
            team_id: Team ID
            request: Add team members request

        Raises:
            HTTPException: If team not found or member already exists
        """
        organization_id = user.organization_id
        actor_user_id = user.user_id
        logger.info(
            f"Adding {len(request.members)} members to team {team_id} by user {actor_user_id}"
        )

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

        # Verify all users exist and belong to organization
        for member in request.members:
            db_user = get_user_by_id(self.session, member.user_id)
            if not db_user:
                logger.error(f"User {member.user_id} not found")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User {member.user_id} not found",
                )

            # Validate user belongs to organization
            if not org_membership_repository.check_user_in_organization(
                self.session, member.user_id, organization_id
            ):
                logger.error(
                    f"User {member.user_id} is not a member of organization {organization_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"User {member.user_id} is not a member of this organization",
                )

        # Add members
        try:
            self._add_members_to_team(team_id, request.members)
            self.session.commit()
            added_members = [
                {"user_id": m.user_id, "role": m.role.value} for m in request.members
            ]
            logger.info(
                "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, team_id=%s, added_members=%s",
                "team.member.add",
                actor_user_id,
                organization_id,
                user.organization_display_name,
                str(team_id),
                added_members,
            )
        except IntegrityError as e:
            self.session.rollback()
            if "duplicate key value violates unique constraint" in str(e.orig):
                logger.error("One or more members already exist in team")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="One or more members already exist in this team",
                )
            logger.error(f"Unexpected error adding members: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to add team members",
            )

    def update_team_members(
        self,
        user: User,
        team_id: UUID,
        request: UpdateTeamMembersRequest,
    ) -> None:
        """
        Update roles for existing team members.

        Args:
            user: Authenticated user making the request
            team_id: Team ID
            request: Update team members request

        Raises:
            HTTPException: If team or member not found
        """
        organization_id = user.organization_id
        logger.info(
            f"Updating {len(request.members)} members in team {team_id} by user {user.user_id}"
        )

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

        # Update each member's role
        changes: list[dict] = []
        for member in request.members:
            membership = team_member_repository.get_membership(
                session=self.session,
                team_id=team_id,
                user_id=member.user_id,
            )
            if not membership:
                logger.error(f"User {member.user_id} is not a member of team {team_id}")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User {member.user_id} is not a member of this team",
                )

            # Capture old role for logging
            old_role = membership.role.value
            membership.role = member.role
            self.session.add(membership)
            changes.append(
                {
                    "user_id": member.user_id,
                    "old_role": old_role,
                    "new_role": member.role.value,
                }
            )

        try:
            self.session.commit()
            logger.info(
                "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, team_id=%s, changes=%s",
                "team.member.update",
                user.user_id,
                organization_id,
                user.organization_display_name,
                str(team_id),
                changes,
            )
        except Exception as e:
            self.session.rollback()
            logger.error(f"Failed to update team members: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update team members",
            )

    def remove_team_members(
        self,
        user: User,
        team_id: UUID,
        request: RemoveTeamMembersRequest,
    ) -> None:
        """
        Remove members from a team.

        Args:
            user: Authenticated user making the request
            team_id: Team ID
            request: Remove team members request

        Raises:
            HTTPException: If team not found
        """
        organization_id = user.organization_id
        logger.info(
            f"Removing {len(request.user_ids)} members from team {team_id} by user {user.user_id}"
        )

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

        # Remove members
        removed_count = 0
        for user_id in request.user_ids:
            membership = team_member_repository.get_membership(
                session=self.session,
                team_id=team_id,
                user_id=user_id,
            )
            if membership:
                self.session.delete(membership)
                removed_count += 1

        try:
            self.session.commit()
            logger.info(
                "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, team_id=%s, removed_user_ids=%s, removed_count=%s",
                "team.member.remove",
                user.user_id,
                organization_id,
                user.organization_display_name,
                str(team_id),
                request.user_ids,
                removed_count,
            )
        except Exception as e:
            self.session.rollback()
            logger.error(f"Failed to remove team members: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to remove team members",
            )

    def _add_members_to_team(
        self,
        team_id: UUID,
        members: list[TeamMemberAddInput],
    ) -> None:
        """
        Add members to a team (internal helper).

        Args:
            team_id: Team ID
            members: List of members to add
        """
        for member in members:
            membership = TeamMembership(
                id=uuid4(),
                team_id=team_id,
                user_id=member.user_id,
                role=member.role,
            )
            self.session.add(membership)
