import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
import httpx
from fastapi import HTTPException

import agent_runtime.copilot.runtime as support_runtime
import agent_runtime.feishu.assets as feishu_assets
import agent_runtime.feishu.bridge as bridge
from agent_runtime.copilot.answer_contract import FEISHU_VISIBLE_REPLY_FALLBACK, SupportAnswer
from agent_runtime.copilot.case_context import SupportAsset, SupportCaseRequest
from agent_runtime.feishu.admission import BotIdentity, should_accept
from agent_runtime.feishu.adapter import build_support_case_request_from_event
from agent_runtime.feishu.assets import download_feishu_assets_for_request
from agent_runtime.feishu.bridge import (
    FeishuMessageEvent,
    build_feishu_user_input,
    clear_runtime_state_for_tests,
    effective_thread_id,
    event_from_payload,
    queue_key_for_event,
    session_id_for_event,
    should_handle_event,
)
from agent_runtime.feishu.event_sources import payload_from_lark_oapi_event
from agent_runtime.feishu.responder import FeishuSdkResponder, ReplyResult, truncate_for_feishu
from agent_runtime.feishu.runtime_store import RuntimeStore
from agent_runtime.feishu.webhook import _challenge_response, _verify_token
from agent_runtime.settings import Settings


def settings_for_tmp(tmp_path, **overrides):
    values = {
        "feishu_support_group_chat_id": "oc_target",
        "feishu_bot_mention_name": "飞书 CLI",
        "feishu_runtime_db_path": str(tmp_path / "runtime.sqlite3"),
        "support_agent_session_db_path": str(tmp_path / "agent_sessions.sqlite3"),
    }
    values.update(overrides)
    return Settings(**values)


class FakeStreamResponse:
    def __init__(self, status_code=200, *, chunks=(), headers=None):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", "https://open.feishu.cn"),
                response=httpx.Response(self.status_code),
            )

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


def test_challenge_response_supports_v2_url_verification():
    payload = {
        "schema": "2.0",
        "header": {"event_type": "url_verification"},
        "challenge": "abc123",
    }

    assert _challenge_response(payload) == {"challenge": "abc123"}


def test_event_from_v2_payload_extracts_message_content_and_mentions():
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "evt_1",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_sender"}},
            "message": {
                "chat_id": "oc_chat",
                "chat_type": "group",
                "message_id": "om_msg",
                "message_type": "text",
                "thread_id": "omt_thread",
                "root_id": "om_root",
                "parent_id": "om_parent",
                "content": '{"text":"@_user_1 AI分析：S043 不亮"}',
                "mentions": [{"name": "售后机器人"}],
            },
        },
    }

    event = event_from_payload(payload)

    assert event == FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_chat",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@_user_1 AI分析：S043 不亮",
        mention_names=("售后机器人",),
        thread_id="omt_thread",
        root_id="om_root",
        parent_id="om_parent",
    )


def test_lark_oapi_event_object_payload_normalizes_like_v2():
    class FakeLarkOapiEvent:
        def to_dict(self):
            return {
                "schema": "2.0",
                "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_sender"}, "sender_type": "user"},
                    "message": {
                        "chat_id": "oc_chat",
                        "chat_type": "group",
                        "message_id": "om_msg",
                        "message_type": "text",
                        "thread_id": "omt_thread",
                        "content": '{"text":"@_user_1 L023 不亮"}',
                        "mentions": [{"id": "ou_bot", "name": "飞书 CLI"}],
                    },
                },
            }

    payload = payload_from_lark_oapi_event(FakeLarkOapiEvent())
    event = event_from_payload(payload)

    assert event is not None
    assert event.message_id == "om_msg"
    assert event.thread_id == "omt_thread"
    assert event.mention_ids == ("ou_bot",)
    assert event.mention_names == ("飞书 CLI",)


def test_event_from_payload_extracts_dict_mention_open_id():
    payload = {
        "chat_id": "oc_chat",
        "content": "@飞书 CLI L023 不亮",
        "mentions": [{"id": {"open_id": "ou_bot", "union_id": "on_bot"}, "name": "飞书 CLI"}],
        "message_id": "om_msg_3",
        "msg_type": "text",
        "sender": {"id": "ou_sender", "sender_type": "user"},
    }

    event = event_from_payload(payload)

    assert event is not None
    assert event.mention_ids == ("ou_bot",)


def test_lark_oapi_nested_object_payload_normalizes_like_v2():
    class Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    payload = payload_from_lark_oapi_event(
        Obj(
            schema="2.0",
            header=Obj(event_id="evt_1", event_type="im.message.receive_v1"),
            event=Obj(
                sender=Obj(sender_id=Obj(open_id="ou_sender"), sender_type="user"),
                message=Obj(
                    chat_id="oc_chat",
                    chat_type="group",
                    message_id="om_msg",
                    message_type="text",
                    thread_id="omt_thread",
                    content='{"text":"@_user_1 L023 不亮"}',
                    mentions=[Obj(id="cli_bot", name="飞书 CLI")],
                ),
            ),
        )
    )
    event = event_from_payload(payload)

    assert event is not None
    assert event.chat_id == "oc_chat"
    assert event.message_id == "om_msg"
    assert event.thread_id == "omt_thread"
    assert event.sender_id == "ou_sender"
    assert event.mention_ids == ("cli_bot",)
    assert event.mention_names == ("飞书 CLI",)


