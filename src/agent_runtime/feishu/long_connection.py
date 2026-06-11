from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from agent_runtime.feishu.admission import BotIdentity
from agent_runtime.feishu.bridge import event_from_payload, process_message_event
from agent_runtime.feishu.event_sources import (
    LarkOapiEventSource,
    fetch_bot_open_id,
)
from agent_runtime.settings import Settings, get_settings


logger = logging.getLogger(__name__)


async def handle_payload(
    payload: dict[str, Any],
    settings: Settings | None = None,
    semaphore: asyncio.Semaphore | None = None,
    bot_identity: BotIdentity | None = None,
) -> str:
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
    try:
        logger.info("Feishu SDK WebSocket event source selected.")
        logger.info("Feishu event concurrency limit: %s.", event_concurrency)
        if bot_open_id:
            logger.info("Feishu bot open_id detected.")

        async def schedule_payload(payload: dict[str, Any]) -> None:
            _track_processing_task(tasks, payload, settings, semaphore, bot_identity)

        return await event_source.run(schedule_payload)
    finally:
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=30)
            for task in pending:
                task.cancel()


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
