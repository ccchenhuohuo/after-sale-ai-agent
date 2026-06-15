from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent_runtime.feishu.parser import extract_text_content


@dataclass(frozen=True)
class FeishuMessageEvent:
    event_id: str
    chat_id: str
    chat_type: str
    message_id: str
    message_type: str
    sender_id: str
    content: str
    sender_type: str = ""
    mention_names: tuple[str, ...] = ()
    mention_ids: tuple[str, ...] = ()
    thread_id: str = ""
    root_id: str = ""
    parent_id: str = ""
    create_time: str = ""
    event_source: str = "sdk"
    raw_content: object = field(default=None, compare=False)


def _field(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value is not None:
            return str(value)
    return ""


def _content(payload: dict[str, Any]) -> object:
    if "content" in payload:
        return payload.get("content")
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    return body.get("content")


def _mention_names(mentions: object) -> tuple[str, ...]:
    if not isinstance(mentions, list):
        return ()
    return tuple(str(item.get("name")) for item in mentions if isinstance(item, dict) and item.get("name"))


def _mention_ids(mentions: object) -> tuple[str, ...]:
    if not isinstance(mentions, list):
        return ()
    ids: list[str] = []
    for item in mentions:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if isinstance(raw_id, dict):
            mention_id = raw_id.get("open_id") or raw_id.get("union_id") or raw_id.get("user_id")
        else:
            mention_id = raw_id
        if mention_id is not None:
            ids.append(str(mention_id))
    return tuple(ids)


def event_from_payload(payload: dict[str, Any]) -> FeishuMessageEvent | None:
    """Normalize Feishu HTTP callback, SDK callback, or OpenAPI message payload."""
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_source = str(payload.get("_agent_runtime_event_source") or "sdk")

    message = event.get("message") if isinstance(event.get("message"), dict) else None
    if message:
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        mentions = message.get("mentions") if isinstance(message.get("mentions"), list) else []
        return FeishuMessageEvent(
            event_id=str(header.get("event_id") or payload.get("event_id") or ""),
            chat_id=str(message.get("chat_id") or ""),
            chat_type=str(message.get("chat_type") or ""),
            message_id=str(message.get("message_id") or ""),
            message_type=str(message.get("message_type") or ""),
            sender_id=str(sender_id.get("open_id") or event.get("sender_id") or ""),
            content=extract_text_content(message.get("content")),
            raw_content=message.get("content"),
            sender_type=str(sender.get("sender_type") or ""),
            mention_names=_mention_names(mentions),
            mention_ids=_mention_ids(mentions),
            thread_id=_field(message, "thread_id"),
            root_id=_field(message, "root_id"),
            parent_id=_field(message, "parent_id", "upper_message_id"),
            create_time=_field(message, "create_time", "create_time_ms"),
            event_source=event_source,
        )

    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    mentions = payload.get("mentions") if isinstance(payload.get("mentions"), list) else []
    return FeishuMessageEvent(
        event_id=str(payload.get("event_id") or ""),
        chat_id=str(payload.get("chat_id") or ""),
        chat_type=str(payload.get("chat_type") or ""),
        message_id=str(payload.get("message_id") or payload.get("id") or ""),
        message_type=str(payload.get("message_type") or payload.get("msg_type") or ""),
        sender_id=str(payload.get("sender_id") or sender_id.get("open_id") or sender.get("id") or ""),
        content=extract_text_content(_content(payload)),
        raw_content=_content(payload),
        sender_type=str(payload.get("sender_type") or sender.get("sender_type") or ""),
        mention_names=_mention_names(mentions),
        mention_ids=_mention_ids(mentions),
        thread_id=_field(payload, "thread_id"),
        root_id=_field(payload, "root_id"),
        parent_id=_field(payload, "parent_id", "upper_message_id"),
        create_time=_field(payload, "create_time", "create_time_ms"),
        event_source=event_source,
    )


def effective_thread_id(event: FeishuMessageEvent) -> str:
    return event.thread_id or event.root_id or event.message_id


def session_id_for_event(event: FeishuMessageEvent) -> str:
    return f"feishu:{event.chat_id}:thread:{effective_thread_id(event)}"


def queue_key_for_event(event: FeishuMessageEvent) -> str:
    return f"{event.chat_id}:{effective_thread_id(event)}"


def parse_event_timestamp(event: FeishuMessageEvent) -> float | None:
    raw_value = event.create_time.strip()
    if not raw_value:
        return None
    if raw_value.isdigit():
        value = int(raw_value)
        if value > 10_000_000_000:
            return value / 1000
        return float(value)
    try:
        normalized = raw_value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def now_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()
