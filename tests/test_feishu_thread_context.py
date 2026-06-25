import asyncio

from agent_runtime.copilot.case_context import SupportCaseRequest
from agent_runtime.feishu.admission import BotIdentity
from agent_runtime.feishu.events import FeishuMessageEvent
from agent_runtime.feishu import event_sources as thread_context
from agent_runtime.feishu.event_sources import (
    ThreadContext,
    fetch_thread_messages,
    merge_thread_context_into_request,
    render_thread_context,
)
from agent_runtime.settings import Settings


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    requests = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, *, params=None, headers=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        return _FakeResponse(
            {
                "code": 0,
                "data": {
                    "items": [
                        {"message_id": "om_2", "create_time": "1002"},
                        {"message_id": "om_1", "create_time": "1001"},
                    ],
                    "has_more": False,
                },
            }
        )


def _text_payload(message_id, text, *, sender_id="ou_sender", sender_type="user", create_time="1000", name="陈煜"):
    return {
        "chat_id": "oc_target",
        "message_id": message_id,
        "msg_type": "text",
        "body": {"content": f'{{"text":"{text}"}}'},
        "sender": {"id": sender_id, "sender_type": sender_type, "name": name},
        "create_time": create_time,
    }


def test_fetch_thread_messages_uses_thread_container_and_sorts(monkeypatch):
    _FakeAsyncClient.requests = []

    async def fake_token(settings):
        return "tenant-token"

    monkeypatch.setattr(thread_context, "get_tenant_access_token", fake_token)
    monkeypatch.setattr(thread_context.httpx, "AsyncClient", _FakeAsyncClient)
    settings = Settings(feishu_thread_context_max_messages=80)

    messages = asyncio.run(fetch_thread_messages(settings, "omt_thread"))

    assert [message["message_id"] for message in messages] == ["om_1", "om_2"]
    request = _FakeAsyncClient.requests[0]
    assert request["params"]["container_id_type"] == "thread"
    assert request["params"]["container_id"] == "omt_thread"
    assert request["params"]["page_size"] == 50
    assert request["headers"]["Authorization"] == "Bearer tenant-token"


def test_render_thread_context_excludes_bot_and_renders_media_placeholders():
    settings = Settings(feishu_app_id="cli_app", feishu_thread_context_max_messages=10)
    trigger = FeishuMessageEvent(
        event_id="evt_trigger",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_trigger",
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI 现在怎么处理",
        mention_names=("飞书 CLI",),
        create_time="1004",
    )
    payloads = [
        _text_payload("om_root", "客户反馈 L023 不亮", create_time="1000", name="客户A"),
        _text_payload(
            "om_bot",
            "机器人历史回复不应进入上下文",
            sender_id="cli_app",
            sender_type="app",
            create_time="1001",
            name="VIJIM-AI售后客服",
        ),
        {
            "chat_id": "oc_target",
            "message_id": "om_img",
            "msg_type": "image",
            "body": {"content": '{"image_key":"img_v3_abc","file_name":"故障图.png"}'},
            "sender": {"id": "ou_sender", "sender_type": "user", "name": "客户A"},
            "create_time": "1002",
        },
        _text_payload("om_reply", "补充：插电没有反应", create_time="1003", name="客服B"),
    ]

    context = render_thread_context(payloads, trigger, settings, BotIdentity(app_id="cli_app"))

    assert context.message_count == 4
    assert not context.truncated
    assert "客户反馈 L023 不亮" in context.text
    assert "[图片：故障图.png]" in context.text
    assert "补充：插电没有反应" in context.text
    assert "现在怎么处理" in context.text
    assert "机器人历史回复" not in context.text


def test_render_thread_context_preserves_root_and_recent_messages_when_truncated():
    settings = Settings(feishu_thread_context_max_messages=3, feishu_thread_context_max_chars=120)
    trigger = FeishuMessageEvent(
        event_id="evt_trigger",
        chat_id="oc_target",
        chat_type="group",
        message_id="om_trigger",
        message_type="text",
        sender_id="ou_sender",
        content="最后请总结处理建议",
        create_time="1005",
    )
    payloads = [
        _text_payload("om_root", "根消息：客户反馈 L023 不亮", create_time="1000"),
        _text_payload("om_mid_1", "中间消息一 " * 10, create_time="1001"),
        _text_payload("om_mid_2", "中间消息二 " * 10, create_time="1002"),
        _text_payload("om_recent", "最近补充：插电没有反应", create_time="1004"),
    ]

    context = render_thread_context(payloads, trigger, settings)

    assert context.truncated
    assert "根消息：客户反馈 L023 不亮" in context.text
    assert "最后请总结处理建议" in context.text
    assert "中间消息一" not in context.text


def test_merge_thread_context_into_request_updates_user_text_and_metadata():
    request = SupportCaseRequest(
        request_id="case_1",
        source="feishu",
        user_text="现在怎么处理",
        metadata={"message_type": "text"},
    )
    context = ThreadContext(text="1. 客户A: L023 不亮\n2. 客服B: 插电无反应", message_count=2, truncated=True)

    merged = merge_thread_context_into_request(request, context)

    assert "当前触发消息" in merged.user_text
    assert "现在怎么处理" in merged.user_text
    assert "客户A: L023 不亮" in merged.user_text
    assert merged.metadata["message_type"] == "text"
    assert merged.metadata["thread_context_message_count"] == 2
    assert merged.metadata["thread_context_truncated"] is True
