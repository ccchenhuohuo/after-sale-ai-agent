import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_runtime.channels.openclaw_feishu.adapter import (
    build_support_case_request_from_openclaw,
    build_support_case_request_from_openclaw_batch,
)
from agent_runtime.channels.openclaw_feishu.assets import support_asset_from_openclaw_resource
from agent_runtime.channels.openclaw_feishu.responder import build_openclaw_thread_reply, readable_plain_text
import agent_runtime.channels.openclaw_feishu.webhook as openclaw_webhook
from agent_runtime.copilot.answer_contract import ContractIssue, FEISHU_VISIBLE_REPLY_FALLBACK, SupportAnswer
from agent_runtime.settings import Settings


def test_openclaw_text_message_converts_to_support_case_request():
    request = build_support_case_request_from_openclaw(
        {
            "chatId": "oc_chat",
            "chatType": "group",
            "messageId": "om_msg",
            "threadId": "omt_thread",
            "senderId": "ou_sender",
            "content": "客户反馈 L023 不亮",
            "contentType": "text",
            "resources": [],
        }
    )

    assert request.source == "feishu"
    assert request.channel == "openclaw_feishu"
    assert request.source_platform == "feishu"
    assert request.user_text == "客户反馈 L023 不亮"
    assert request.chat_id == "oc_chat"
    assert request.thread_id == "omt_thread"
    assert request.message_id == "om_msg"
    assert request.sender_id == "ou_sender"
    assert request.assets == []
    assert request.metadata["channel"] == "openclaw_feishu"


def test_openclaw_official_message_context_converts_to_support_case_request():
    request = build_support_case_request_from_openclaw(
        {
            "chatId": "oc_chat",
            "messageId": "om_msg",
            "senderId": "ou_sender",
            "senderName": "客服",
            "chatType": "group",
            "content": "客户反馈屏幕黑屏",
            "contentType": "text",
            "resources": [
                {
                    "type": "image",
                    "fileKey": "img_key",
                    "fileName": "screen.jpg",
                }
            ],
            "rawMessage": {
                "message_id": "om_raw",
                "chat_id": "oc_raw",
                "thread_id": "omt_thread",
                "root_id": "om_root",
                "parent_id": "om_parent",
                "chat_type": "group",
                "message_type": "image",
            },
            "rawSender": {"sender_id": {"open_id": "ou_raw_sender"}},
        }
    )

    assert request.chat_id == "oc_chat"
    assert request.thread_id == "omt_thread"
    assert request.message_id == "om_msg"
    assert request.sender_id == "ou_sender"
    assert request.user_text == "客户反馈屏幕黑屏"
    assert request.metadata["chat_type"] == "group"
    assert request.metadata["content_type"] == "text"
    assert request.metadata["root_id"] == "om_root"
    assert request.metadata["parent_id"] == "om_parent"
    assert request.assets[0].asset_id == "om_msg:image:img_key"
    assert request.assets[0].filename == "screen.jpg"


def test_openclaw_raw_message_context_fallbacks_are_supported():
    request = build_support_case_request_from_openclaw(
        {
            "content": "客户反馈无法充电",
            "rawMessage": {
                "message_id": "om_raw",
                "chat_id": "oc_raw",
                "thread_id": "omt_raw_thread",
                "chat_type": "group",
                "message_type": "text",
            },
            "rawSender": {"sender_id": {"open_id": "ou_raw_sender"}},
        }
    )

    assert request.chat_id == "oc_raw"
    assert request.thread_id == "omt_raw_thread"
    assert request.message_id == "om_raw"
    assert request.sender_id == "ou_raw_sender"
    assert request.session_id == "openclaw-feishu:oc_raw:thread:omt_raw_thread"
    assert request.metadata["chat_type"] == "group"
    assert request.metadata["content_type"] == "text"


def test_openclaw_media_resources_convert_to_support_assets():
    message = {
        "chatId": "oc_chat",
        "messageId": "om_msg",
        "threadId": "omt_thread",
        "content": "看下附件",
        "resources": [
            {
                "type": "image",
                "imageKey": "img_key",
                "fileName": "chat_screenshot.png",
                "mimeType": "image/png",
                "localPath": "/tmp/openclaw/chat_screenshot.png",
                "description": "聊天截图",
            },
            {
                "type": "video",
                "fileKey": "video_key",
                "fileName": "fault.mp4",
                "mimeType": "video/mp4",
            },
            {
                "type": "file",
                "fileKey": "file_key",
                "fileName": "invoice.pdf",
                "mimeType": "application/pdf",
            },
        ],
    }

    request = build_support_case_request_from_openclaw(message)

    assert [asset.media_type for asset in request.assets] == ["image", "video", "file"]
    assert request.assets[0].file_key == "img_key"
    assert request.assets[0].local_path == "/tmp/openclaw/chat_screenshot.png"
    assert request.assets[1].file_key == "video_key"
    assert request.assets[2].filename == "invoice.pdf"


