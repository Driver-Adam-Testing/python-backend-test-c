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


# =============================================================================
# Public Route Handlers
# =============================================================================


@router.get("/onboarding-checklist", response_model=OnboardingChecklist)
def get_onboarding_checklist(
    session: CurrentSession, user: UserToken
) -> OnboardingChecklist:
    enforce_org_membership(session, user)

    svc = OnboardingChecklistService.get_or_create_checklist(
        session=session,
        organization_id=user.organization_id,
        user_id=user.user_id,
    )
    _run_all_inferences(session, user, svc)
    _update_checklist_completion(session, svc.checklist)

    return svc.checklist


@router.post("/onboarding-checklist/skip/{step}", response_model=OnboardingChecklist)
def skip_onboarding_step(
    step: str, session: CurrentSession, user: UserToken
) -> OnboardingChecklist:
    enforce_org_membership(session, user)
    _validate_step(step)
    svc = OnboardingChecklistService.get_or_create_checklist(
        session=session,
        organization_id=user.organization_id,
        user_id=user.user_id,
    )
    svc.skip_step(step)
    _run_all_inferences(session, user, svc)
    _update_checklist_completion(session, svc.checklist)
    return svc.checklist


@router.post("/onboarding-checklist/unskip/{step}", response_model=OnboardingChecklist)
def unskip_onboarding_step(
    step: str, session: CurrentSession, user: UserToken
) -> OnboardingChecklist:
    enforce_org_membership(session, user)
    _validate_step(step)
    svc = OnboardingChecklistService.get_or_create_checklist(
        session=session,
        organization_id=user.organization_id,
        user_id=user.user_id,
    )
    svc.unskip_step(step)
    _run_all_inferences(session, user, svc)
    _update_checklist_completion(session, svc.checklist)
    return svc.checklist


# =============================================================================
# Private Helper Functions
# =============================================================================


def _validate_step(step: str) -> None:
    if step not in SKIPPABLE_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid step: {step}. Valid steps are: {', '.join(SKIPPABLE_STEPS)}",
        )


def _run_all_inferences(
    session: CurrentSession,
    user: UserToken,
    svc: OnboardingChecklistService,
) -> None:
    checklist = svc.checklist
    _infer_connect_codebase(session, user, svc, checklist)
    _infer_generate_codebase(session, user, svc, checklist)
    _infer_setup_mcp(session, user, svc, checklist)
    _infer_enable_export(session, user, svc, checklist)
    _infer_configured_rbac(session, user, svc, checklist)
    _infer_teams_completed(session, user, svc, checklist)


def _is_step_done(
    completed_at: datetime | None, skipped_at: datetime | None
) -> bool:
    return completed_at is not None or skipped_at is not None


def _get_step_timestamp(
    completed_at: datetime | None, skipped_at: datetime | None
) -> datetime | None:
    return completed_at if completed_at is not None else skipped_at


def _infer_connect_codebase(
    session: CurrentSession,
    user: UserToken,
    svc: OnboardingChecklistService,
    checklist: OnboardingChecklist,
) -> None:
    if _is_step_done(
        checklist.connect_codebase_completed_at, checklist.connect_codebase_skipped_at
    ):
        return
    first_codebase = session.exec(
        select(PrimaryAsset)
        .where(PrimaryAsset.organization_id == user.organization_id)
        .where(PrimaryAsset.kind == PrimaryAssetKind.CODEBASE)
        .order_by(PrimaryAsset.created_at.asc())
    ).first()
    if first_codebase is not None:
        svc.mark_connect_codebase_completed(first_codebase.created_at)


def _infer_generate_codebase(
    session: CurrentSession,
    user: UserToken,
    svc: OnboardingChecklistService,
    checklist: OnboardingChecklist,
) -> None:
    if _is_step_done(
        checklist.generate_codebase_completed_at, checklist.generate_codebase_skipped_at
    ):
        return
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


def _infer_setup_mcp(
    session: CurrentSession,
    user: UserToken,
    svc: OnboardingChecklistService,
    checklist: OnboardingChecklist,
) -> None:
    if _is_step_done(checklist.setup_mcp_completed_at, checklist.setup_mcp_skipped_at):
        return
    api_key = session.exec(
        select(ApiKey)
        .where(ApiKey.organization_id == user.organization_id)
        .where(ApiKey.user_id == user.user_id)
        .where(ApiKey.last_used_at.is_not(None))
        .order_by(ApiKey.last_used_at.asc())
    ).first()
    if api_key is not None:
        svc.mark_setup_mcp_completed(api_key.last_used_at)


def _infer_enable_export(
    session: CurrentSession,
    user: UserToken,
    svc: OnboardingChecklistService,
    checklist: OnboardingChecklist,
) -> None:
    if _is_step_done(
        checklist.enable_export_completed_at, checklist.enable_export_skipped_at
    ):
        return
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


def _infer_configured_rbac(
    session: CurrentSession,
    user: UserToken,
    svc: OnboardingChecklistService,
    checklist: OnboardingChecklist,
) -> None:
    if _is_step_done(
        checklist.configured_rbac_completed_at, checklist.configured_rbac_skipped_at
    ):
        return
    # Require 3+ grants since initial admin users get automatic grants
    role_grants = session.exec(
        select(PrimaryAssetRoleGrant)
        .where(PrimaryAssetRoleGrant.organization_id == user.organization_id)
        .order_by(PrimaryAssetRoleGrant.created_at.asc())
        .limit(3)
    ).all()
    if len(role_grants) >= 3:
        svc.mark_configured_rbac_completed(role_grants[2].created_at)


def _infer_teams_completed(
    session: CurrentSession,
    user: UserToken,
    svc: OnboardingChecklistService,
    checklist: OnboardingChecklist,
) -> None:
    if _is_step_done(checklist.teams_completed_at, checklist.teams_skipped_at):
        return
    first_team = session.exec(
        select(Team).where(Team.organization_id == user.organization_id)
    ).first()
    if first_team is not None:
        # Team model lacks created_at field
        svc.mark_teams_completed(datetime.now(timezone.utc))


def _update_checklist_completion(
    session: CurrentSession, checklist: OnboardingChecklist
) -> None:
    required_steps_done = [
        _is_step_done(
            checklist.connect_codebase_completed_at,
            checklist.connect_codebase_skipped_at,
        ),
        _is_step_done(checklist.teams_completed_at, checklist.teams_skipped_at),
        _is_step_done(
            checklist.generate_autodoc_completed_at,
            checklist.generate_autodoc_skipped_at,
        ),
    ]

    if not all(required_steps_done):
        if checklist.checklist_completed_at is not None:
            checklist.checklist_completed_at = None
            session.add(checklist)
            session.commit()
            session.refresh(checklist)
        return

    completed_dates = [
        ts
        for ts in [
            _get_step_timestamp(
                checklist.connect_codebase_completed_at,
                checklist.connect_codebase_skipped_at,
            ),
            _get_step_timestamp(checklist.teams_completed_at, checklist.teams_skipped_at),
            _get_step_timestamp(
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
