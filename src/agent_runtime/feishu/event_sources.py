from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import Future
from typing import Any, Awaitable, Callable

import httpx
from agents import custom_span

from agent_runtime.feishu.message_sender import FEISHU_BASE_URL, get_tenant_access_token
from agent_runtime.settings import Settings


PayloadHandler = Callable[[dict[str, Any]], Awaitable[None]]

EVENT_KEY = "im.message.receive_v1"
NOOP_EVENT_TYPES = {
    "im.message.reaction.created_v1",
    "im.message.reaction.deleted_v1",
}

logger = logging.getLogger(__name__)


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


async def fetch_recent_chat_messages(
    settings: Settings,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    if not settings.feishu_support_group_chat_id:
        return []
    token = await get_tenant_access_token(settings)
    end_time = int(now or time.time()) + 5
    start_time = max(0, end_time - max(1, settings.feishu_backfill_lookback_seconds))
    page_size = min(max(1, settings.feishu_backfill_page_size), 100)
    messages: list[dict[str, Any]] = []
    page_token = ""
    async with httpx.AsyncClient(timeout=15) as client:
        for _ in range(3):
            params: dict[str, object] = {
                "container_id_type": "chat",
                "container_id": settings.feishu_support_group_chat_id,
                "start_time": start_time,
                "end_time": end_time,
                "page_size": page_size,
            }
            if page_token:
                params["page_token"] = page_token
            response = await client.get(
                f"{FEISHU_BASE_URL}/im/v1/messages",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
            code = int(payload.get("code", 0) or 0)
            if code != 0:
                raise RuntimeError(f"Failed to list Feishu messages: code={code} msg={payload.get('msg')}")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            for item in data.get("items") or []:
                if isinstance(item, dict):
                    messages.append(item)
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                break
    return sorted(messages, key=_message_sort_key)


def _message_sort_key(message: dict[str, Any]) -> tuple[int, str]:
    raw_time = str(message.get("create_time") or message.get("create_time_ms") or "")
    if raw_time.isdigit():
        return int(raw_time), str(message.get("message_id") or message.get("id") or "")
    return 0, str(message.get("message_id") or message.get("id") or "")


def _set_future_result(result: Future[int], value: int) -> None:
    if not result.done():
        result.set_result(value)


def _set_future_exception(result: Future[int], exc: Exception) -> None:
    if not result.done():
        result.set_exception(exc)
