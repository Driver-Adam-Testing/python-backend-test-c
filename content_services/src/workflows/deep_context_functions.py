from datetime import timedelta

from hatchet_client import hatchet
from hatchet_sdk import Context
from hatchet_sdk.runnables.types import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel


class MakeChangelogInput(BaseModel):
    version_id: str
    install_id: str
    previous_version_id: str | None


@hatchet.task(
    name="make-changelog-workflow",
    execution_timeout=timedelta(minutes=120),
    concurrency=ConcurrencyExpression(
        max_runs=5,
        expression="'make-changelog-workflow'",  # NOTE: must be a string literal to be evaluated as a constant task name
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
async def make_changelog_task(input: MakeChangelogInput, ctx: Context) -> list:
    print("starting make changelog task")
    # Call the function to generate changelog
    await make_changelog(
        input.version_id,
        input.install_id,
        input.previous_version_id,
    )
    print("executed make changelog task")
    return {"status": "completed"}


async def make_changelog(
    version_id: str,
    install_id: str,
    previous_version_id: str | None = None,
) -> None:
    from database.db import async_engine
    from database.models import DerivedContent
    from database.models_enums import ContentKind
    from shared.inspector.inspection.changelog import create_changelog, update_changelog
    from shared.inspector.utils.db import get_version_by_id
    from sqlmodel import delete, select
    from sqlmodel.ext.asyncio.session import AsyncSession

    print(f"Making changelog for version {version_id}")
    content_kind = ContentKind.DEEP_CONTEXT_CHANGELOG
    version = await get_version_by_id(version_id)
    root_node_id = version.root_version_node.node_id
    root_node_relative_path = version.root_version_node.relative_path
    repo_id = version.primary_asset.repository_id
    if repo_id is None:
        print("No repo_id found, skipping changelog generation.")
        return content_kind, "", "", [], "", ""

    async with AsyncSession(async_engine) as session:
        previous_changelog_full = None
        previous_changelog_monthly = None
        previous_sha = None
        if previous_version_id:
            # Fetch previous changelog content
            previous_version = await get_version_by_id(previous_version_id)
            previous_root_node_id = previous_version.root_version_node.node_id
            previous_changelog = (
                await session.exec(
                    select(DerivedContent).where(
                        DerivedContent.node_id == previous_root_node_id,
                        DerivedContent.content_kind == content_kind,
                    )
                )
            ).first()
            if previous_changelog:
                previous_changelog_full = previous_changelog.content
                previous_changelog_monthly = previous_changelog.misc_metadata
                previous_sha = previous_version.vcs_hash

    if previous_sha is None:
        print(f"Creating changelog for version {version_id}")
        changelog = await create_changelog(version_id=version_id, install_id=install_id)
        print("Changelog content:", changelog["overall_changelog"])
    else:
        print("Updating changelog from previous version:", previous_version_id)
        changelog = await update_changelog(
            version_id=version_id,
            install_id=install_id,
            previous_sha=previous_sha,
            previous_monthly_changelogs=previous_changelog_monthly,
            previous_overall_changelog=previous_changelog_full,
        )

    async with AsyncSession(async_engine) as session:
        dc_delete_query = delete(DerivedContent).where(
            DerivedContent.node_id == root_node_id,
            DerivedContent.content_kind == content_kind,
        )
        await session.exec(dc_delete_query)
        await session.commit()

        derived_content = DerivedContent(
            node_id=root_node_id,
            relative_path=root_node_relative_path,
            content_kind=content_kind,
            content=changelog["overall_changelog"],
            misc_metadata=changelog["monthly_changelogs"],
        )
        session.add(derived_content)
        await session.commit()

    name = "Changelog"
    user_context_str = ""
    sources = []
    config_content = ""
    doc_content = changelog["overall_changelog"]
    return (
        content_kind,
        name,
        user_context_str,
        sources,
        config_content,
        doc_content,
    )