def test_feishu_adapter_converts_image_message_to_support_asset(tmp_path):
    settings = settings_for_tmp(tmp_path)
    event = FeishuMessageEvent(
        event_id="evt_img",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_img",
        message_type="image",
        sender_id="ou_sender",
        content="@飞书 CLI 看下这个截图",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
        raw_content='{"image_key":"img_v3_abc","file_name":"chat_screenshot.png"}',
    )

    request = build_support_case_request_from_event(event, settings)

    assert request.user_text == "看下这个截图"
    assert request.channel == "legacy_feishu"
    assert request.source_platform == "feishu"
    assert len(request.assets) == 1
    assert request.assets[0].media_type == "image"
    assert request.assets[0].file_key == "img_v3_abc"
    assert request.assets[0].message_id == "om_img"


def test_feishu_adapter_converts_video_message_to_support_asset(tmp_path):
    settings = settings_for_tmp(tmp_path)
    event = FeishuMessageEvent(
        event_id="evt_video",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_video",
        message_type="video",
        sender_id="ou_sender",
        content="@飞书 CLI 故障视频",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
        raw_content='{"file_key":"video_v3_abc","file_name":"fault_video.mp4"}',
    )

    request = build_support_case_request_from_event(event, settings)

    assert request.user_text == "故障视频"
    assert request.channel == "legacy_feishu"
    assert request.source_platform == "feishu"
    assert len(request.assets) == 1
    assert request.assets[0].media_type == "video"
    assert request.assets[0].file_key == "video_v3_abc"


def test_feishu_asset_downloader_sets_local_path(monkeypatch, tmp_path):
    captured = {}

    async def fake_token(settings):
        return "tenant-token"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, *, params=None, headers=None):
            captured["method"] = method
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return FakeStreamResponse(200, chunks=[b"fake-png"], headers={"content-type": "image/png"})

    monkeypatch.setattr(feishu_assets, "get_tenant_access_token", fake_token)
    monkeypatch.setattr(feishu_assets.httpx, "AsyncClient", FakeClient)
    settings = settings_for_tmp(tmp_path, feishu_asset_cache_dir=str(tmp_path / "assets"))
    request = SupportCaseRequest(
        request_id="case_1",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="asset_1",
                media_type="image",
                file_key="img_v3_abc",
                message_id="om_img",
            )
        ],
    )

    enriched = asyncio.run(download_feishu_assets_for_request(request, settings))

    asset = enriched.assets[0]
    assert asset.local_path.endswith("img_v3_abc.png")
    assert (tmp_path / "assets" / "om_img" / "img_v3_abc.png").read_bytes() == b"fake-png"
    assert asset.mime_type == "image/png"
    assert asset.metadata["download_status"] == "ok"
    assert captured["method"] == "GET"
    assert captured["params"] == {"type": "image"}
    assert captured["headers"] == {"Authorization": "Bearer tenant-token"}


def test_feishu_asset_downloader_failure_stays_non_blocking(monkeypatch, tmp_path):
    async def fake_token(settings):
        return "tenant-token"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, *, params=None, headers=None):
            return FakeStreamResponse(
                200,
                chunks=[b'{"code": 234002, "msg": "no permission"}'],
                headers={"content-type": "application/json"},
            )

    monkeypatch.setattr(feishu_assets, "get_tenant_access_token", fake_token)
    monkeypatch.setattr(feishu_assets.httpx, "AsyncClient", FakeClient)
    settings = settings_for_tmp(tmp_path, feishu_asset_cache_dir=str(tmp_path / "assets"))
    request = SupportCaseRequest(
        request_id="case_1",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="asset_1",
                media_type="image",
                file_key="img_v3_abc",
                message_id="om_img",
            )
        ],
    )

    enriched = asyncio.run(download_feishu_assets_for_request(request, settings))

    asset = enriched.assets[0]
    assert asset.local_path == ""
    assert asset.metadata["download_status"] == "error"
    assert "234002" in asset.metadata["download_error"]


