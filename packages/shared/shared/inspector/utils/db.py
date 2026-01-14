import uuid

from database.models import (
    DerivedContent,
    GitProviderAppInstallation,
    InspectorRun,
    Node,
    Version,
    VersionNode,
)
from database.models_enums import ContentKind, NodeKind, VersionStatus
from sqlmodel.ext.asyncio.session import AsyncSession


async def get_version_by_id(version_id: uuid.UUID) -> Version:
    from database.db import async_engine
    from sqlalchemy.orm import selectinload
    from sqlmodel import select

    async with AsyncSession(async_engine) as session:
        statement = (
            select(Version)
            .where(Version.id == version_id)
            .options(
                selectinload(Version.primary_asset),
                selectinload(Version.root_version_node).selectinload(VersionNode.node),
            )
        )
        return (await session.exec(statement)).one()


def sync_get_version_by_id(version_id: uuid.UUID) -> Version:
    from database.db import engine
    from sqlalchemy.orm import selectinload
    from sqlmodel import Session, select

    with Session(engine) as session:
        statement = (
            select(Version)
            .where(Version.id == version_id)
            .options(
                selectinload(Version.primary_asset),
                selectinload(Version.root_version_node).selectinload(VersionNode.node),
            )
        )
        return session.exec(statement).one()


async def delete_version_by_id(version_id: uuid.UUID) -> None:
    from database.db import async_engine
    from sqlmodel import select

    async with AsyncSession(async_engine) as session:
        statement = select(Version).where(Version.id == version_id)
        version = (await session.exec(statement)).one()
        await session.delete(version)
        await session.commit()


async def get_version_nodes_by_version_id(version_id: uuid.UUID) -> list[VersionNode]:
    from database.db import async_engine
    from sqlmodel import select

    async with AsyncSession(async_engine) as session:
        statement = select(VersionNode).where(VersionNode.version_id == version_id)
        results = await session.exec(statement)
        return results.all()


async def try_get_prev_version(version_id: uuid.UUID) -> None | Version:
    from database.db import async_engine
    from sqlalchemy.orm import selectinload
    from sqlmodel import select

    async with AsyncSession(async_engine) as session:
        stmt = select(Version).where(Version.id == version_id)
        version = (await session.exec(stmt)).one()
        stmt = (
            select(Version)
            .where(Version.id == version.previous_version_id)
            .where(Version.status.in_([VersionStatus.GENERATION_COMPLETE]))
            .options(
                selectinload(Version.primary_asset),
                selectinload(Version.root_version_node).selectinload(VersionNode.node),
            )
        )
        previous_version = (await session.exec(stmt)).first()
        return previous_version


async def create_inspector_run(version_id: uuid.UUID) -> uuid.UUID:
    import modal
    from database.db import async_engine

    modal_call_id = modal.current_function_call_id()

    async with AsyncSession(async_engine) as session:
        inspector_run = InspectorRun(
            inspection_version_id=None,
            version_id=version_id,
            call_id=modal_call_id,
        )
        session.add(inspector_run)
        await session.commit()
        await session.refresh(inspector_run)
        run_id = inspector_run.id

    return run_id


async def try_get_latest_run_from_version_id(version_id: uuid.UUID) -> uuid.UUID | None:
    from database.db import async_engine
    from sqlmodel import select

    async with AsyncSession(async_engine) as session:
        statement = (
            select(InspectorRun)
            .where(
                InspectorRun.version_id == version_id
            )  # TODO this fk name will probably be version_id once updated
            .order_by(InspectorRun.created_at.desc())
        )
        result = (await session.exec(statement)).first()
        # It is possible, though uncommon, that a version won't have a run
        # This happens, for example, for codebases that were created before runs/versions were introduced, but versions
        # were created during a migration for those codebases
        if result is None:
            return None
        return result.id


