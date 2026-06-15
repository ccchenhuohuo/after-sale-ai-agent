from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs

from agent_runtime.channels.openclaw_feishu.assets import support_assets_from_openclaw_payload
from agent_runtime.copilot.case_context import SupportAsset, SupportCaseRequest
from agent_runtime.copilot.evidence import short_hash


def build_support_case_request_from_openclaw(message: Mapping[str, Any]) -> SupportCaseRequest:
    message_id = _message_id(message)
    chat_id = _chat_id(message)
    thread_id = _thread_id(message)
    return SupportCaseRequest(
        request_id=f"openclaw-feishu:{short_hash(message_id or chat_id or _text_from_message(message))}",
        source="feishu",
        channel="openclaw_feishu",
        source_platform="feishu",
        user_text=_text_from_message(message),
        assets=support_assets_from_openclaw_payload(message, message_id=message_id),
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        sender_id=_sender_id(message),
        session_id=_session_id(chat_id, thread_id),
        trace_group_id=_trace_group_id(chat_id, thread_id),
        metadata={
            "channel": "openclaw_feishu",
            "chat_type": _chat_type(message),
            "content_type": _content_type(message),
            "root_id": _root_id(message),
            "parent_id": _parent_id(message),
        },
    )


def build_support_case_request_from_openclaw_batch(
    messages: Sequence[Mapping[str, Any]],
    *,
    batch_id: str = "",
) -> SupportCaseRequest:
    if not messages:
        raise ValueError("OpenClaw Feishu batch must include at least one message.")
    first = messages[0]
    chat_id = _chat_id(first)
    thread_id = _thread_id(first)
    message_ids = [_message_id(message) for message in messages]
    assets: list[SupportAsset] = []
    for message in messages:
        message_id = _message_id(message)
        assets.extend(support_assets_from_openclaw_payload(message, message_id=message_id))
    text = "\n".join(part for part in (_text_from_message(message) for message in messages) if part)
    stable_id = batch_id or "|".join(message_id for message_id in message_ids if message_id) or text
    return SupportCaseRequest(
        request_id=f"openclaw-feishu-batch:{short_hash(stable_id)}",
        source="feishu",
        channel="openclaw_feishu",
        source_platform="feishu",
        user_text=text,
        assets=assets,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_ids[-1] if message_ids else "",
        sender_id=_sender_id(first),
        session_id=_session_id(chat_id, thread_id),
        trace_group_id=_trace_group_id(chat_id, thread_id),
        metadata={
            "channel": "openclaw_feishu",
            "batch_id": batch_id,
            "batch_size": len(messages),
            "message_ids": [message_id for message_id in message_ids if message_id],
            "chat_type": _chat_type(first),
            "content_type": "batch",
        },
    )


def _text_from_message(message: Mapping[str, Any]) -> str:
    for key in ("BodyForAgent", "Body", "CommandBody", "RawBody", "MessageText", "messageText"):
        text = _first_value(message, key)
        if text:
            return text
    content = _first_present(message, "content", "Content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Mapping):
        return _first_value(content, "text", "plainText", "markdown", "body")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                parts.append(_first_value(item, "text", "plainText", "markdown", "body"))
        return "\n".join(part for part in parts if part).strip()
    return _first_value(message, "text", "plainText", "body")


def _message_id(message: Mapping[str, Any]) -> str:
    return _first_value(
        message,
        "messageId",
        "message_id",
        "id",
        "CurrentMessageId",
        "MessageSid",
    ) or _first_nested_value(message, ("rawMessage", "message_id"))


def _chat_id(message: Mapping[str, Any]) -> str:
    explicit = _first_value(
        message,
        "chatId",
        "chat_id",
        "ChatId",
    )
    if explicit:
        return explicit
    to = _first_value(message, "To")
    if to:
        return _normalize_feishu_target(to)
    return _first_nested_value(message, ("rawMessage", "chat_id"))


def _sender_id(message: Mapping[str, Any]) -> str:
    return (
        _first_value(message, "senderId", "sender_id", "SenderId", "From")
        or _first_nested_value(message, ("rawSender", "sender_id", "open_id"))
        or _first_nested_value(message, ("sender", "sender_id", "open_id"))
    )


def _chat_type(message: Mapping[str, Any]) -> str:
    return _first_value(
        message,
        "chatType",
        "chat_type",
    ) or _first_nested_value(message, ("rawMessage", "chat_type"))


def _content_type(message: Mapping[str, Any]) -> str:
    return _first_value(
        message,
        "contentType",
        "content_type",
        "MessageType",
    ) or _first_nested_value(message, ("rawMessage", "message_type"))


def _root_id(message: Mapping[str, Any]) -> str:
    return _first_value(
        message,
        "rootId",
        "root_id",
    ) or _first_nested_value(message, ("rawMessage", "root_id"))


def _parent_id(message: Mapping[str, Any]) -> str:
    return _first_value(
        message,
        "parentId",
        "parent_id",
    ) or _first_nested_value(message, ("rawMessage", "parent_id"))


def _thread_id(message: Mapping[str, Any]) -> str:
    return (
        _first_value(
            message,
            "threadId",
            "thread_id",
            "MessageThreadId",
            "rootId",
            "root_id",
        )
        or _first_nested_value(message, ("rawMessage", "thread_id"))
        or _first_nested_value(message, ("rawMessage", "root_id"))
        or _target_fragment_value(_first_value(message, "To"), "__feishu_thread_id")
        or _first_value(
            message,
            "messageId",
            "message_id",
            "id",
            "CurrentMessageId",
            "MessageSid",
        )
        or _first_nested_value(message, ("rawMessage", "message_id"))
    )


def _session_id(chat_id: str, thread_id: str) -> str:
    return f"openclaw-feishu:{chat_id}:thread:{thread_id}" if chat_id or thread_id else "openclaw-feishu"


def _trace_group_id(chat_id: str, thread_id: str) -> str:
    return f"openclaw-feishu:{short_hash(chat_id)}:thread:{short_hash(thread_id)}"


def _first_value(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_present(payload: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _first_nested_value(payload: Mapping[str, Any], path: tuple[str, ...]) -> str:
    current: object = payload
    for key in path:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(key)
    if current is None:
        return ""
    text = str(current).strip()
    return text


def _normalize_feishu_target(raw: str) -> str:
    target = raw.split("#", 1)[0].strip()
    prefixes = ("feishu:", "chat:", "user:", "open_id:")
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if target.startswith(prefix):
                target = target[len(prefix) :].strip()
                changed = True
    return target


def _target_fragment_value(raw: str, key: str) -> str:
    if "#" not in raw:
        return ""
    fragment = raw.split("#", 1)[1].strip()
    if not fragment:
        return ""
    values = parse_qs(fragment).get(key) or []
    return str(values[0]).strip() if values else ""
