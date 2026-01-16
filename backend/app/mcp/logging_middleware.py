import asyncio
import json
import logging
from datetime import datetime
from enum import StrEnum
from logging import Formatter, LogRecord
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult  # noqa: TCH002
from pydantic import BaseModel, ValidationError

from app.mcp.oauth_auth import get_user_from_token
from app.services.onboarding_checklist_service import mark_setup_mcp_completed_async

logger = logging.getLogger(__name__)


class _McpComponentType(StrEnum):
    TOOL = "tool"
    PROMPT = "prompt"
    RESOURCE = "resource"


class _McpLogData(BaseModel):
    component_name: str
    component_type: _McpComponentType
    params: dict[str, Any] | None
    payload: str | None
    error_message: str | None
    org_id: str
    org_name: str
    user_id: str
    user_email: str


class _McpJsonFormatter(Formatter):
    def format(self, record: LogRecord) -> str:
        json_record = {
            "level": record.levelname,
            "timestamp": datetime.fromtimestamp(record.created).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "name": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "getMessage",
                "exc_info",
                "exc_text",
                "stack_info",
                "message",
                "taskName",
            }:
                json_record[key] = value

        return json.dumps(json_record)


class DriverMcpToolResponse(BaseModel):
    payload: Any | None
    error_message: str | None


class McpLoggingMiddleware(Middleware):
    def __init__(self) -> None:
        self._RESPONSE_MAX_LENGTH = 500
        self._logger = logging.getLogger(__name__)

        handler = logging.StreamHandler()
        handler.setFormatter(_McpJsonFormatter())
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    async def on_initialize(self, ctx: MiddlewareContext, call_next: any) -> None:
        await call_next(ctx)

        # Mark MCP setup complete after successful initialization
        try:
            user = get_user_from_token()
            asyncio.create_task(
                mark_setup_mcp_completed_async(user.organization_id, user.user_id)
            )
        except Exception:
            logger.exception("Failed to get user for MCP setup completion tracking")

    async def on_call_tool(self, ctx: MiddlewareContext, call_next: any) -> any:
        user = get_user_from_token()

        log_data = _McpLogData(
            component_name=ctx.message.name,
            component_type=_McpComponentType.TOOL,
            params=ctx.message.arguments,
            payload=None,
            error_message=None,
            org_id=user.organization_id,
            org_name=user.organization_display_name or "",
            user_id=user.user_id,
            user_email=user.email or "",
        )

        try:
            tool_result: ToolResult = await call_next(ctx)
        except Exception as e:
            log_data.error_message = str(e)

            self._logger.error(
                "Tool call raised an exception", extra=log_data.model_dump()
            )
            raise
        try:
            response = DriverMcpToolResponse.model_validate(
                tool_result.structured_content
            )
            if response.error_message:
                log_data.error_message = response.error_message

                self._logger.error(
                    "Tool call completed with errors", extra=log_data.model_dump()
                )
            else:
                payload_str = str(response.payload)
                if len(payload_str) > self._RESPONSE_MAX_LENGTH:
                    payload_str = payload_str[: self._RESPONSE_MAX_LENGTH] + "..."
                log_data.payload = payload_str

                self._logger.info(
                    "Tool call completed without errors",
                    extra=log_data.model_dump(),
                )

        except ValidationError as e:
            raise ValidationError(
                f"All Driver MCP tools must return DriverMcpToolResponse.  Error: {e}"
            ) from e

        return tool_result

    async def on_get_prompt(self, ctx: MiddlewareContext, call_next: any) -> any:
        user = get_user_from_token()

        log_data = _McpLogData(
            component_name=ctx.message.name,
            component_type=_McpComponentType.PROMPT,
            params=None,
            payload=None,
            error_message=None,
            org_id=user.organization_id,
            org_name=user.organization_display_name or "",
            user_id=user.user_id,
            user_email=user.email or "",
        )

        self._logger.info("Prompt requested", extra=log_data.model_dump())

        return await call_next(ctx)
