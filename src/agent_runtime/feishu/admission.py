from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.feishu.events import FeishuMessageEvent, now_timestamp, parse_event_timestamp
from agent_runtime.feishu.parser import should_trigger_ai
from agent_runtime.feishu.runtime_store import RuntimeStore
from agent_runtime.settings import Settings


MEDIA_MESSAGE_TYPES = {"image", "video", "file", "audio"}


@dataclass(frozen=True)
class BotIdentity:
    app_id: str = ""
    open_id: str = ""
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateResult:
    accepted: bool
    status: str


def split_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def should_accept(
    event: FeishuMessageEvent,
    settings: Settings,
    bot_identity: BotIdentity | None = None,
    runtime_store: RuntimeStore | None = None,
) -> GateResult:
    bot_identity = bot_identity or BotIdentity(
        app_id=settings.feishu_app_id,
        open_id=settings.feishu_bot_open_id,
        names=(settings.feishu_bot_mention_name,) if settings.feishu_bot_mention_name else (),
    )
    if not event.message_id or not event.chat_id:
        return GateResult(False, "ignored")
    if event.chat_type and event.chat_type != "group":
        return GateResult(False, "ignored")
    if event.message_type == "interactive":
        return GateResult(False, "ignored")

    allowed_chat_ids = split_csv(settings.feishu_support_group_chat_id)
    if allowed_chat_ids and event.chat_id not in allowed_chat_ids:
        return GateResult(False, "ignored")

    allowed_user_ids = split_csv(settings.feishu_allowed_user_open_ids)
    if allowed_user_ids and event.sender_id not in allowed_user_ids:
        return GateResult(False, "ignored")

    if _is_expired(event, settings):
        return GateResult(False, "expired")

    is_bot_sender = _is_bot_sender(event, bot_identity)
    if runtime_store is not None:
        bot_turns = runtime_store.record_sender_turn(event, is_bot_sender)
        if is_bot_sender and bot_turns > settings.feishu_bot_loop_max_turns:
            return GateResult(False, "suppressed_bot_loop")
    if is_bot_sender:
        return GateResult(False, "ignored_bot_sender")

    if _mentions_bot(event, settings, bot_identity):
        return GateResult(True, "accepted")
    if _media_auto_accept(event, settings):
        return GateResult(True, "accepted_media")
    return GateResult(False, "ignored")


def _is_expired(event: FeishuMessageEvent, settings: Settings) -> bool:
    if settings.feishu_event_max_age_seconds <= 0:
        return False
    timestamp = parse_event_timestamp(event)
    if timestamp is None:
        return False
    return now_timestamp() - timestamp > settings.feishu_event_max_age_seconds


def _is_bot_sender(event: FeishuMessageEvent, bot_identity: BotIdentity) -> bool:
    if event.sender_type in {"app", "bot"}:
        return True
    if bot_identity.app_id and event.sender_id == bot_identity.app_id:
        return True
    if bot_identity.open_id and event.sender_id == bot_identity.open_id:
        return True
    return False


def _mentions_bot(event: FeishuMessageEvent, settings: Settings, bot_identity: BotIdentity) -> bool:
    if bot_identity.open_id and bot_identity.open_id in event.mention_ids:
        return True
    if bot_identity.app_id and bot_identity.app_id in event.mention_ids:
        return True

    names = set(bot_identity.names)
    if settings.feishu_bot_mention_name:
        names.add(settings.feishu_bot_mention_name)
    if names and any(name in event.mention_names for name in names):
        return True

    mention_names = tuple(name for name in names if name)
    return should_trigger_ai(event.content, settings.support_agent_trigger_prefix, mention_names)


def _media_auto_accept(event: FeishuMessageEvent, settings: Settings) -> bool:
    if not settings.feishu_media_auto_accept_enabled:
        return False
    if event.message_type not in MEDIA_MESSAGE_TYPES:
        return False
    allowed_chat_ids = split_csv(settings.feishu_support_group_chat_id)
    return bool(allowed_chat_ids and event.chat_id in allowed_chat_ids)
