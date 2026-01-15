import logging
import os
import signal
import sys

import truststore
import truststore._api as tapi
from hatchet_client import hatchet
from worker_config import HatchetWorkerType
from workflows.analytics_workflow import analytics_task
from workflows.auth0_sync_workflow import (
    auth0_sync_task,
    process_auth0_event_task,
    scheduled_auth0_sync_task,
)
from workflows.autodocs_functions import llm_generate_task
from workflows.autodocs_workflow import autodocs_task
from workflows.deep_context_functions import make_changelog_task
from workflows.inspector_functions import (
    codebase_tags_task,
    deep_context_docs_task,
    export_tech_docs_task,
    folder_doc_task,
    symbol_doc_task,
    tech_doc_task,
    toplevel_doc_task,
)
from workflows.inspector_workflow import inspector_task
from workflows.onboarding_workflows import (
    connect_repos_for_installation_task,
    handle_azure_devops_events_task,
    handle_bitbucket_dc_events_task,
    handle_bitbucket_events_task,
    handle_github_events_task,
    handle_gitlab_events_task,
    run_codebase_connection_task,
)
from workflows.pdf_processing_workflow import pdf_processing_task

# Suppress noisy HTTP/AWS library loggers before basicConfig
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

truststore.inject_into_ssl()
# Patch botocore to use truststore's SSLContext (see https://github.com/sethmlarson/truststore/pull/180)
try:
    import botocore.httpsession

    botocore.httpsession.SSLContext = tapi.SSLContext
except ImportError:
    pass

heavy_workflow_set = [
    pdf_processing_task,
    inspector_task,
    tech_doc_task,
    folder_doc_task,
    symbol_doc_task,
    toplevel_doc_task,
    autodocs_task,
    run_codebase_connection_task,
    llm_generate_task,
    make_changelog_task,
    deep_context_docs_task,
    export_tech_docs_task,
    codebase_tags_task,
]

base_workflow_set = [
    auth0_sync_task,
    process_auth0_event_task,
    scheduled_auth0_sync_task,
    handle_github_events_task,
    handle_azure_devops_events_task,
    handle_bitbucket_dc_events_task,
    handle_bitbucket_events_task,
    handle_gitlab_events_task,
    connect_repos_for_installation_task,
]

analytics_workflow_set = [
    analytics_task,
]


def _wrap_signal_handler(worker: object, worker_type: HatchetWorkerType) -> None:
    """Wrap the SDK's signal handler to add application-level logging."""
    original_handler = worker._handle_exit_signal

    def logged_handler(signum: int, frame: object) -> None:
        sig_name = signal.Signals(signum).name
        logger.info(
            f"Received {sig_name} - initiating graceful shutdown for {worker_type}-worker, "
            "waiting for active tasks to complete..."
        )
        original_handler(signum, frame)

    worker._handle_exit_signal = logged_handler


def main() -> None:
    worker_type = HatchetWorkerType(os.environ["WORKFLOW_SET_NAME"])
    match worker_type:
        case HatchetWorkerType.ANALYTICS:
            workflows = analytics_workflow_set
        case HatchetWorkerType.HEAVY:
            workflows = heavy_workflow_set
        case HatchetWorkerType.BASE:
            workflows = base_workflow_set
        case _:
            raise ValueError(f"Unknown worker type: {worker_type}")
    worker = hatchet.worker(
        f"{worker_type}-worker",
        slots=250,
        workflows=workflows,
    )
    _wrap_signal_handler(worker, worker_type)
    worker.start()


if __name__ == "__main__":
    main()
