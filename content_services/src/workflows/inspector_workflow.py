from datetime import timedelta

from hatchet_client import hatchet
from hatchet_sdk import Context
from hatchet_sdk.runnables.types import ConcurrencyExpression, ConcurrencyLimitStrategy
from inspector.src.main import inspect_db
from shared.interfaces.hatchet_interfaces import InspectorInput


@hatchet.task(
    name="inspector-workflow",
    execution_timeout=timedelta(minutes=720),
    schedule_timeout=timedelta(hours=8),
    concurrency=ConcurrencyExpression(
        max_runs=5,
        expression="'inspector-workflow'",  # NOTE: must be a string literal to be evaluated as a constant task name
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
async def inspector_task(input: InspectorInput, ctx: Context) -> dict[str, str]:
    print("starting inspector task")
    await inspect_db(input.version_id)
    print("executed inspector task")
    return {"status": "inspection complete"}