def test_feishu_asset_downloader_stops_stream_when_resource_is_too_large(monkeypatch, tmp_path):
    async def fake_token(settings):
        return "tenant-token"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, *, params=None, headers=None):
            return FakeStreamResponse(
                200,
                chunks=[b"12345", b"67890"],
                headers={"content-type": "image/png"},
            )

    monkeypatch.setattr(feishu_assets, "get_tenant_access_token", fake_token)
    monkeypatch.setattr(feishu_assets.httpx, "AsyncClient", FakeClient)
    settings = settings_for_tmp(
        tmp_path,
        feishu_asset_cache_dir=str(tmp_path / "assets"),
        feishu_asset_download_max_bytes=5,
    )
    request = SupportCaseRequest(
        request_id="case_1",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="asset_1",
                media_type="image",
                file_key="img_v3_abc",
                message_id="om_img",
            )
        ],
    )

    enriched = asyncio.run(download_feishu_assets_for_request(request, settings))

    asset = enriched.assets[0]
    assert asset.local_path == ""
    assert asset.metadata["download_status"] == "error"
    assert "FEISHU_ASSET_DOWNLOAD_MAX_BYTES" in asset.metadata["download_error"]
    assert not (tmp_path / "assets" / "om_img" / "img_v3_abc.png").exists()


def test_feishu_agent_downloads_assets_before_core_runtime(monkeypatch, tmp_path):
    settings = settings_for_tmp(tmp_path, llm_api_key="test-key")
    captured = {}

    async def fake_download(request, settings):
        captured["download_seen_assets"] = [asset.file_key for asset in request.assets]
        return request.model_copy(
            update={
                "assets": [
                    request.assets[0].model_copy(
                        update={
                            "local_path": str(tmp_path / "downloaded.png"),
                            "metadata": {**request.assets[0].metadata, "download_status": "ok"},
                        }
                    )
                ]
            }
        )

    async def fake_runtime(request, settings, **kwargs):
        captured["runtime_asset_local_path"] = request.assets[0].local_path
        captured["runtime_download_status"] = request.assets[0].metadata["download_status"]
        return SimpleNamespace(
            contract_issues=[],
            answer=SupportAnswer(
                issue_type="unknown",
                run_mode="Agent SDK",
                confidence="低",
                confidence_reason="未查询到可信正式依据。",
                user_issue_summary="客户发送图片，需要补充文字说明。",
                sku_match="未在 SKU 目录中命中；需要补充订单 SKU、包装 SKU、产品铭牌或图片。",
                suggested_reply="已收到图片，建议先补充产品型号、故障现象和订单信息，方便进一步确认。",
                troubleshooting_steps=["补充产品型号", "说明故障现象"],
                follow_up_questions=["请补充 SKU 或订单号"],
                official_evidence="未查询到可信正式依据，不可编造。",
                history_reference="未查询到可信历史参考，不可编造。",
                ticket_draft="不建议生成工单，并说明原因。",
            ),
        )

    monkeypatch.setattr(bridge, "configure_agents_runtime", lambda settings: settings)
    monkeypatch.setattr(bridge, "download_feishu_assets_for_request", fake_download)
    monkeypatch.setattr(bridge, "run_support_case_request", fake_runtime)
    event = FeishuMessageEvent(
        event_id="evt_img",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_img",
        message_type="image",
        sender_id="ou_sender",
        content="@飞书 CLI 看下这个截图",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
        raw_content='{"image_key":"img_v3_abc","file_name":"chat_screenshot.png"}',
    )

    reply = asyncio.run(bridge.run_support_agent_for_event(event, settings))

    assert captured == {
        "download_seen_assets": ["img_v3_abc"],
        "runtime_asset_local_path": str(tmp_path / "downloaded.png"),
        "runtime_download_status": "ok",
    }
    assert "客服可以先这样回应客户" in reply


def test_should_handle_event_requires_target_group_and_trigger():
    settings = Settings(
        feishu_support_group_chat_id="oc_target",
        feishu_bot_mention_name="售后机器人",
    )
    mentioned = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@_user_1 S043 不亮",
        mention_names=("售后机器人",),
    )
    other_group = FeishuMessageEvent(
        **{**mentioned.__dict__, "chat_id": "oc_other"}
    )
    no_trigger = FeishuMessageEvent(
        **{**mentioned.__dict__, "mention_names": (), "content": "普通消息"}
    )
    mentioned_someone_else = FeishuMessageEvent(
        **{**mentioned.__dict__, "mention_names": ("张三",), "content": "@_user_2 普通消息"}
    )

    assert should_handle_event(mentioned, settings)
    assert not should_handle_event(other_group, settings)
    assert not should_handle_event(no_trigger, settings)
    assert not should_handle_event(mentioned_someone_else, settings)


def test_build_feishu_user_input_strips_mention_before_trigger(tmp_path):
    settings = settings_for_tmp(tmp_path, feishu_bot_mention_name="飞书 CLI")
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI AI分析：L023 收到后不亮",
        mention_names=("飞书 CLI",),
    )
    placeholder_event = FeishuMessageEvent(
        **{
            **event.__dict__,
            "message_id": "om_msg_2",
            "content": "@_user_1 AI分析：T081 一推就掉",
        }
    )

    assert build_feishu_user_input(event, settings) == "L023 收到后不亮"
    assert build_feishu_user_input(placeholder_event, settings) == "T081 一推就掉"


def test_should_handle_event_allows_prefix_trigger():
    settings = Settings(feishu_support_group_chat_id="oc_target")
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="AI分析：S043 不亮",
    )

    assert should_handle_event(event, settings)


