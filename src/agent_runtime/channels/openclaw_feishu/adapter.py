from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_runtime.channels.openclaw_feishu.assets import support_assets_from_openclaw_resources
from agent_runtime.copilot.case_context import SupportAsset, SupportCaseRequest
from agent_runtime.copilot.evidence import short_hash


def build_support_case_request_from_openclaw(message: Mapping[str, Any]) -> SupportCaseRequest:
    message_id = _first_value(message, "messageId", "message_id", "id")
    chat_id = _first_value(message, "chatId", "chat_id")
    thread_id = _thread_id(message)
    return SupportCaseRequest(
        request_id=f"openclaw-feishu:{short_hash(message_id or chat_id or _text_from_message(message))}",
        source="feishu",
        user_text=_text_from_message(message),
        assets=support_assets_from_openclaw_resources(message.get("resources"), message_id=message_id),
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        sender_id=_first_value(message, "senderId", "sender_id"),
        session_id=_session_id(chat_id, thread_id),
        trace_group_id=_trace_group_id(chat_id, thread_id),
        metadata={
            "channel": "openclaw_feishu",
            "chat_type": _first_value(message, "chatType", "chat_type"),
            "content_type": _first_value(message, "contentType", "content_type"),
            "root_id": _first_value(message, "rootId", "root_id"),
            "parent_id": _first_value(message, "parentId", "parent_id"),
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
    chat_id = _first_value(first, "chatId", "chat_id")
    thread_id = _thread_id(first)
    message_ids = [_first_value(message, "messageId", "message_id", "id") for message in messages]
    assets: list[SupportAsset] = []
    for message in messages:
        message_id = _first_value(message, "messageId", "message_id", "id")
        assets.extend(support_assets_from_openclaw_resources(message.get("resources"), message_id=message_id))
    text = "\n".join(part for part in (_text_from_message(message) for message in messages) if part)
    stable_id = batch_id or "|".join(message_id for message_id in message_ids if message_id) or text
    return SupportCaseRequest(
        request_id=f"openclaw-feishu-batch:{short_hash(stable_id)}",
        source="feishu",
        user_text=text,
        assets=assets,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_ids[-1] if message_ids else "",
        sender_id=_first_value(first, "senderId", "sender_id"),
        session_id=_session_id(chat_id, thread_id),
        trace_group_id=_trace_group_id(chat_id, thread_id),
        metadata={
            "channel": "openclaw_feishu",
            "batch_id": batch_id,
            "batch_size": len(messages),
            "message_ids": [message_id for message_id in message_ids if message_id],
            "chat_type": _first_value(first, "chatType", "chat_type"),
            "content_type": "batch",
        },
    )


def _text_from_message(message: Mapping[str, Any]) -> str:
    content = message.get("content")
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


def _thread_id(message: Mapping[str, Any]) -> str:
    return _first_value(message, "threadId", "thread_id", "rootId", "root_id", "messageId", "message_id", "id")


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
