import asyncio

import pytest

from agent_runtime.feishu import event_sources
from agent_runtime.settings import Settings


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

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
                        {"message_id": "om_new", "create_time": "1003"},
                        {"message_id": "om_old", "create_time": "1001"},
                    ],
                    "has_more": False,
                },
            }
        )


def test_fetch_recent_chat_messages_uses_chat_window_and_sorts(monkeypatch):
    _FakeAsyncClient.requests = []

    async def fake_token(settings):
        return "tenant-token"

    monkeypatch.setattr(event_sources, "get_tenant_access_token", fake_token)
    monkeypatch.setattr(event_sources.httpx, "AsyncClient", _FakeAsyncClient)
    settings = Settings(
        feishu_support_group_chat_id="oc_target",
        feishu_backfill_lookback_seconds=60,
        feishu_backfill_page_size=2,
    )

    messages = asyncio.run(event_sources.fetch_recent_chat_messages(settings, now=1000))

    assert [message["message_id"] for message in messages] == ["om_old", "om_new"]
    request = _FakeAsyncClient.requests[0]
    assert request["params"]["container_id_type"] == "chat"
    assert request["params"]["container_id"] == "oc_target"
    assert request["params"]["start_time"] == 945
    assert request["params"]["end_time"] == 1005
    assert request["params"]["page_size"] == 2
    assert request["params"]["sort_type"] == "ByCreateTimeAsc"
    assert request["params"]["only_thread_root_messages"] is True
    assert request["params"]["card_msg_content_type"] == "raw_card_content"
    assert request["headers"]["Authorization"] == "Bearer tenant-token"


def test_fetch_recent_chat_messages_expands_comma_separated_chat_ids(monkeypatch):
    class MultiChatClient:
        requests = []

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, *, params=None, headers=None):
            self.requests.append({"url": url, "params": params, "headers": headers})
            chat_id = params["container_id"]
            return _FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [{"message_id": f"om_{chat_id}", "create_time": "1001"}],
                        "has_more": False,
                    },
                }
            )

    MultiChatClient.requests = []

    async def fake_token(settings):
        return "tenant-token"

    monkeypatch.setattr(event_sources, "get_tenant_access_token", fake_token)
    monkeypatch.setattr(event_sources.httpx, "AsyncClient", MultiChatClient)
    settings = Settings(feishu_support_group_chat_id="oc_a, oc_b, oc_a")

    messages = asyncio.run(event_sources.fetch_recent_chat_messages(settings, now=1000))

    assert [request["params"]["container_id"] for request in MultiChatClient.requests] == ["oc_a", "oc_b"]
    assert [message["message_id"] for message in messages] == ["om_oc_a", "om_oc_b"]


def test_feishu_error_response_exposes_sanitized_code_and_message():
    response = _FakeResponse(
        {"code": 230027, "msg": "Lack of necessary permissions, ext=need scope: im:message.group_msg"},
        status_code=400,
    )

    with pytest.raises(RuntimeError) as exc_info:
        event_sources._feishu_json_payload(response, "List Feishu chat messages")

    message = str(exc_info.value)
    assert "status=400" in message
    assert "code=230027" in message
    assert "need scope: im:message.group_msg" in message
    assert "tenant-token" not in message