def test_should_handle_event_accepts_media_only_when_enabled_for_target_group():
    event = FeishuMessageEvent(
        event_id="evt_image",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_image",
        message_type="image",
        sender_id="ou_sender",
        content="",
        raw_content='{"image_key":"img_v3_abc"}',
    )

    disabled = Settings(
        feishu_support_group_chat_id="oc_target",
        feishu_media_auto_accept_enabled=False,
    )
    enabled = Settings(
        feishu_support_group_chat_id="oc_target",
        feishu_media_auto_accept_enabled=True,
    )
    enabled_without_whitelist = Settings(feishu_media_auto_accept_enabled=True)
    enabled_other_group = Settings(
        feishu_support_group_chat_id="oc_other",
        feishu_media_auto_accept_enabled=True,
    )

    assert not should_handle_event(event, disabled)
    assert should_handle_event(event, enabled)
    assert not should_handle_event(event, enabled_without_whitelist)
    assert not should_handle_event(event, enabled_other_group)


def test_should_handle_event_allows_comma_separated_target_groups():
    settings = Settings(
        feishu_support_group_chat_id="oc_other, oc_target",
        feishu_bot_mention_name="售后机器人",
    )
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@_user_1 S043 不亮",
        mention_names=("售后机器人",),
    )

    assert should_handle_event(event, settings)


def test_should_handle_event_ignores_app_sender():
    settings = Settings(
        feishu_support_group_chat_id="oc_target",
        feishu_bot_mention_name="售后机器人",
    )
    event = FeishuMessageEvent(
        event_id="",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_bot_reply",
        message_type="text",
        sender_id="cli_app",
        content="@售后机器人 S043 不亮",
        sender_type="app",
        mention_names=("售后机器人",),
    )

    assert not should_handle_event(event, settings)


def test_admission_prefers_bot_open_id_over_name(tmp_path):
    settings = settings_for_tmp(
        tmp_path,
        feishu_bot_mention_name="错误名称",
        feishu_bot_open_id="ou_bot",
    )
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        mention_ids=("ou_bot",),
    )

    assert should_accept(event, settings).accepted


def test_admission_accepts_dict_mention_id_with_bot_open_id(tmp_path):
    settings = settings_for_tmp(
        tmp_path,
        feishu_bot_mention_name="错误名称",
        feishu_bot_open_id="ou_bot",
    )
    payload = {
        "chat_id": "oc_target",
        "content": "@飞书 CLI L023 不亮",
        "mentions": [{"id": {"open_id": "ou_bot"}, "name": "飞书 CLI"}],
        "message_id": "om_msg_3",
        "msg_type": "text",
        "sender": {"id": "ou_sender", "sender_type": "user"},
    }
    event = event_from_payload(payload)

    assert event is not None
    assert should_accept(event, settings).accepted


def test_webhook_verification_token_requires_present_match():
    settings = Settings(feishu_verification_token="expected")

    _verify_token({"token": "expected"}, settings)
    _verify_token({"header": {"token": "expected"}}, settings)
    with pytest.raises(HTTPException) as missing:
        _verify_token({}, settings)
    with pytest.raises(HTTPException) as wrong:
        _verify_token({"token": "wrong"}, settings)
    assert missing.value.status_code == 403
    assert wrong.value.status_code == 403


def test_admission_falls_back_to_bot_name(tmp_path):
    settings = settings_for_tmp(tmp_path, feishu_bot_open_id="")
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
    )

    assert should_accept(event, settings).accepted


def test_admission_blocks_disallowed_user(tmp_path):
    settings = settings_for_tmp(tmp_path, feishu_allowed_user_open_ids="ou_allowed")
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_other",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
    )

    assert should_accept(event, settings).status == "ignored"


def test_admission_suppresses_bot_loop(tmp_path):
    settings = settings_for_tmp(tmp_path, feishu_bot_loop_max_turns=1)
    store = RuntimeStore(settings.feishu_runtime_db_path, settings.feishu_dedup_ttl_seconds, 5000)
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_bot_1",
        message_type="text",
        sender_id="ou_other_bot",
        sender_type="bot",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
    )
    next_event = FeishuMessageEvent(**{**event.__dict__, "event_id": "evt_2", "message_id": "om_bot_2"})

    assert should_accept(event, settings, runtime_store=store).status == "ignored_bot_sender"
    assert should_accept(next_event, settings, runtime_store=store).status == "suppressed_bot_loop"


def test_chat_messages_list_payload_normalizes_mentions_and_sender():
    payload = {
        "chat_id": "oc_chat",
        "content": "@飞书 CLI L023 不亮",
        "mentions": [{"id": "ou_bot", "key": "@_user_1", "name": "飞书 CLI"}],
        "message_id": "om_msg_3",
        "msg_type": "text",
        "sender": {
            "id": "ou_sender",
            "id_type": "open_id",
            "name": "陈煜",
            "sender_type": "user",
        },
    }

    event = event_from_payload(payload)

    assert event == FeishuMessageEvent(
        event_id="",
        chat_id="oc_chat",
        chat_type="",
        message_id="om_msg_3",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        sender_type="user",
        mention_names=("飞书 CLI",),
        mention_ids=("ou_bot",),
    )


