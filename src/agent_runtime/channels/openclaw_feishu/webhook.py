from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from agent_runtime.channels.openclaw_feishu.adapter import (
    build_support_case_request_from_openclaw,
    build_support_case_request_from_openclaw_batch,
)
from agent_runtime.channels.openclaw_feishu.responder import build_openclaw_thread_reply
from agent_runtime.copilot.answer_contract import SupportAnswer
from agent_runtime.copilot.case_context import SupportCaseRequest
from agent_runtime.copilot.runtime import build_support_runtime_session, run_support_case_request
from agent_runtime.llm import configure_agents_runtime
from agent_runtime.settings import Settings, get_settings


router = APIRouter(prefix="/channels/openclaw-feishu", tags=["openclaw-feishu"])


@router.get("/health")
async def openclaw_feishu_health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "ok": True,
        "channel": "openclaw_feishu",
        "runtime": "support_copilot",
        "requiresSecret": bool(settings.openclaw_feishu_bridge_secret),
    }


@router.post("/support-case")
async def openclaw_feishu_support_case(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    x_openclaw_feishu_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    _verify_openclaw_secret(settings, authorization, x_openclaw_feishu_secret)
    request = _request_from_payload(payload)
    if payload.get("contractOnly") is True:
        return build_openclaw_thread_reply(_contract_only_result(request))

    settings = configure_agents_runtime(settings)
    session = build_support_runtime_session(settings, request.session_id or request.request_id)
    result = await run_support_case_request(
        request,
        settings,
        entrypoint="openclaw_feishu",
        source_label="OpenClaw 飞书客服话题群",
        session=session,
    )
    return build_openclaw_thread_reply(result)


def _contract_only_result(request: SupportCaseRequest) -> SimpleNamespace:
    return SimpleNamespace(
        request=request,
        contract_issues=[],
        answer=SupportAnswer(
            issue_type="unknown",
            run_mode="Agent SDK",
            confidence="低",
            confidence_reason="OpenClaw 通道检查未运行模型。",
            user_issue_summary=request.user_text or "OpenClaw Feishu 通道检查。",
            sku_match="通道检查不执行 SKU 检索。",
            suggested_reply="OpenClaw Feishu 通道合同检查已收到消息和附件，可生成原话题回复 payload。",
            troubleshooting_steps=["确认 sidecar 能转发 batch payload", "确认附件 metadata 能进入 SupportAsset"],
            follow_up_questions=[],
            official_evidence="未查询到可信正式依据，不可编造。",
            history_reference="未查询到可信历史参考，不可编造。",
            data_sources_used=[],
            missing_data_sources=["正式知识库", "产品 MRD/手册"],
            recommended_action="human_review",
            mention_enabled=False,
            ticket_draft="通道检查不生成工单。",
        ),
        coverage=SimpleNamespace(recommended_action="human_review"),
    )


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
