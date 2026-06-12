import asyncio
import logging

from agent_runtime.feishu import event_sources, long_connection
from agent_runtime.feishu.admission import BotIdentity
from agent_runtime.settings import Settings


def _settings(**overrides):
    values = {
        "feishu_support_group_chat_id": "oc_target",
        "feishu_bot_mention_name": "飞书 CLI",
        "feishu_event_max_age_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


def _bot_identity() -> BotIdentity:
    return BotIdentity(app_id="cli_app", open_id="ou_bot", names=("飞书 CLI",))


def _openapi_message(
    *,
    message_id: str,
    text: str,
    sender_id: str = "ou_sender",
    sender_type: str = "user",
    msg_type: str = "text",
    mentions=None,
):
    return {
        "chat_id": "oc_target",
        "message_id": message_id,
        "msg_type": msg_type,
        "body": {"content": f'{{"text":"{text}"}}'},
        "mentions": mentions or [],
        "sender": {"id": sender_id, "sender_type": sender_type},
    }


def _sdk_message_payload(*, sender_id: str, sender_type: str = "user"):
    return {
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "chat_id": "oc_target",
                "chat_type": "group",
                "message_id": "om_sdk",
                "message_type": "text",
                "content": '{"text":"@_user_1 AI分析：S043 黑屏"}',
                "mentions": [{"id": {"open_id": "ou_bot"}, "name": "飞书 CLI"}],
            },
            "sender": {"sender_id": {"open_id": sender_id}, "sender_type": sender_type},
        },
    }


def test_backfill_prefilter_only_schedules_real_user_trigger(monkeypatch):
    app_error = _openapi_message(
        message_id="om_error",
        text="Error:",
        sender_id="cli_app",
        sender_type="app",
        msg_type="post",
    )
    no_trigger = _openapi_message(message_id="om_no_trigger", text="普通同步消息")
    user_trigger = _openapi_message(
        message_id="om_user",
        text="@_user_1 AI分析：S043 黑屏",
        mentions=[{"id": {"open_id": "ou_bot"}, "name": "飞书 CLI"}],
    )
    scheduled = []

    async def fake_fetch(settings):
        return [app_error, no_trigger, user_trigger]

    def fake_track(tasks, payload, settings, semaphore, bot_identity):
        scheduled.append(payload["message_id"])

    async def runner():
        monkeypatch.setattr(long_connection, "fetch_recent_chat_messages", fake_fetch)
        monkeypatch.setattr(long_connection, "_track_processing_task", fake_track)
        return await long_connection._backfill_once(set(), _settings(), asyncio.Semaphore(1), _bot_identity())

    assert asyncio.run(runner()) == 1
    assert scheduled == ["om_user"]


def test_backfill_admission_rejects_app_post_error():
    payload = _openapi_message(
        message_id="om_error",
        text="Error:",
        sender_id="cli_app",
        sender_type="app",
        msg_type="post",
    )

    admission = long_connection._backfill_payload_admission(payload, _settings(), _bot_identity())

    assert not admission.accepted
    assert admission.status == "skipped_app_or_bot"


def test_backfill_admission_rejects_user_text_without_trigger():
    payload = _openapi_message(message_id="om_no_trigger", text="普通同步消息")

    admission = long_connection._backfill_payload_admission(payload, _settings(), _bot_identity())

    assert not admission.accepted
    assert admission.status == "skipped_no_trigger"


def test_websocket_self_echo_is_not_scheduled(monkeypatch):
    scheduled = []

    def fake_track(tasks, payload, settings, semaphore, bot_identity):
        scheduled.append(payload)

    async def runner():
        monkeypatch.setattr(long_connection, "_track_processing_task", fake_track)
        return await long_connection._schedule_websocket_payload(
            set(),
            _sdk_message_payload(sender_id="ou_bot"),
            _settings(),
            asyncio.Semaphore(1),
            _bot_identity(),
        )

    assert asyncio.run(runner()) == "skipped_app_or_bot"
    assert scheduled == []


def test_reaction_events_are_registered_as_noop_events():
    assert event_sources.is_noop_event_type("im.message.reaction.created_v1")
    assert event_sources.is_noop_event_type("im.message.reaction.deleted_v1")
    assert not event_sources.is_noop_event_type(event_sources.EVENT_KEY)


def test_runtime_logging_suppresses_raw_http_client_urls():
    long_connection.configure_runtime_logging()

    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
