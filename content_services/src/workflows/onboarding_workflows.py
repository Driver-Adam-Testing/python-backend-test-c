from datetime import timedelta

from hatchet_client import hatchet
from hatchet_sdk import Context
from hatchet_sdk.runnables.types import ConcurrencyExpression, ConcurrencyLimitStrategy
from onboarding.onboard import (
    connect_repos_for_installation,
    handle_azure_devops_events,
    handle_bitbucket_dc_events,
    handle_bitbucket_events,
    handle_github_events,
    handle_gitlab_events,
    run_codebase_connection,
)
from shared.interfaces.hatchet_interfaces import (
    ConnectReposForInstallationInput,
    HandleAzureDevopsEventsInput,
    HandleBitbucketDCEventsInput,
    HandleBitbucketEventsInput,
    HandleGithubEventsInput,
    HandleGitlabEventsInput,
    RunCodebaseConnectionInput,
)


@hatchet.task(
    name="handle-github-events-workflow",
    execution_timeout=timedelta(minutes=60),
    concurrency=ConcurrencyExpression(
        max_runs=5,
        expression="'handle-github-events-workflow'",  # NOTE: must be a string literal to be evaluated as a constant task name
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
def handle_github_events_task(input: HandleGithubEventsInput, ctx: Context) -> None:
    print("starting handle github events task")
    handle_github_events(
        input.installation_id,
        input.org_id,
        input.repos_added,
        input.repos_deleted,
        input.repos_pushed,
    )
    print("executed handle github events task")


@hatchet.task(
    name="handle-gitlab-events-workflow",
    execution_timeout=timedelta(minutes=60),
    concurrency=ConcurrencyExpression(
        max_runs=5,
        expression="'handle-gitlab-events-workflow'",  # NOTE: must be a string literal to be evaluated as a constant task name
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
def handle_gitlab_events_task(input: HandleGitlabEventsInput, ctx: Context) -> None:
    print("starting handle gitlab events task")
    handle_gitlab_events(
        input.installation_id,
        input.org_id,
        input.repos_added,
        input.repos_deleted,
        input.repos_pushed,
    )
    print("executed handle gitlab events task")


@hatchet.task(
    name="handle-bitbucket-events-workflow",
    execution_timeout=timedelta(minutes=60),
    concurrency=ConcurrencyExpression(
        max_runs=5,
        expression="'handle-bitbucket-events-workflow'",  # NOTE: must be a string literal to be evaluated as a constant task name
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
def handle_bitbucket_events_task(
    input: HandleBitbucketEventsInput, ctx: Context
) -> None:
    print("starting handle bitbucket events task")
    handle_bitbucket_events(
        input.installation_id,
        input.org_id,
        input.repos_added,
        input.repos_deleted,
        input.repos_pushed,
    )
    print("executed handle bitbucket events task")


@hatchet.task(
    name="handle-azure-devops-events-workflow",
    execution_timeout=timedelta(minutes=60),
    concurrency=ConcurrencyExpression(
        max_runs=5,
        expression="'handle-azure-devops-events-workflow'",  # NOTE: must be a string literal to be evaluated as a constant task name
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
def handle_azure_devops_events_task(
    input: HandleAzureDevopsEventsInput, ctx: Context
) -> None:
    print("starting handle azure devops events task")
    handle_azure_devops_events(
        input.installation_id,
        input.org_id,
        input.repos_added,
        input.repos_deleted,
        input.repos_pushed,
    )
    print("executed handle azure devops events task")


@hatchet.task(
    name="handle-bitbucket-dc-events-workflow",
    execution_timeout=timedelta(minutes=60),
    concurrency=ConcurrencyExpression(
        max_runs=5,
        expression="'handle-bitbucket-dc-events-workflow'",  # NOTE: must be a string literal to be evaluated as a constant task name
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
def handle_bitbucket_dc_events_task(
    input: HandleBitbucketDCEventsInput, ctx: Context
) -> None:
    print("starting handle bitbucket dc events task")
    handle_bitbucket_dc_events(
        input.installation_id,
        input.org_id,
        input.repos_added,
        input.repos_deleted,
        input.repos_pushed,
    )
    print("executed handle bitbucket dc events task")


@hatchet.task(
    name="connect-repos-for-installation-workflow",
    execution_timeout=timedelta(minutes=60),
    concurrency=ConcurrencyExpression(
        max_runs=1,
        expression="'connect-repos-for-installation-workflow'",  # NOTE: must be a string literal to be evaluated as a constant task name
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
def connect_repos_for_installation_task(
    input: ConnectReposForInstallationInput, ctx: Context
) -> None:
    print("starting connect repos for installation task")
    connect_repos_for_installation(input.github_installation_id)
    print("executed connect repos for installation task")


@hatchet.task(
    name="run-codebase-connection-workflow",
    execution_timeout=timedelta(minutes=740),
    concurrency=ConcurrencyExpression(
        max_runs=5,
        expression="'run-codebase-connection-workflow'",  # NOTE: must be a string literal to be evaluated as a constant task name
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
    schedule_timeout=timedelta(hours=1),
)
def run_codebase_connection_task(
    input: RunCodebaseConnectionInput, ctx: Context
) -> None:
    print("starting run codebase connection task")
    run_codebase_connection(
        input.presigned_url,
        input.provisional_codebase_name,
        input.org_id,
        input.version_id,
        input.provider,
    )
    print("executed run codebase connection task")