# TODO : get analyable nodes by version_id
async def get_analyzable_version_nodes_by_version_id(
    version_id: uuid.UUID, content_types: set[NodeKind]
) -> list[Node]:
    from database.db import async_engine
    from sqlalchemy.orm import selectinload
    from sqlmodel import select

    async with AsyncSession(async_engine) as session:
        statement = (
            select(VersionNode)
            .join(VersionNode.node)
            .where(
                VersionNode.version_id == version_id,
                Node.kind.in_(content_types),
            )
            .options(selectinload(VersionNode.node))
        )

        results = await session.exec(statement)

    res_list = []
    for res in results.all():
        is_file = res.node.kind == NodeKind.CODEBASE_FILE
        is_directory = res.node.kind == NodeKind.CODEBASE_DIRECTORY
        is_analyzable_file = is_file and res.misc_metadata.get("is_analyzable") is True
        if is_directory or is_analyzable_file:
            res_list.append(res)
    return res_list


async def get_source_code_derived_content(version_node_id: uuid.UUID) -> DerivedContent:
    from database.db import async_engine
    from sqlmodel import select

    async with AsyncSession(async_engine) as session:
        statement = (
            select(DerivedContent)
            .join(VersionNode, DerivedContent.node_id == VersionNode.node_id)
            .where(
                VersionNode.id == version_node_id,
                DerivedContent.content_kind == ContentKind.CODEBASE_FILE,
            )
        )
        return (await session.exec(statement)).one()


async def get_all_derived_content_by_version_node_id(
    version_node_id: uuid.UUID,
    content_kinds: set[ContentKind] | None = None,
) -> list[DerivedContent]:
    from database.db import async_engine
    from sqlmodel import select

    async with AsyncSession(async_engine) as session:
        statement = (
            select(DerivedContent)
            .join(VersionNode, DerivedContent.node_id == VersionNode.node_id)
            .where(
                VersionNode.id == version_node_id,
            )
        )
        if content_kinds is not None:
            statement = statement.where(DerivedContent.content_kind.in_(content_kinds))
        return (await session.exec(statement)).all()


def sync_get_all_derived_content_by_version_node_id(
    version_node_id: uuid.UUID,
    content_kinds: set[ContentKind] | None = None,
) -> list[DerivedContent]:
    from database.db import engine
    from sqlmodel import Session, select

    with Session(engine) as session:
        statement = (
            select(DerivedContent)
            .join(VersionNode, DerivedContent.node_id == VersionNode.node_id)
            .where(
                VersionNode.id == version_node_id,
            )
        )
        if content_kinds is not None:
            statement = statement.where(DerivedContent.content_kind.in_(content_kinds))
        results = session.exec(statement)
        return results.all()


async def get_node_from_version_node_id(
    version_node_id: uuid.UUID,
) -> Node:
    from database.db import async_engine
    from sqlalchemy.orm import selectinload
    from sqlmodel import select

    async with AsyncSession(async_engine) as session:
        statement = (
            select(VersionNode)
            .where(VersionNode.id == version_node_id)
            .options(selectinload(VersionNode.node))
        )
        version_node = (await session.exec(statement)).one()
        return version_node.node


def sync_get_node_from_version_node_id(
    version_node_id: uuid.UUID,
) -> Node:
    from database.db import engine
    from sqlalchemy.orm import selectinload
    from sqlmodel import Session, select

    with Session(engine) as session:
        statement = (
            select(VersionNode)
            .where(VersionNode.id == version_node_id)
            .options(selectinload(VersionNode.node))
        )
        version_node = session.exec(statement).one()
        return version_node.node


def get_usage_balance_in_bytes(
    org_id: str,
) -> int:
    from database.db import engine
    from shared.interfaces.usage.usage_schema import UsageMetricUnitType
    from shared.usage.usage_service import UsageService
    from sqlmodel import Session

    with Session(engine) as session:
        usage_balance = (
            UsageService(session)
            .get_usage_balance(
                org_id
            )  # Since our app reports usage in SLOC this function returns the balance in SLOC
            .convert_to(UsageMetricUnitType.BYTES)  # Convert SLOC to bytes
        )
        return usage_balance.balance


def git_provider_app_installation_by_id(
    installation_id: str,
) -> GitProviderAppInstallation:
    from database.db import engine
    from sqlalchemy.orm import selectinload
    from sqlmodel import Session, select

    with Session(engine) as session:
        query = (
            select(GitProviderAppInstallation)
            .where(
                GitProviderAppInstallation.id == installation_id,
            )
            .options(selectinload(GitProviderAppInstallation.git_provider_app))
        )
        return session.exec(query).one()
