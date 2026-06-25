from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from agents import custom_span

from agent_runtime.copilot.case_context import SupportCaseRequest
from agent_runtime.copilot.reference_safety import redact_internal_references
from agent_runtime.feishu.admission import BotIdentity
from agent_runtime.feishu.events import (
    FeishuMessageEvent,
    effective_thread_id,
    event_from_payload,
    parse_event_timestamp,
)
from agent_runtime.feishu.message_sender import FEISHU_BASE_URL, get_tenant_access_token
from agent_runtime.settings import Settings


PayloadHandler = Callable[[dict[str, Any]], Awaitable[None]]

EVENT_KEY = "im.message.receive_v1"
NOOP_EVENT_TYPES = {
    "im.message.reaction.created_v1",
    "im.message.reaction.deleted_v1",
}
THREAD_CONTEXT_MESSAGE_TYPES = {"text", "post", "image", "video", "file", "audio"}
MEDIA_LABELS = {
    "image": "图片",
    "video": "视频",
    "audio": "音频",
    "file": "文件",
    "media": "媒体",
}

logger = logging.getLogger(__name__)


def _feishu_json_payload(response: httpx.Response, operation: str) -> dict[str, Any]:
    status_code = int(getattr(response, "status_code", 200) or 200)
    try:
        payload = response.json()
    except ValueError as exc:
        if status_code >= 400:
            raise RuntimeError(f"{operation} failed: status={status_code} code=unknown msg=non_json_response") from exc
        raise RuntimeError(f"{operation} returned non-JSON response: status={status_code}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{operation} returned unexpected response shape: status={status_code}")

    raw_code = payload.get("code", 0)
    try:
        code = int(raw_code or 0)
    except (TypeError, ValueError):
        code = -1
    if status_code >= 400 or code != 0:
        msg = str(payload.get("msg") or "")[:300]
        raise RuntimeError(f"{operation} failed: status={status_code} code={raw_code or 'unknown'} msg={msg}")
    return payload


@dataclass(frozen=True)
class ThreadContext:
    text: str = ""
    message_count: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class _ContextMessage:
    event: FeishuMessageEvent
    sender_name: str = ""
    source_index: int = 0


class FeishuEventSource(ABC):
    @abstractmethod
    async def run(self, handler: PayloadHandler) -> int:
        """Run the event source until stopped."""


class LarkOapiEventSource(FeishuEventSource):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, handler: PayloadHandler) -> int:
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret:
            raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required for Feishu SDK WebSocket")
        parent_loop = asyncio.get_running_loop()
        result: Future[int] = Future()
        thread = threading.Thread(
            target=self._run_client,
            args=(parent_loop, handler, result),
            name="feishu-sdk-websocket",
            daemon=True,
        )

        logger.info("Starting Feishu SDK WebSocket event source for %s.", EVENT_KEY)
        thread.start()
        try:
            return await asyncio.wrap_future(result)
        except asyncio.CancelledError:
            logger.info("Stopping Feishu SDK WebSocket event source.")
            raise

    def _run_client(
        self,
        parent_loop: asyncio.AbstractEventLoop,
        handler: PayloadHandler,
        result: Future[int],
    ) -> None:
        thread_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(thread_loop)
        try:
            try:
                import lark_oapi as lark
                import lark_oapi.ws.client as ws_client
            except ImportError as exc:
                raise RuntimeError("lark-oapi is required for Feishu SDK WebSocket") from exc

            ws_client.loop = thread_loop

            def on_message(data: Any) -> None:
                try:
                    payload = payload_from_lark_oapi_event(data)
                    event_type = _payload_event_type(payload)
                    event_id = _payload_event_id(payload)
                    with custom_span(
                        "receive_event",
                        {
                            "event_type": event_type,
                            "event_id_hash": _short_hash(event_id),
                        },
                    ):
                        logger.info(
                            "Received Feishu SDK event: event_type=%s event_id_hash=%s.",
                            event_type,
                            _short_hash(event_id),
                        )
                    future = asyncio.run_coroutine_threadsafe(handler(payload), parent_loop)
                    future.add_done_callback(_log_handler_failure)
                except Exception:
                    logger.exception("Failed to dispatch Feishu SDK event.")

            def on_ignored_event(data: Any) -> None:
                payload = payload_from_lark_oapi_event(data)
                event_type = _payload_event_type(payload)
                event_id = _payload_event_id(payload)
                logger.info(
                    "Ignored Feishu SDK event type: status=%s event_type=%s event_id_hash=%s.",
                    "noop_reaction_event" if is_noop_event_type(event_type) else "ignored_sdk_event",
                    event_type,
                    _short_hash(event_id),
                )

            event_handler_builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
                on_message
            )
            event_handler_builder = _register_optional_sdk_handler(
                event_handler_builder,
                "register_p2_im_message_reaction_created_v1",
                on_ignored_event,
            )
            event_handler_builder = _register_optional_sdk_handler(
                event_handler_builder,
                "register_p2_im_message_reaction_deleted_v1",
                on_ignored_event,
            )
            event_handler = event_handler_builder.build()
            client = lark.ws.Client(
                self.settings.feishu_app_id,
                self.settings.feishu_app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.ERROR,
            )
            client.start()
            _set_future_result(result, 0)
        except Exception as exc:
            _set_future_exception(result, exc)
        finally:
            if not thread_loop.is_closed():
                thread_loop.close()


