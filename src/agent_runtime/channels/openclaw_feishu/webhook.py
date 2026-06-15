from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException

from agent_runtime.channels.openclaw_feishu.adapter import (
    build_support_case_request_from_openclaw,
    build_support_case_request_from_openclaw_batch,
)
from agent_runtime.channels.openclaw_feishu.responder import build_openclaw_thread_reply
from agent_runtime.copilot.case_context import SupportCaseRequest
from agent_runtime.copilot.runtime import build_support_runtime_session, run_support_case_request
from agent_runtime.llm import configure_agents_runtime
from agent_runtime.settings import Settings, get_settings


router = APIRouter(prefix="/channels/openclaw-feishu", tags=["openclaw-feishu"])


@router.post("/support-case")
async def openclaw_feishu_support_case(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    x_openclaw_feishu_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    _verify_openclaw_secret(settings, authorization, x_openclaw_feishu_secret)
    settings = configure_agents_runtime(settings)
    request = _request_from_payload(payload)
    session = build_support_runtime_session(settings, request.session_id or request.request_id)
    result = await run_support_case_request(
        request,
        settings,
        entrypoint="openclaw_feishu",
        source_label="OpenClaw 飞书客服话题群",
        session=session,
    )
    return build_openclaw_thread_reply(result)


def _request_from_payload(payload: dict[str, Any]) -> SupportCaseRequest:
    messages = payload.get("messages")
    if isinstance(messages, list):
        valid_messages = [message for message in messages if isinstance(message, dict)]
        if not valid_messages:
            raise HTTPException(status_code=400, detail="OpenClaw Feishu batch must include at least one message")
        return build_support_case_request_from_openclaw_batch(valid_messages, batch_id=str(payload.get("batchId") or ""))
    message = payload.get("message")
    if isinstance(message, dict):
        return build_support_case_request_from_openclaw(message)
    return build_support_case_request_from_openclaw(payload)


def _verify_openclaw_secret(
    settings: Settings,
    authorization: str | None,
    x_openclaw_feishu_secret: str | None,
) -> None:
    if not settings.openclaw_feishu_bridge_secret:
        return
    bearer = f"Bearer {settings.openclaw_feishu_bridge_secret}"
    if authorization == bearer or x_openclaw_feishu_secret == settings.openclaw_feishu_bridge_secret:
        return
    raise HTTPException(status_code=403, detail="invalid OpenClaw Feishu bridge secret")
