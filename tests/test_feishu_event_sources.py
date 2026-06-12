import asyncio

from agent_runtime.feishu import event_sources
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
    assert request["headers"]["Authorization"] == "Bearer tenant-token"