def payload_from_lark_oapi_event(data: Any) -> dict[str, Any]:
    payload = _to_plain_payload(data)
    if isinstance(payload, dict):
        return payload
    return {}


def _to_plain_payload(data: Any) -> Any:
    if isinstance(data, (str, int, float, bool)) or data is None:
        return data
    if isinstance(data, dict):
        return {str(key): _to_plain_payload(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_to_plain_payload(item) for item in data]

    raw = getattr(data, "raw", None)
    if isinstance(raw, dict):
        return _to_plain_payload(raw)
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            return _to_plain_payload(payload)
    if hasattr(data, "model_dump"):
        payload = data.model_dump()
        if isinstance(payload, dict):
            return _to_plain_payload(payload)
    if hasattr(data, "to_dict"):
        payload = data.to_dict()
        if isinstance(payload, dict):
            return _to_plain_payload(payload)
    payload = getattr(data, "__dict__", {})
    if isinstance(payload, dict):
        return {key: _to_plain_payload(value) for key, value in payload.items() if not key.startswith("_")}
    return data


def _payload_event_type(payload: dict[str, Any]) -> str:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    return str(header.get("event_type") or payload.get("event_type") or "")


def _payload_event_id(payload: dict[str, Any]) -> str:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    return str(header.get("event_id") or payload.get("event_id") or "")


def is_noop_event_type(event_type: str) -> bool:
    return event_type in NOOP_EVENT_TYPES


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12] if value else ""


def _register_optional_sdk_handler(builder: Any, method_name: str, handler: Callable[[Any], None]) -> Any:
    register = getattr(builder, method_name, None)
    if not callable(register):
        logger.warning("Feishu SDK event handler registration is unavailable: %s", method_name)
        return builder
    return register(handler)


def _log_handler_failure(future: Future[Any]) -> None:
    try:
        future.result()
    except Exception:
        logger.exception("Feishu SDK event handler failed.")


async def fetch_bot_open_id(settings: Settings) -> str:
    if settings.feishu_bot_open_id:
        return settings.feishu_bot_open_id
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
            )
            token_payload = token_response.json()
            token = token_payload.get("tenant_access_token")
            if not token:
                logger.warning("Failed to fetch Feishu tenant access token for bot identity.")
                return ""
            bot_response = await client.get(
                "https://open.feishu.cn/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
            )
            bot_payload = bot_response.json()
            data = bot_payload.get("data") if isinstance(bot_payload.get("data"), dict) else bot_payload
            bot = data.get("bot") if isinstance(data.get("bot"), dict) else data
            open_id = bot.get("open_id") if isinstance(bot, dict) else ""
            return str(open_id or "")
    except (httpx.HTTPError, ValueError, TypeError):
        logger.warning("Failed to fetch Feishu bot identity.", exc_info=True)
        return ""


