from __future__ import annotations

from datetime import datetime, timezone
from typing import Self

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from database.models import OnboardingChecklist


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


SKIPPABLE_STEPS = [
    "connect_codebase",
    "generate_codebase",
    "setup_mcp",
    "enable_export",
    "generate_autodoc",
    "invite_teammate",
    "configured_rbac",
    "teams",
    "scim_provisioning",
    "sso_sync",
]


class OnboardingChecklistService:
    def __init__(self, session: Session, checklist: OnboardingChecklist) -> None:
        self._session = session
        self._checklist = checklist

    @property
    def checklist(self) -> OnboardingChecklist:
        return self._checklist

    @classmethod
    def get_or_create_checklist(
        cls, session: Session, organization_id: str, user_id: str
    ) -> Self:
        checklist = session.exec(
            select(OnboardingChecklist)
            .where(OnboardingChecklist.organization_id == organization_id)
            .where(OnboardingChecklist.user_id == user_id)
        ).one_or_none()

        if checklist is None:
            checklist = OnboardingChecklist(
                organization_id=organization_id, user_id=user_id
            )
            session.add(checklist)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                checklist = session.exec(
                    select(OnboardingChecklist)
                    .where(OnboardingChecklist.organization_id == organization_id)
                    .where(OnboardingChecklist.user_id == user_id)
                ).one()

        session.refresh(checklist)
        return cls(session, checklist)

    def _set_once(self, field: str, when: datetime) -> None:
        if getattr(self._checklist, field) is None:
            setattr(self._checklist, field, when)
            self._session.add(self._checklist)
            self._session.commit()
            self._session.refresh(self._checklist)

    def mark_invite_teammate_completed(self, when: datetime) -> Self:
        self._set_once("invite_teammate_completed_at", when)
        return self

    def mark_generate_autodoc_completed(self, when: datetime) -> Self:
        self._set_once("generate_autodoc_completed_at", when)
        return self

    def mark_connect_codebase_completed(self, when: datetime) -> Self:
        self._set_once("connect_codebase_completed_at", when)
        return self

    def mark_generate_codebase_completed(self, when: datetime) -> Self:
        self._set_once("generate_codebase_completed_at", when)
        return self

    def mark_setup_mcp_completed(self, when: datetime) -> Self:
        self._set_once("setup_mcp_completed_at", when)
        return self

    def mark_enable_export_completed(self, when: datetime) -> Self:
        self._set_once("enable_export_completed_at", when)
        return self

    def mark_configured_rbac_completed(self, when: datetime) -> Self:
        self._set_once("configured_rbac_completed_at", when)
        return self

    def mark_teams_completed(self, when: datetime) -> Self:
        self._set_once("teams_completed_at", when)
        return self

    def mark_scim_provisioning_completed(self, when: datetime) -> Self:
        self._set_once("scim_provisioning_completed_at", when)
        return self

    def mark_sso_sync_completed(self, when: datetime) -> Self:
        self._set_once("sso_sync_completed_at", when)
        return self

    def skip_step(self, step: str) -> Self:
        if step not in SKIPPABLE_STEPS:
            raise ValueError(f"Invalid step: {step}")
        setattr(self._checklist, f"{step}_skipped_at", _now_utc())
        self._session.add(self._checklist)
        self._session.commit()
        self._session.refresh(self._checklist)
        return self

    def unskip_step(self, step: str) -> Self:
        if step not in SKIPPABLE_STEPS:
            raise ValueError(f"Invalid step: {step}")
        setattr(self._checklist, f"{step}_skipped_at", None)
        self._session.add(self._checklist)
        self._session.commit()
        self._session.refresh(self._checklist)
        return self