def test_openclaw_inbound_envelope_media_payload_converts_to_support_assets():
    request = build_support_case_request_from_openclaw(
        {
            "BodyForAgent": "客户补充了损坏图片和视频",
            "To": "oc_chat",
            "CurrentMessageId": "om_env",
            "MessageThreadId": "omt_env_thread",
            "SenderId": "ou_sender",
            "MediaPaths": ["/tmp/openclaw/damage.jpg", "/tmp/openclaw/fault.mp4"],
            "MediaTypes": ["image/jpeg", "video/mp4"],
            "MediaUrls": ["/tmp/openclaw/damage.jpg", "/tmp/openclaw/fault.mp4"],
        }
    )

    assert request.chat_id == "oc_chat"
    assert request.thread_id == "omt_env_thread"
    assert request.message_id == "om_env"
    assert request.sender_id == "ou_sender"
    assert request.user_text == "客户补充了损坏图片和视频"
    assert [asset.media_type for asset in request.assets] == ["image", "video"]
    assert request.assets[0].local_path == "/tmp/openclaw/damage.jpg"
    assert request.assets[1].local_path == "/tmp/openclaw/fault.mp4"


def test_openclaw_route_target_envelope_is_normalized():
    request = build_support_case_request_from_openclaw(
        {
            "BodyForAgent": "客户继续追问处理进度",
            "To": "chat:oc_chat#__feishu_reply_to=om_parent&__feishu_thread_id=omt_thread",
            "CurrentMessageId": "om_current",
            "SenderId": "ou_sender",
        }
    )

    assert request.chat_id == "oc_chat"
    assert request.thread_id == "omt_thread"
    assert request.message_id == "om_current"
    assert request.session_id == "openclaw-feishu:oc_chat:thread:omt_thread"


def test_openclaw_download_failure_asset_does_not_block_request_construction():
    asset = support_asset_from_openclaw_resource(
        {
            "type": "image",
            "imageKey": "img_key",
            "fileName": "damage.jpg",
            "status": "error",
            "downloadError": "HTTP 504",
        },
        message_id="om_msg",
    )

    assert asset.asset_id == "om_msg:image:img_key"
    assert asset.media_type == "image"
    assert asset.metadata["download_status"] == "error"
    assert asset.metadata["download_error"] == "HTTP 504"


def test_openclaw_burst_batch_merges_text_and_assets_into_one_case():
    request = build_support_case_request_from_openclaw_batch(
        [
            {
                "chatId": "oc_chat",
                "messageId": "om_text",
                "threadId": "omt_thread",
                "senderId": "ou_sender",
                "content": "客户说产品断裂",
                "resources": [],
            },
            {
                "chatId": "oc_chat",
                "messageId": "om_img",
                "threadId": "omt_thread",
                "senderId": "ou_sender",
                "content": "",
                "resources": [{"type": "image", "imageKey": "img_damage", "fileName": "damage.jpg"}],
            },
        ],
        batch_id="batch_1",
    )

    assert request.user_text == "客户说产品断裂"
    assert request.thread_id == "omt_thread"
    assert request.message_id == "om_img"
    assert request.channel == "openclaw_feishu"
    assert request.source_platform == "feishu"
    assert len(request.assets) == 1
    assert request.assets[0].asset_id == "om_img:image:img_damage"
    assert request.metadata["batch_size"] == 2


def test_openclaw_thread_reply_payload_uses_thread_only_and_plain_text_fallback():
    result = SimpleNamespace(
        blocked=False,
        answer=SupportAnswer(
            issue_type="unknown",
            run_mode="Agent SDK",
            confidence="低",
            confidence_reason="未查询到可信正式依据。",
            user_issue_summary="客户反馈设备异常。",
            sku_match="未在 SKU 目录中命中；需要补充订单 SKU、包装 SKU、产品铭牌或图片。",
            suggested_reply="建议先安抚客户，并说明需要补充信息后再确认处理方式。",
            troubleshooting_steps=["确认型号", "收集截图"],
            follow_up_questions=["请补充 SKU"],
            official_evidence="未查询到可信正式依据，不可编造。",
            history_reference="未查询到可信历史参考，不可编造。",
            ticket_draft="不建议生成工单，并说明原因。",
        ),
        contract_issues=[],
        request=SimpleNamespace(
            chat_id="oc_chat",
            thread_id="omt_thread",
            message_id="om_msg",
            request_id="case_1",
        ),
        coverage=SimpleNamespace(recommended_action="human_review"),
    )

    reply = build_openclaw_thread_reply(result)

    assert reply["channel"] == "feishu"
    assert reply["mode"] == "thread_reply"
    assert reply["replyInThread"] is True
    assert reply["replyToMessageId"] == "om_msg"
    assert reply["preferredFormat"] == "post"
    assert "##" not in reply["fallbackText"]
    assert "|" not in reply["fallbackText"]
    assert "客服可以先这样回应客户" in reply["fallbackText"]
    assert reply["metadata"]["blocked"] is False