async def fetch_thread_messages(settings: Settings, thread_id: str) -> list[dict[str, Any]]:
    if not thread_id:
        return []

    token = await get_tenant_access_token(settings)
    max_messages = max(1, settings.feishu_thread_context_max_messages)
    fetch_limit = max_messages + 1
    page_size = min(fetch_limit, 50)
    page_token = ""
    messages: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=10) as client:
        while len(messages) < fetch_limit:
            params: dict[str, object] = {
                "container_id_type": "thread",
                "container_id": thread_id,
                "sort_type": "ByCreateTimeAsc",
                "page_size": page_size,
            }
            if page_token:
                params["page_token"] = page_token
            response = await client.get(
                f"{FEISHU_BASE_URL}/im/v1/messages",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            payload = _feishu_json_payload(response, "List Feishu thread messages")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            for item in data.get("items") or []:
                if isinstance(item, dict):
                    messages.append(item)
                if len(messages) >= fetch_limit:
                    break
            if len(messages) >= fetch_limit or not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                break
    return sorted(messages, key=_message_sort_key)


async def build_thread_context_for_event(
    event: FeishuMessageEvent,
    settings: Settings,
    bot_identity: BotIdentity | None = None,
) -> ThreadContext:
    if not settings.feishu_thread_context_enabled:
        return ThreadContext()
    thread_id = effective_thread_id(event)
    if not thread_id:
        return ThreadContext()
    payloads = await fetch_thread_messages(settings, thread_id)
    return render_thread_context(payloads, event, settings, bot_identity)


def merge_thread_context_into_request(
    request: SupportCaseRequest,
    context: ThreadContext,
) -> SupportCaseRequest:
    metadata = {
        **request.metadata,
        "thread_context_message_count": context.message_count,
        "thread_context_truncated": context.truncated,
    }
    if not context.text:
        return request.model_copy(update={"metadata": metadata})
    return request.model_copy(
        update={
            "user_text": _compose_user_text_with_thread_context(request.user_text, context.text),
            "metadata": metadata,
        }
    )


def render_thread_context(
    payloads: list[dict[str, Any]],
    trigger_event: FeishuMessageEvent,
    settings: Settings,
    bot_identity: BotIdentity | None = None,
) -> ThreadContext:
    bot_identity = bot_identity or BotIdentity(
        app_id=settings.feishu_app_id,
        open_id=settings.feishu_bot_open_id,
        names=(settings.feishu_bot_mention_name,) if settings.feishu_bot_mention_name else (),
    )
    messages = _context_messages_from_payloads(payloads, trigger_event)
    messages = _filter_context_messages(messages, settings, bot_identity)
    if not messages:
        return ThreadContext()

    messages, count_truncated = _limit_messages(messages, max(1, settings.feishu_thread_context_max_messages))
    lines = _render_context_lines(messages, bot_identity)
    text, char_truncated = _apply_char_cap(lines, max(0, settings.feishu_thread_context_max_chars))
    return ThreadContext(
        text=text,
        message_count=len(messages),
        truncated=count_truncated or char_truncated,
    )


def _context_messages_from_payloads(
    payloads: list[dict[str, Any]],
    trigger_event: FeishuMessageEvent,
) -> list[_ContextMessage]:
    messages: list[_ContextMessage] = []
    seen_message_ids: set[str] = set()
    for index, payload in enumerate(payloads):
        event = event_from_payload(payload)
        if event is None or not event.message_id:
            continue
        seen_message_ids.add(event.message_id)
        messages.append(
            _ContextMessage(
                event=event,
                sender_name=_sender_name_from_payload(payload),
                source_index=index,
            )
        )

    if trigger_event.message_id and trigger_event.message_id not in seen_message_ids:
        messages.append(_ContextMessage(event=trigger_event, source_index=len(messages)))

    return sorted(messages, key=_context_message_sort_key)


def _filter_context_messages(
    messages: list[_ContextMessage],
    settings: Settings,
    bot_identity: BotIdentity,
) -> list[_ContextMessage]:
    output: list[_ContextMessage] = []
    for message in messages:
        event = message.event
        if event.message_type not in THREAD_CONTEXT_MESSAGE_TYPES:
            continue
        if not settings.feishu_thread_context_include_bot and _is_bot_sender(event, bot_identity):
            continue
        content = _message_content_for_context(event)
        if not content:
            continue
        output.append(message)
    return output


def _limit_messages(messages: list[_ContextMessage], max_messages: int) -> tuple[list[_ContextMessage], bool]:
    if len(messages) <= max_messages:
        return messages, False
    if max_messages == 1:
        return [messages[-1]], True
    return [messages[0], *messages[-(max_messages - 1) :]], True


def _render_context_lines(messages: list[_ContextMessage], bot_identity: BotIdentity) -> list[str]:
    sender_labels: dict[str, str] = {}
    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        label = _sender_label(message, sender_labels, bot_identity)
        content = _message_content_for_context(message.event)
        lines.append(f"{index}. {label}: {content}")
    return lines


def _compose_user_text_with_thread_context(current_text: str, thread_context: str) -> str:
    current = current_text.strip() or "（当前触发消息无可提取文本）"
    return (
        "当前触发消息：\n"
        f"{current}\n\n"
        "话题上下文（按时间顺序，已排除机器人历史回复；历史图片/文件仅作占位）：\n"
        f"{thread_context}"
    )


def _apply_char_cap(lines: list[str], max_chars: int) -> tuple[str, bool]:
    if not lines:
        return "", False
    if max_chars <= 0:
        return "", True
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text, False
    if len(lines) == 1:
        return _truncate_text(lines[0], max_chars), True

    root_line = lines[0]
    tail_lines: list[str] = []
    for line in reversed(lines[1:]):
        candidate_tail = [line, *tail_lines]
        candidate = _join_root_and_tail(root_line, candidate_tail)
        if len(candidate) > max_chars:
            break
        tail_lines = candidate_tail
    if tail_lines:
        return _join_root_and_tail(root_line, tail_lines), True
    return _truncate_text(root_line, max_chars), True


def _join_root_and_tail(root_line: str, tail_lines: list[str]) -> str:
    return "\n[中间话题上下文已截断]\n".join([root_line, "\n".join(tail_lines)])


def _truncate_text(value: str, max_chars: int) -> str:
    suffix = "...[已截断]"
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return f"{value[: max_chars - len(suffix)]}{suffix}"


def _sender_label(
    message: _ContextMessage,
    sender_labels: dict[str, str],
    bot_identity: BotIdentity,
) -> str:
    if _is_bot_sender(message.event, bot_identity):
        return "机器人"
    if message.sender_name:
        return message.sender_name
    sender_id = message.event.sender_id
    if sender_id not in sender_labels:
        sender_labels[sender_id] = f"成员{len(sender_labels) + 1}"
    return sender_labels[sender_id]


def _message_content_for_context(event: FeishuMessageEvent) -> str:
    text = event.content.strip()
    placeholders = _media_placeholders(event)
    parts = [text] if text else []
    parts.extend(placeholders)
    if parts:
        return " ".join(parts)
    if event.message_type:
        return f"[{event.message_type}消息]"
    return ""


def _media_placeholders(event: FeishuMessageEvent) -> list[str]:
    content = _content_object(event.raw_content)
    placeholders: list[str] = []
    if event.message_type in MEDIA_LABELS:
        placeholders.append(_media_placeholder(event.message_type, content))
    placeholders.extend(_rich_content_placeholders(content))
    return _dedupe_strings(placeholders)


def _rich_content_placeholders(content: dict[str, Any]) -> list[str]:
    blocks = content.get("content")
    if not isinstance(blocks, list):
        return []
    placeholders: list[str] = []
    for block in blocks:
        if not isinstance(block, list):
            continue
        for item in block:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").lower()
            if tag in {"img", "image"}:
                placeholders.append("[图片]")
            elif tag in MEDIA_LABELS:
                placeholders.append(_media_placeholder(tag, item))
    return placeholders


def _media_placeholder(kind: str, content: dict[str, Any]) -> str:
    label = MEDIA_LABELS.get(kind, "媒体")
    filename = str(content.get("file_name") or content.get("name") or "").strip()
    if filename:
        return f"[{label}：{filename[:80]}]"
    return f"[{label}]"


def _dedupe_strings(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _content_object(raw_content: object) -> dict[str, Any]:
    if isinstance(raw_content, dict):
        return raw_content
    if isinstance(raw_content, str):
        try:
            payload = json.loads(raw_content)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _sender_name_from_payload(payload: dict[str, Any]) -> str:
    sender = _sender_payload(payload)
    name = sender.get("name") or sender.get("sender_name")
    return str(name or "").strip()


def _sender_payload(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if isinstance(event.get("sender"), dict):
        return event["sender"]
    sender = payload.get("sender")
    return sender if isinstance(sender, dict) else {}


def _is_bot_sender(event: FeishuMessageEvent, bot_identity: BotIdentity) -> bool:
    if event.sender_type in {"app", "bot"}:
        return True
    if bot_identity.app_id and event.sender_id == bot_identity.app_id:
        return True
    if bot_identity.open_id and event.sender_id == bot_identity.open_id:
        return True
    return False


async def fetch_recent_chat_messages(
    settings: Settings,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    chat_ids = _split_csv_ordered(settings.feishu_support_group_chat_id)
    if not chat_ids:
        return []
    token = await get_tenant_access_token(settings)
    end_time = int(now or time.time()) + 5
    start_time = max(0, end_time - max(1, settings.feishu_backfill_lookback_seconds))
    page_size = min(max(1, settings.feishu_backfill_page_size), 50)
    messages: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for chat_id in chat_ids:
            try:
                messages.extend(
                    await _fetch_recent_chat_messages_for_chat(
                        client,
                        token,
                        chat_id,
                        start_time=start_time,
                        end_time=end_time,
                        page_size=page_size,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Feishu message backfill failed for one chat; continuing. chat_id_hash=%s error_type=%s error=%s",
                    _short_hash(chat_id),
                    type(exc).__name__,
                    redact_internal_references(exc, max_chars=300),
                )
    return sorted(messages, key=_message_sort_key)


async def _fetch_recent_chat_messages_for_chat(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    *,
    start_time: int,
    end_time: int,
    page_size: int,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    page_token = ""
    for _ in range(3):
        params: dict[str, object] = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": start_time,
            "end_time": end_time,
            "page_size": page_size,
            "sort_type": "ByCreateTimeAsc",
            "only_thread_root_messages": True,
            "card_msg_content_type": "raw_card_content",
        }
        if page_token:
            params["page_token"] = page_token
        response = await client.get(
            f"{FEISHU_BASE_URL}/im/v1/messages",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = _feishu_json_payload(response, "List Feishu chat messages")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for item in data.get("items") or []:
            if isinstance(item, dict):
                messages.append(item)
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            break
    return messages


def _split_csv_ordered(value: str) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in value.split(","):
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def _message_sort_key(message: dict[str, Any]) -> tuple[int, str]:
    raw_time = str(message.get("create_time") or message.get("create_time_ms") or "")
    if raw_time.isdigit():
        return int(raw_time), str(message.get("message_id") or message.get("id") or "")
    return 0, str(message.get("message_id") or message.get("id") or "")


def _context_message_sort_key(message: _ContextMessage) -> tuple[int, float, int, str]:
    timestamp = parse_event_timestamp(message.event)
    if timestamp is None:
        return 1, 0.0, message.source_index, message.event.message_id
    return 0, timestamp, message.source_index, message.event.message_id


def _set_future_result(result: Future[int], value: int) -> None:
    if not result.done():
        result.set_result(value)


def _set_future_exception(result: Future[int], exc: Exception) -> None:
    if not result.done():
        result.set_exception(exc)
