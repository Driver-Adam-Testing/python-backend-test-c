from datetime import datetime, timezone

from database.models import (
    ApiKey,
    OnboardingChecklist,
    PrimaryAsset,
    PrimaryAssetRoleGrant,
    Team,
    Version,
)
from database.models_enums import PrimaryAssetKind, VersionStatus
from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.api.auth import UserToken
from app.api.session import CurrentSession
from app.authorization.fastapi import enforce_org_membership
from app.services.onboarding_checklist_service import (
    SKIPPABLE_STEPS,
    OnboardingChecklistService,
)

router = APIRouter()


@router.get("/onboarding-checklist", response_model=OnboardingChecklist)
def get_onboarding_checklist(
    session: CurrentSession, user: UserToken
) -> OnboardingChecklist:
    enforce_org_membership(session, user)

    # Fetch or create the checklist record
    svc = OnboardingChecklistService.get_or_create_checklist(
        session=session,
        organization_id=user.organization_id,
        user_id=user.user_id,
    )
    checklist = svc.checklist

    # If connect codebase is not marked complete, attempt to infer it
    if checklist.connect_codebase_completed_at is None:
        first_codebase = session.exec(
            select(PrimaryAsset)
            .where(PrimaryAsset.organization_id == user.organization_id)
            .where(PrimaryAsset.kind == PrimaryAssetKind.CODEBASE)
            .order_by(PrimaryAsset.created_at.asc())
        ).first()

        if first_codebase is not None:
            svc.mark_connect_codebase_completed(first_codebase.created_at)

    # If generate codebase is not marked complete, infer from earliest completed version
    if checklist.generate_codebase_completed_at is None:
        first_completed_version = session.exec(
            select(Version)
            .join(PrimaryAsset, Version.primary_asset_id == PrimaryAsset.id)
            .where(PrimaryAsset.organization_id == user.organization_id)
            .where(PrimaryAsset.kind == PrimaryAssetKind.CODEBASE)
            .where(
                Version.status.in_(
                    [VersionStatus.GENERATION_COMPLETE, VersionStatus.GENERATING]
                )
            )
            .order_by(Version.created_at.asc())
        ).first()

        if first_completed_version is not None:
            svc.mark_generate_codebase_completed(first_completed_version.created_at)

    # If setup MCP is not marked complete, infer from API key last_used_at
    if checklist.setup_mcp_completed_at is None:
        api_key = session.exec(
            select(ApiKey)
            .where(ApiKey.organization_id == user.organization_id)
            .where(ApiKey.user_id == user.user_id)
            .where(ApiKey.last_used_at.is_not(None))
            .order_by(ApiKey.last_used_at.asc())
        ).first()

        if api_key is not None:
            svc.mark_setup_mcp_completed(api_key.last_used_at)

    # If enable export is not marked complete, infer from any codebase with auto commit enabled
    if checklist.enable_export_completed_at is None:
        auto_export_codebase = session.exec(
            select(PrimaryAsset)
            .where(PrimaryAsset.organization_id == user.organization_id)
            .where(PrimaryAsset.kind == PrimaryAssetKind.CODEBASE)
            .where(PrimaryAsset.codebase_settings_auto_commit_docs.is_(True))
            .order_by(PrimaryAsset.updated_at.asc())
        ).first()

        if auto_export_codebase is not None:
            svc.mark_enable_export_completed(
                auto_export_codebase.updated_at or auto_export_codebase.created_at
            )

    # If configured_rbac is not marked complete, infer from 3+ role grants existing
    # (initial admin users typically get automatic grants, so we check for additional config)
    if checklist.configured_rbac_completed_at is None:
        role_grants = session.exec(
            select(PrimaryAssetRoleGrant)
            .where(PrimaryAssetRoleGrant.organization_id == user.organization_id)
            .order_by(PrimaryAssetRoleGrant.created_at.asc())
            .limit(3)
        ).all()

        if len(role_grants) >= 3:
            # Use the timestamp of the 3rd grant as the completion time
            svc.mark_configured_rbac_completed(role_grants[2].created_at)

    # If teams_completed is not marked complete, infer from any teams existing
    if checklist.teams_completed_at is None:
        first_team = session.exec(
            select(Team)
            .where(Team.organization_id == user.organization_id)
        ).first()

        if first_team is not None:
            # Team model doesn't have created_at, use current time
            svc.mark_teams_completed(datetime.now(timezone.utc))

    # Helper to check if a step is done (completed or skipped)
    def is_step_done(
        completed_at: datetime | None, skipped_at: datetime | None
    ) -> bool:
        return completed_at is not None or skipped_at is not None

    def get_step_timestamp(
        completed_at: datetime | None, skipped_at: datetime | None
    ) -> datetime | None:
        if completed_at is not None:
            return completed_at
        return skipped_at

    # Check the 3 required steps (done = completed OR skipped)
    required_steps_done = [
        is_step_done(
            checklist.connect_codebase_completed_at,
            checklist.connect_codebase_skipped_at,
        ),
        is_step_done(checklist.teams_completed_at, checklist.teams_skipped_at),
        is_step_done(
            checklist.generate_autodoc_completed_at,
            checklist.generate_autodoc_skipped_at,
        ),
    ]

    if all(required_steps_done):
        # Get the timestamps for completed/skipped steps
        completed_dates = [
            ts
            for ts in [
                get_step_timestamp(
                    checklist.connect_codebase_completed_at,
                    checklist.connect_codebase_skipped_at,
                ),
                get_step_timestamp(
                    checklist.teams_completed_at, checklist.teams_skipped_at
                ),
                get_step_timestamp(
                    checklist.generate_autodoc_completed_at,
                    checklist.generate_autodoc_skipped_at,
                ),
            ]
            if ts is not None
        ]
        if completed_dates:
            newest = max(completed_dates)
            if checklist.checklist_completed_at != newest:
                checklist.checklist_completed_at = newest
                session.add(checklist)
                session.commit()
                session.refresh(checklist)

    return checklist


@router.post("/onboarding-checklist/skip/{step}", response_model=OnboardingChecklist)
def skip_onboarding_step(
    step: str, session: CurrentSession, user: UserToken
) -> OnboardingChecklist:
    """Skip a step in the onboarding checklist."""
    enforce_org_membership(session, user)

    if step not in SKIPPABLE_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid step: {step}. Valid steps are: {', '.join(SKIPPABLE_STEPS)}",
        )

    svc = OnboardingChecklistService.get_or_create_checklist(
        session=session,
        organization_id=user.organization_id,
        user_id=user.user_id,
    )
    svc.skip_step(step)

    return svc.checklist


@router.post("/onboarding-checklist/unskip/{step}", response_model=OnboardingChecklist)
def unskip_onboarding_step(
    step: str, session: CurrentSession, user: UserToken
) -> OnboardingChecklist:
    """Unskip a step in the onboarding checklist."""
    enforce_org_membership(session, user)

    if step not in SKIPPABLE_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid step: {step}. Valid steps are: {', '.join(SKIPPABLE_STEPS)}",
        )

    svc = OnboardingChecklistService.get_or_create_checklist(
        session=session,
        organization_id=user.organization_id,
        user_id=user.user_id,
    )
    svc.unskip_step(step)

    return svc.checklist
