import asyncio

import httpx

import agent_runtime.feishu.reactions as reactions
from agent_runtime.settings import Settings


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("POST", "https://open.feishu.cn"),
                response=httpx.Response(self.status_code),
            )

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
        return False

    async def post(self, url, *, headers=None, json=None):
        self.requests.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return _FakeResponse({"code": 0, "data": {"reaction_id": "reaction_1"}})

    async def delete(self, url, *, headers=None):
        self.requests.append({"method": "DELETE", "url": url, "headers": headers})
        return _FakeResponse({"code": 0, "data": {"reaction_id": "reaction_1"}})


def test_working_reaction_create_and_delete_call_feishu_api(monkeypatch):
    _FakeAsyncClient.requests = []

    async def fake_token(settings):
        return "tenant-token"

    monkeypatch.setattr(reactions, "get_tenant_access_token", fake_token)
    monkeypatch.setattr(reactions.httpx, "AsyncClient", _FakeAsyncClient)
    settings = Settings(
        feishu_working_reaction_enabled=True,
        feishu_working_reaction_emoji_type="OnIt",
        feishu_working_reaction_timeout_seconds=3,
    )

    reaction = asyncio.run(reactions.create_working_reaction("om_msg", settings))
    asyncio.run(reactions.delete_working_reaction(reaction, settings))

    assert reaction is not None
    assert reaction.reaction_id == "reaction_1"
    assert reaction.emoji_type == "OnIt"
    assert _FakeAsyncClient.requests == [
        {
            "method": "POST",
            "url": "https://open.feishu.cn/open-apis/im/v1/messages/om_msg/reactions",
            "headers": {"Authorization": "Bearer tenant-token"},
            "json": {"reaction_type": {"emoji_type": "OnIt"}},
        },
        {
            "method": "DELETE",
            "url": "https://open.feishu.cn/open-apis/im/v1/messages/om_msg/reactions/reaction_1",
            "headers": {"Authorization": "Bearer tenant-token"},
        },
    ]


def test_working_reaction_failure_is_non_blocking(monkeypatch):
    async def failing_token(settings):
        raise RuntimeError("missing secret")

    monkeypatch.setattr(reactions, "get_tenant_access_token", failing_token)
    settings = Settings(feishu_working_reaction_enabled=True)

    assert asyncio.run(reactions.create_working_reaction("om_msg", settings)) is None
