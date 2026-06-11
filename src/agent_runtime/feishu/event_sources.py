from __future__ import annotations

import asyncio
import json
import logging
import threading
from abc import ABC, abstractmethod
from concurrent.futures import Future
from typing import Any, Awaitable, Callable

import httpx

from agent_runtime.settings import Settings


PayloadHandler = Callable[[dict[str, Any]], Awaitable[None]]

EVENT_KEY = "im.message.receive_v1"

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
                    logger.info(
                        "Received Feishu SDK event: event_type=%s event_id=%s.",
                        _payload_event_type(payload),
                        _payload_event_id(payload),
                    )
                    future = asyncio.run_coroutine_threadsafe(handler(payload), parent_loop)
                    future.add_done_callback(_log_handler_failure)
                except Exception:
                    logger.exception("Failed to dispatch Feishu SDK event.")

            def on_ignored_event(data: Any) -> None:
                payload = payload_from_lark_oapi_event(data)
                logger.info(
                    "Ignored Feishu SDK event type: event_type=%s event_id=%s.",
                    _payload_event_type(payload),
                    _payload_event_id(payload),
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


def _set_future_result(result: Future[int], value: int) -> None:
    if not result.done():
        result.set_result(value)


def _set_future_exception(result: Future[int], exc: Exception) -> None:
    if not result.done():
        result.set_exception(exc)
