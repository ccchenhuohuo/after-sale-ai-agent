from __future__ import annotations

import asyncio
import hashlib
import logging
import signal
from typing import Any

from agents import custom_span

from agent_runtime.feishu.admission import BotIdentity
from agent_runtime.feishu.bridge import event_from_payload, process_message_event
from agent_runtime.feishu.event_sources import (
    LarkOapiEventSource,
    fetch_bot_open_id,
    fetch_recent_chat_messages,
)
from agent_runtime.settings import Settings, get_settings


logger = logging.getLogger(__name__)


async def handle_payload(
    payload: dict[str, Any],
    settings: Settings | None = None,
    semaphore: asyncio.Semaphore | None = None,
    bot_identity: BotIdentity | None = None,
) -> str:
    with custom_span("parse_event", {"event_id_hash": _payload_event_id_hash(payload)}):
        event = event_from_payload(payload)
    if event is None:
        return "ignored"
    return await process_message_event(
        event,
        settings or get_settings(),
        semaphore=semaphore,
        bot_identity=bot_identity,
    )


async def _process_payload(
    payload: dict[str, Any],
    settings: Settings,
    semaphore: asyncio.Semaphore,
    bot_identity: BotIdentity,
) -> None:
    try:
        status = await handle_payload(payload, settings, semaphore=semaphore, bot_identity=bot_identity)
    except Exception:
        logger.exception("Failed to process Feishu event.")
        return
    logger.info("Processed Feishu event: %s", status)


def _payload_event_id_hash(payload: dict[str, Any]) -> str:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_id = str(header.get("event_id") or payload.get("event_id") or "")
    return hashlib.sha1(event_id.encode("utf-8")).hexdigest()[:12] if event_id else ""


def _track_processing_task(
    tasks: set[asyncio.Task[None]],
    payload: dict[str, Any],
    settings: Settings,
    semaphore: asyncio.Semaphore,
    bot_identity: BotIdentity,
) -> None:
    task = asyncio.create_task(_process_payload(payload, settings, semaphore, bot_identity))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _backfill_loop(
    tasks: set[asyncio.Task[None]],
    settings: Settings,
    semaphore: asyncio.Semaphore,
    bot_identity: BotIdentity,
) -> None:
    interval_seconds = max(1.0, settings.feishu_backfill_interval_seconds)
    while True:
        try:
            await _backfill_once(tasks, settings, semaphore, bot_identity)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Feishu message backfill poll failed.", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def _backfill_once(
    tasks: set[asyncio.Task[None]],
    settings: Settings,
    semaphore: asyncio.Semaphore,
    bot_identity: BotIdentity,
) -> int:
    with custom_span(
        "backfill_poll",
        {
            "chat_id_hash": _short_hash(settings.feishu_support_group_chat_id),
            "lookback_seconds": settings.feishu_backfill_lookback_seconds,
            "page_size": settings.feishu_backfill_page_size,
        },
    ):
        payloads = await fetch_recent_chat_messages(settings)
    for payload in payloads:
        _track_processing_task(tasks, payload, settings, semaphore, bot_identity)
    if payloads:
        logger.info("Feishu message backfill scheduled %s recent payload(s).", len(payloads))
    return len(payloads)


async def consume_long_connection(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    event_concurrency = max(1, settings.feishu_event_concurrency)
    semaphore = asyncio.Semaphore(event_concurrency)
    tasks: set[asyncio.Task[None]] = set()
    bot_open_id = await fetch_bot_open_id(settings)
    bot_identity = BotIdentity(
        app_id=settings.feishu_app_id,
        open_id=bot_open_id,
        names=(settings.feishu_bot_mention_name,) if settings.feishu_bot_mention_name else (),
    )
    event_source = LarkOapiEventSource(settings)
    backfill_task: asyncio.Task[None] | None = None
    try:
        logger.info("Feishu SDK WebSocket event source selected.")
        logger.info("Feishu event concurrency limit: %s.", event_concurrency)
        if bot_open_id:
            logger.info("Feishu bot open_id detected.")
        if settings.feishu_backfill_enabled:
            logger.info(
                "Feishu message backfill enabled: interval=%ss lookback=%ss.",
                settings.feishu_backfill_interval_seconds,
                settings.feishu_backfill_lookback_seconds,
            )
            backfill_task = asyncio.create_task(_backfill_loop(tasks, settings, semaphore, bot_identity))

        async def schedule_payload(payload: dict[str, Any]) -> None:
            _track_processing_task(tasks, payload, settings, semaphore, bot_identity)

        return await event_source.run(schedule_payload)
    finally:
        if backfill_task is not None:
            backfill_task.cancel()
            try:
                await backfill_task
            except asyncio.CancelledError:
                pass
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=30)
            for task in pending:
                task.cancel()


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12] if value else ""


async def _amain() -> int:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    task = asyncio.create_task(consume_long_connection())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for pending_task in pending:
        pending_task.cancel()
    if stop_task in done:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return 0
    return task.result()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