def test_openclaw_thread_reply_uses_fallback_when_runtime_contract_blocks():
    result = SimpleNamespace(
        blocked=True,
        answer=SupportAnswer(
            issue_type="unknown",
            run_mode="Agent SDK",
            confidence="低",
            confidence_reason="未查询到可信正式依据。",
            user_issue_summary="客户反馈设备异常。",
            sku_match="未在 SKU 目录中命中；需要补充订单 SKU、包装 SKU、产品铭牌或图片。",
            suggested_reply="建议先收集信息并人工确认。",
            troubleshooting_steps=["确认型号"],
            follow_up_questions=["请补充 SKU"],
            official_evidence="未查询到可信正式依据，不可编造。",
            history_reference="未查询到可信历史参考，不可编造。",
            ticket_draft="不建议生成工单，并说明原因。",
        ),
        contract_issues=[ContractIssue("missing_field", "缺少字段")],
        request=SimpleNamespace(
            chat_id="oc_chat",
            thread_id="omt_thread",
            message_id="om_msg",
            request_id="case_1",
        ),
        coverage=SimpleNamespace(recommended_action="human_review"),
    )

    reply = build_openclaw_thread_reply(result)

    assert reply["text"] == FEISHU_VISIBLE_REPLY_FALLBACK
    assert reply["metadata"]["blocked"] is True
    assert reply["metadata"]["issueCodes"] == ["missing_field"]


def test_readable_plain_text_flattens_markdown_fallback():
    text = readable_plain_text("### 标题\n```txt\ncode\n```\n| A | B |\n|---|---|\n| 一 | 二 |")

    assert "###" not in text
    assert "```" not in text
    assert "|" not in text
    assert "一 / 二" in text


def test_openclaw_webhook_secret_is_checked_before_runtime_configuration(monkeypatch):
    configured = False

    def fake_configure(settings):
        nonlocal configured
        configured = True
        return settings

    monkeypatch.setattr(
        openclaw_webhook,
        "get_settings",
        lambda: Settings(openclaw_feishu_bridge_secret="secret"),
    )
    monkeypatch.setattr(openclaw_webhook, "configure_agents_runtime", fake_configure)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            openclaw_webhook.openclaw_feishu_support_case(
                {"messageId": "om_msg", "content": "客户反馈 L023 不亮"},
                authorization="Bearer wrong",
            )
        )

    assert exc_info.value.status_code == 403
    assert configured is False


def test_openclaw_health_reports_channel_without_runtime_configuration(monkeypatch):
    configured = False

    def fake_configure(settings):
        nonlocal configured
        configured = True
        return settings

    monkeypatch.setattr(
        openclaw_webhook,
        "get_settings",
        lambda: Settings(openclaw_feishu_bridge_secret="secret"),
    )
    monkeypatch.setattr(openclaw_webhook, "configure_agents_runtime", fake_configure)

    health = asyncio.run(openclaw_webhook.openclaw_feishu_health())

    assert health == {
        "ok": True,
        "channel": "openclaw_feishu",
        "runtime": "support_copilot",
        "requiresSecret": True,
    }
    assert configured is False


def test_openclaw_health_reports_open_message_endpoint_when_secret_unset(monkeypatch):
    monkeypatch.setattr(openclaw_webhook, "get_settings", lambda: Settings())

    health = asyncio.run(openclaw_webhook.openclaw_feishu_health())

    assert health == {
        "ok": True,
        "channel": "openclaw_feishu",
        "runtime": "support_copilot",
        "requiresSecret": False,
    }