def test_chat_messages_list_body_payload_normalizes_content(tmp_path):
    payload = {
        "chat_id": "oc_chat",
        "body": {"content": '{"text":"@_user_1 AI分析：L023 收到后不亮"}'},
        "mentions": [{"id": "ou_bot", "key": "@_user_1", "name": "飞书 CLI"}],
        "message_id": "om_msg_4",
        "msg_type": "text",
        "sender": {
            "id": "ou_sender",
            "id_type": "open_id",
            "name": "陈煜",
            "sender_type": "user",
        },
    }
    settings = settings_for_tmp(tmp_path, feishu_bot_mention_name="飞书 CLI")

    event = event_from_payload(payload)

    assert event is not None
    assert event.content == "@_user_1 AI分析：L023 收到后不亮"
    assert event.mention_ids == ("ou_bot",)
    assert build_feishu_user_input(event, settings) == "L023 收到后不亮"


class _FakeReplyBodyBuilder:
    def __init__(self):
        self.body = SimpleNamespace(content="", msg_type="", reply_in_thread=False, uuid="")

    def content(self, value):
        self.body.content = value
        return self

    def msg_type(self, value):
        self.body.msg_type = value
        return self

    def reply_in_thread(self, value):
        self.body.reply_in_thread = value
        return self

    def uuid(self, value):
        self.body.uuid = value
        return self

    def build(self):
        return self.body


class _FakeReplyBody:
    @staticmethod
    def builder():
        return _FakeReplyBodyBuilder()


class _FakeReplyRequestBuilder:
    def __init__(self):
        self.request = SimpleNamespace(message_id="", request_body=None)

    def message_id(self, value):
        self.request.message_id = value
        return self

    def request_body(self, value):
        self.request.request_body = value
        return self

    def build(self):
        return self.request


class _FakeReplyRequest:
    @staticmethod
    def builder():
        return _FakeReplyRequestBuilder()


class _FakeSdk:
    im = SimpleNamespace(
        v1=SimpleNamespace(
            ReplyMessageRequestBody=_FakeReplyBody,
            ReplyMessageRequest=_FakeReplyRequest,
        )
    )


class _FakeMessageApi:
    def __init__(self, response):
        self.response = response
        self.reply_request = None
        self.create_called = False

    async def areply(self, request):
        self.reply_request = request
        return self.response

    async def acreate(self, request):
        self.create_called = True
        return self.response


def _fake_client(response):
    message_api = _FakeMessageApi(response)
    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
    return client, message_api


def test_sdk_responder_replies_in_thread_with_uuid(tmp_path):
    response = SimpleNamespace(code=0, msg="ok", data=SimpleNamespace(message_id="om_reply"))
    client, message_api = _fake_client(response)
    settings = settings_for_tmp(tmp_path, feishu_reply_max_chars=200)
    responder = FeishuSdkResponder(settings, client=client, sdk=_FakeSdk)

    result = asyncio.run(responder.reply_in_thread("om_source", "收到", "idem-1"))

    assert result.reply_message_id == "om_reply"
    assert message_api.reply_request.message_id == "om_source"
    body = message_api.reply_request.request_body
    assert body.msg_type == "text"
    assert json.loads(body.content) == {"text": "收到"}
    assert body.reply_in_thread is True
    assert body.uuid == "idem-1"
    assert message_api.create_called is False


def test_sdk_responder_failure_raises_without_top_level_create(tmp_path):
    response = SimpleNamespace(code=999, msg="bad request", data=None)
    client, message_api = _fake_client(response)
    responder = FeishuSdkResponder(settings_for_tmp(tmp_path), client=client, sdk=_FakeSdk)

    try:
        asyncio.run(responder.reply_in_thread("om_source", "收到", "idem-1"))
    except RuntimeError as exc:
        assert "code=999" in str(exc)
    else:
        raise AssertionError("Expected SDK reply failure")

    assert message_api.reply_request.message_id == "om_source"
    assert message_api.create_called is False


def test_truncate_for_feishu_uses_plain_visible_truncation():
    answer = "\n".join(
        [
            "我先看了一下，客户反馈设备异常，目前材料还不够直接下结论。",
            "很长的正文" * 80,
        ]
    )

    truncated = truncate_for_feishu(answer, 120)

    assert len(truncated) <= 120
    assert "[安全边界摘要]" not in truncated
    assert "正式依据" not in truncated
    assert truncated.endswith("...[内容过长已截断]")


def test_effective_thread_id_prefers_thread_then_root_then_message():
    event = FeishuMessageEvent(
        event_id="evt",
        chat_id="oc_chat",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="hello",
        root_id="om_root",
    )

    assert effective_thread_id(event) == "om_root"
    assert session_id_for_event(event) == "feishu:oc_chat:thread:om_root"
    assert queue_key_for_event(event) == "oc_chat:om_root"
    assert session_id_for_event(FeishuMessageEvent(**{**event.__dict__, "thread_id": "omt_thread"})) == (
        "feishu:oc_chat:thread:omt_thread"
    )
    assert session_id_for_event(FeishuMessageEvent(**{**event.__dict__, "root_id": ""})) == (
        "feishu:oc_chat:thread:om_msg"
    )


def test_session_id_is_shared_within_thread_and_split_across_threads():
    base = {
        "event_id": "evt",
        "chat_id": "oc_chat",
        "chat_type": "group",
        "message_type": "text",
        "sender_id": "ou_sender",
        "content": "@飞书 CLI 问题",
        "mention_names": ("飞书 CLI",),
    }

    first = FeishuMessageEvent(**base, message_id="om_1", thread_id="omt_same")
    second = FeishuMessageEvent(**base, message_id="om_2", thread_id="omt_same")
    other = FeishuMessageEvent(**base, message_id="om_3", thread_id="omt_other")

    assert session_id_for_event(first) == session_id_for_event(second)
    assert session_id_for_event(first) != session_id_for_event(other)


def test_persistent_dedup_survives_runtime_cache_reset(monkeypatch, tmp_path):
    clear_runtime_state_for_tests()
    calls = []

    async def fake_agent(event, settings):
        calls.append(("agent", event.message_id))
        return "answer"

    async def fake_reply(message_id, text, settings=None):
        calls.append(("reply", message_id))

    monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", fake_reply)
    settings = settings_for_tmp(tmp_path)
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
    )

    assert asyncio.run(bridge.process_message_event(event, settings)) == "replied"
    clear_runtime_state_for_tests()
    assert asyncio.run(bridge.process_message_event(event, settings)) == "duplicate"
    assert calls == [("agent", "om_msg"), ("reply", "om_msg")]


def test_openapi_message_payload_duplicate_is_deduped(monkeypatch, tmp_path):
    clear_runtime_state_for_tests()
    calls = []

    async def fake_agent(event, settings):
        calls.append(("agent", event.message_id))
        return "answer"

    async def fake_reply(message_id, text, settings=None):
        calls.append(("reply", message_id))

    monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", fake_reply)
    settings = settings_for_tmp(tmp_path)
    payload = {
        "chat_id": "oc_target",
        "chat_type": "group",
        "message_id": "om_poll_msg",
        "msg_type": "text",
        "content": "@飞书 CLI L023 不亮",
        "mentions": [{"name": "飞书 CLI"}],
        "sender": {
            "id": "ou_sender",
            "sender_type": "user",
        },
        "root_id": "om_topic",
    }
    event = event_from_payload(payload)

    assert event is not None
    assert asyncio.run(bridge.process_message_event(event, settings)) == "replied"
    clear_runtime_state_for_tests()
    assert asyncio.run(bridge.process_message_event(event, settings)) == "duplicate"
    assert calls == [("agent", "om_poll_msg"), ("reply", "om_poll_msg")]


def test_expired_event_is_not_processed(monkeypatch, tmp_path):
    clear_runtime_state_for_tests()
    calls = []

    async def fake_agent(event, settings):
        calls.append(("agent", event.message_id))
        return "answer"

    async def fake_reply(message_id, text, settings=None):
        calls.append(("reply", message_id))

    monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", fake_reply)
    settings = settings_for_tmp(tmp_path, feishu_event_max_age_seconds=1)
    event = FeishuMessageEvent(
        event_id="evt_old",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_old",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        create_time="946684800000",
    )

    assert asyncio.run(bridge.process_message_event(event, settings)) == "expired"
    assert calls == []


def test_per_thread_queue_serializes_same_thread(monkeypatch, tmp_path):
    clear_runtime_state_for_tests()
    started = []

    async def fake_agent(event, settings):
        started.append(event.message_id)
        await asyncio.sleep(0.01)
        return "answer"

    async def fake_reply(message_id, text, settings=None):
        return None

    monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", fake_reply)
    settings = settings_for_tmp(tmp_path)

    def event(message_id):
        return FeishuMessageEvent(
            event_id=message_id,
            chat_id="oc_target",
            chat_type="group",
            message_id=message_id,
            message_type="text",
            sender_id="ou_sender",
            content="@飞书 CLI L023 不亮",
            mention_names=("飞书 CLI",),
            thread_id="omt_same",
        )

    async def runner():
        await asyncio.gather(
            bridge.process_message_event(event("om_1"), settings),
            bridge.process_message_event(event("om_2"), settings),
            bridge.process_message_event(event("om_3"), settings),
        )

    asyncio.run(runner())

    assert started == ["om_1", "om_2", "om_3"]