def test_openclaw_webhook_returns_thread_reply_payload(monkeypatch):
    captured = {}

    async def fake_run_support_case_request(request, settings, **kwargs):
        captured["request"] = request
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            blocked=False,
            answer=_support_answer(),
            contract_issues=[],
            request=request,
            coverage=SimpleNamespace(recommended_action="answer"),
        )

    monkeypatch.setattr(openclaw_webhook, "get_settings", lambda: Settings(llm_api_key="test-key"))
    monkeypatch.setattr(openclaw_webhook, "configure_agents_runtime", lambda settings: settings)
    monkeypatch.setattr(openclaw_webhook, "build_support_runtime_session", lambda settings, session_id: "session")
    monkeypatch.setattr(openclaw_webhook, "run_support_case_request", fake_run_support_case_request)

    reply = asyncio.run(
        openclaw_webhook.openclaw_feishu_support_case(
            {
                "message": {
                    "chatId": "oc_chat",
                    "messageId": "om_msg",
                    "threadId": "omt_thread",
                    "senderId": "ou_sender",
                    "content": "客户反馈 L023 不亮",
                    "resources": [{"type": "image", "imageKey": "img_key"}],
                }
            }
        )
    )

    assert captured["request"].channel == "openclaw_feishu"
    assert captured["request"].source_platform == "feishu"
    assert captured["request"].assets[0].file_key == "img_key"
    assert captured["kwargs"]["entrypoint"] == "openclaw_feishu"
    assert captured["kwargs"]["session"] == "session"
    assert reply["mode"] == "thread_reply"
    assert reply["replyInThread"] is True
    assert reply["replyToMessageId"] == "om_msg"


def test_openclaw_webhook_contract_only_smoke_does_not_configure_runtime(monkeypatch):
    configured = False

    def fake_configure(settings):
        nonlocal configured
        configured = True
        raise AssertionError("contractOnly smoke must not configure the LLM runtime")

    monkeypatch.setattr(openclaw_webhook, "get_settings", lambda: Settings())
    monkeypatch.setattr(openclaw_webhook, "configure_agents_runtime", fake_configure)

    reply = asyncio.run(
        openclaw_webhook.openclaw_feishu_support_case(
            {
                "contractOnly": True,
                "batchId": "batch_1",
                "messages": [
                    {
                        "chatId": "oc_chat",
                        "messageId": "om_text",
                        "threadId": "omt_thread",
                        "senderId": "ou_sender",
                        "content": "客户反馈 L023 不亮，补了一张图片。",
                        "contentType": "text",
                    },
                    {
                        "chatId": "oc_chat",
                        "messageId": "om_image",
                        "threadId": "omt_thread",
                        "senderId": "ou_sender",
                        "contentType": "image",
                        "resources": [{"type": "image", "imageKey": "img_key"}],
                    },
                ],
            }
        )
    )

    assert configured is False
    assert reply["mode"] == "thread_reply"
    assert reply["replyInThread"] is True
    assert reply["chatId"] == "oc_chat"
    assert reply["threadId"] == "omt_thread"
    assert reply["replyToMessageId"] == "om_image"
    assert reply["metadata"]["recommendedAction"] == "human_review"
    assert reply["metadata"]["blocked"] is False


def test_openclaw_webhook_rejects_empty_batch():
    with pytest.raises(HTTPException) as exc_info:
        openclaw_webhook._request_from_payload({"messages": []})

    assert exc_info.value.status_code == 400


def test_copilot_runtime_has_no_feishu_or_openclaw_channel_imports():
    runtime_path = Path(__file__).resolve().parents[1] / "src" / "agent_runtime" / "copilot" / "runtime.py"
    tree = ast.parse(runtime_path.read_text(encoding="utf-8"))
    imported_modules = []
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)
            imported_names.extend(alias.name for alias in node.names)

    forbidden = (
        "agent_runtime.feishu",
        "agent_runtime.channels",
        "openclaw",
        "lark_oapi",
    )
    assert not any(module.startswith(forbidden) for module in imported_modules)
    assert "render_feishu_reply" not in imported_names
    assert "validate_feishu_visible_reply" not in imported_names


def _support_answer() -> SupportAnswer:
    return SupportAnswer(
        issue_type="unknown",
        run_mode="Agent SDK",
        confidence="低",
        confidence_reason="未查询到可信正式依据。",
        user_issue_summary="客户反馈设备异常。",
        sku_match="未在 SKU 目录中命中；需要补充订单 SKU、包装 SKU、产品铭牌或图片。",
        suggested_reply="建议先安抚客户，并说明需要补充信息后再确认处理方式。",
        troubleshooting_steps=["确认型号"],
        follow_up_questions=["请补充 SKU"],
        official_evidence="未查询到可信正式依据，不可编造。",
        history_reference="未查询到可信历史参考，不可编造。",
        ticket_draft="不建议生成工单，并说明原因。",
    )