def test_different_threads_can_process_concurrently(monkeypatch, tmp_path):
    clear_runtime_state_for_tests()
    started = []

    async def runner():
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_agent(event, settings):
            started.append(event.message_id)
            if len(started) == 2:
                both_started.set()
            await release.wait()
            return "answer"

        async def fake_reply(message_id, text, settings=None):
            return None

        monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
        monkeypatch.setattr(bridge, "reply_in_thread", fake_reply)
        settings = settings_for_tmp(tmp_path)
        semaphore = asyncio.Semaphore(2)
        event_1 = FeishuMessageEvent(
            event_id="evt_1",
            chat_id="oc_target",
            chat_type="group",
            message_id="om_1",
            message_type="text",
            sender_id="ou_sender",
            content="@飞书 CLI L023 不亮",
            mention_names=("飞书 CLI",),
            thread_id="omt_1",
        )
        event_2 = FeishuMessageEvent(
            **{**event_1.__dict__, "event_id": "evt_2", "message_id": "om_2", "thread_id": "omt_2"}
        )
        task_1 = asyncio.create_task(bridge.process_message_event(event_1, settings, semaphore=semaphore))
        task_2 = asyncio.create_task(bridge.process_message_event(event_2, settings, semaphore=semaphore))
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        release.set()
        assert await asyncio.gather(task_1, task_2) == ["replied", "replied"]

    asyncio.run(runner())

    assert set(started) == {"om_1", "om_2"}


def test_thread_reply_failure_does_not_fallback_to_top_level(monkeypatch, tmp_path):
    clear_runtime_state_for_tests()
    calls = []

    async def fake_agent(event, settings):
        calls.append(("agent", event.message_id))
        return "answer"

    async def failing_reply(message_id, text, settings=None):
        calls.append(("reply", message_id))
        raise RuntimeError("reply target missing")

    monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", failing_reply)
    settings = settings_for_tmp(tmp_path)
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
    )

    assert asyncio.run(bridge.process_message_event(event, settings)) == "reply_failed"
    assert calls == [("agent", "om_msg"), ("reply", "om_msg")]
    with sqlite3.connect(settings.feishu_runtime_db_path) as connection:
        row = connection.execute(
            "SELECT status, error FROM reply_ledger WHERE source_message_id = ?",
            ("om_msg",),
        ).fetchone()
    assert row[0] == "reply_failed"
    assert "reply target missing" in row[1]


def test_reply_failed_event_can_retry(monkeypatch, tmp_path):
    clear_runtime_state_for_tests()
    calls = []

    async def fake_agent(event, settings):
        calls.append(("agent", event.message_id))
        return "answer"

    attempts = {"count": 0}

    async def flaky_reply(message_id, text, settings=None):
        calls.append(("reply", message_id))
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary reply failure")
        return ReplyResult(status="replied", reply_message_id="om_reply")

    monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", flaky_reply)
    settings = settings_for_tmp(tmp_path)
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
    )

    assert asyncio.run(bridge.process_message_event(event, settings)) == "reply_failed"
    assert asyncio.run(bridge.process_message_event(event, settings)) == "replied"
    assert calls == [
        ("agent", "om_msg"),
        ("reply", "om_msg"),
        ("agent", "om_msg"),
        ("reply", "om_msg"),
    ]


def test_feishu_agent_session_uses_persistent_db(monkeypatch, tmp_path):
    settings = settings_for_tmp(tmp_path, llm_api_key="test-key")
    observed_lengths = []

    monkeypatch.setattr(bridge, "configure_agents_runtime", lambda settings: settings)
    monkeypatch.setattr(support_runtime, "build_support_copilot", lambda model: object())

    async def fake_collect(raw_issue, settings):
        from agent_runtime.copilot.evidence import SupportEvidencePack

        return SupportEvidencePack(
            raw_issue_hash="hash",
            query_chars=len(raw_issue),
            issue_type="unknown",
            product_model="",
            sku=[],
            official=[],
            history=[],
            media=[],
        )

    async def fake_run(agent, input_text, *, context=None, session=None, run_config=None):
        observed_lengths.append(len(await session.get_items(limit=100)))
        await session.add_items([{"role": "user", "content": "session marker"}])
        return SimpleNamespace(
            final_output=SupportAnswer(
                issue_type="unknown",
                run_mode="Agent SDK",
                confidence="低",
                confidence_reason="未查询到可信正式依据。",
                user_issue_summary="客户反馈异常。",
                sku_match="未在 SKU 目录中命中；需要补充订单 SKU、包装 SKU、产品铭牌或图片。",
                suggested_reply="建议先收集信息并人工确认。",
                troubleshooting_steps=["确认型号"],
                follow_up_questions=["请补充 SKU"],
                official_evidence="未查询到可信正式依据，不可编造。",
                history_reference="未查询到可信历史参考，不可编造。",
                ticket_draft="不建议生成工单，并说明原因。",
            )
        )

    monkeypatch.setattr(support_runtime, "collect_support_evidence", fake_collect)
    monkeypatch.setattr(support_runtime.Runner, "run", fake_run)
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_1",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
    )
    next_event = FeishuMessageEvent(**{**event.__dict__, "event_id": "evt_2", "message_id": "om_2"})

    asyncio.run(bridge.run_support_agent_for_event(event, settings))
    asyncio.run(bridge.run_support_agent_for_event(next_event, settings))

    assert observed_lengths == [0, 1]
    assert (tmp_path / "agent_sessions.sqlite3").exists()


def test_feishu_agent_returns_visible_natural_reply(monkeypatch, tmp_path):
    settings = settings_for_tmp(tmp_path, llm_api_key="test-key")

    monkeypatch.setattr(bridge, "configure_agents_runtime", lambda settings: settings)
    monkeypatch.setattr(support_runtime, "build_support_copilot", lambda model: object())

    async def fake_collect(raw_issue, settings):
        from agent_runtime.copilot.evidence import SupportEvidencePack

        return SupportEvidencePack(
            raw_issue_hash="hash",
            query_chars=len(raw_issue),
            issue_type="unknown",
            product_model="",
            sku=[],
            official=[],
            history=[],
            media=[],
        )

    async def fake_run(agent, input_text, *, context=None, session=None, run_config=None):
        return SimpleNamespace(
            final_output=SupportAnswer(
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
            )
        )

    monkeypatch.setattr(support_runtime, "collect_support_evidence", fake_collect)
    monkeypatch.setattr(support_runtime.Runner, "run", fake_run)
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_1",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
    )

    reply = asyncio.run(bridge.run_support_agent_for_event(event, settings))

    assert "客服可以先这样回应客户" in reply
    assert "AI 客服参考" not in reply
    assert "问题类型：" not in reply
    assert "Agent SDK" not in reply
    assert "未查询到可信正式依据" not in reply


def test_feishu_agent_visible_validation_uses_safe_fallback(monkeypatch, tmp_path):
    settings = settings_for_tmp(tmp_path, llm_api_key="test-key")

    monkeypatch.setattr(bridge, "configure_agents_runtime", lambda settings: settings)
    monkeypatch.setattr(support_runtime, "build_support_copilot", lambda model: object())

    async def fake_collect(raw_issue, settings):
        from agent_runtime.copilot.evidence import SupportEvidencePack

        return SupportEvidencePack(
            raw_issue_hash="hash",
            query_chars=len(raw_issue),
            issue_type="unknown",
            product_model="",
            sku=[],
            official=[],
            history=[],
            media=[],
        )

    async def fake_run(agent, input_text, *, context=None, session=None, run_config=None):
        return SimpleNamespace(
            final_output=SupportAnswer(
                issue_type="unknown",
                run_mode="Agent SDK",
                confidence="低",
                confidence_reason="未查询到可信正式依据。",
                user_issue_summary="客户反馈设备异常。",
                sku_match="未在 SKU 目录中命中；需要补充订单 SKU、包装 SKU、产品铭牌或图片。",
                suggested_reply="可以退款。",
                troubleshooting_steps=["确认型号"],
                follow_up_questions=["请补充 SKU"],
                official_evidence="未查询到可信正式依据，不可编造。",
                history_reference="未查询到可信历史参考，不可编造。",
                ticket_draft="不建议生成工单，并说明原因。",
            )
        )

    monkeypatch.setattr(support_runtime, "collect_support_evidence", fake_collect)
    monkeypatch.setattr(support_runtime.Runner, "run", fake_run)
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_1",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
    )

    assert asyncio.run(bridge.run_support_agent_for_event(event, settings)) == FEISHU_VISIBLE_REPLY_FALLBACK


def test_feishu_agent_failure_fallback_does_not_expose_internal_error(monkeypatch, tmp_path):
    clear_runtime_state_for_tests()

    async def failing_agent(event, settings):
        raise RuntimeError("database password leaked stack trace")

    replies = []

    async def fake_reply(message_id, text, settings=None):
        replies.append(text)
        return ReplyResult(status="replied", reply_message_id="om_reply")

    monkeypatch.setattr(bridge, "run_support_agent_for_event", failing_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", fake_reply)
    settings = settings_for_tmp(tmp_path)
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_msg",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        thread_id="omt_thread",
    )

    assert asyncio.run(bridge.process_message_event(event, settings)) == "replied"
    assert replies == [FEISHU_VISIBLE_REPLY_FALLBACK]
    assert "password" not in replies[0]
    assert "错误" not in replies[0]


def test_feishu_bridge_logs_hash_identifiers(caplog, tmp_path):
    settings = settings_for_tmp(tmp_path)
    event = FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_raw_secret",
        chat_type="group",
        message_id="om_raw_secret",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
    )

    with caplog.at_level("INFO"):
        assert asyncio.run(bridge.process_message_event(event, settings)) == "ignored"

    assert "oc_raw_secret" not in caplog.text
    assert "om_raw_secret" not in caplog.text
    assert "chat_id_hash" in caplog.text
    assert "message_id_hash" in caplog.text
