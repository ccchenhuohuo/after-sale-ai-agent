from __future__ import annotations

import asyncio
import hashlib
import logging
import signal
from dataclasses import dataclass
from typing import Any

from agents import custom_span

from agent_runtime.feishu.admission import BotIdentity, should_accept, split_csv
from agent_runtime.feishu.bridge import event_from_payload, process_message_event
from agent_runtime.feishu.event_sources import (
    LarkOapiEventSource,
    fetch_bot_open_id,
    fetch_recent_chat_messages,
)
from agent_runtime.feishu.events import FeishuMessageEvent, effective_thread_id
from agent_runtime.settings import Settings, get_settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PayloadAdmission:
    accepted: bool
    status: str
    event: FeishuMessageEvent | None = None


@dataclass
class BackfillStats:
    fetched_count: int = 0
    scheduled_count: int = 0
    skipped_app_or_bot: int = 0
    skipped_non_text: int = 0
    skipped_no_trigger: int = 0
    skipped_expired: int = 0

    def record(self, status: str) -> None:
        if status == "accepted":
            self.scheduled_count += 1
        elif status == "skipped_app_or_bot":
            self.skipped_app_or_bot += 1
        elif status == "skipped_non_text":
            self.skipped_non_text += 1
        elif status == "skipped_expired":
            self.skipped_expired += 1
        else:
            self.skipped_no_trigger += 1

    def trace_attributes(self) -> dict[str, int]:
        return {
            "fetched_count": self.fetched_count,
            "scheduled_count": self.scheduled_count,
            "skipped_app_or_bot": self.skipped_app_or_bot,
            "skipped_non_text": self.skipped_non_text,
            "skipped_no_trigger": self.skipped_no_trigger,
            "skipped_expired": self.skipped_expired,
        }


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


async def _schedule_websocket_payload(
    tasks: set[asyncio.Task[None]],
    payload: dict[str, Any],
    settings: Settings,
    semaphore: asyncio.Semaphore,
    bot_identity: BotIdentity,
) -> str:
    admission = _websocket_payload_admission(payload, bot_identity)
    if not admission.accepted:
        event = admission.event
        logger.info(
            "Skipped Feishu SDK event before queue: status=%s message_id_hash=%s chat_id_hash=%s thread_id_hash=%s sender_type=%s",
            admission.status,
            _short_hash(event.message_id if event else ""),
            _short_hash(event.chat_id if event else ""),
            _short_hash(effective_thread_id(event) if event else ""),
            event.sender_type if event else "",
        )
        return admission.status
    _track_processing_task(tasks, payload, settings, semaphore, bot_identity)
    return "scheduled"


def _websocket_payload_admission(payload: dict[str, Any], bot_identity: BotIdentity) -> PayloadAdmission:
    event = event_from_payload(payload)
    if event is None or not event.message_id or not event.chat_id:
        return PayloadAdmission(False, "ignored", event)
    if _is_self_echo_or_bot_sender(event, bot_identity):
        return PayloadAdmission(False, "skipped_app_or_bot", event)
    return PayloadAdmission(True, "accepted", event)


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
    stats = BackfillStats(fetched_count=len(payloads))
    for payload in payloads:
        admission = _backfill_payload_admission(payload, settings, bot_identity)
        stats.record(admission.status)
        if not admission.accepted:
            continue
        _track_processing_task(tasks, payload, settings, semaphore, bot_identity)
    with custom_span("backfill_filter", stats.trace_attributes()):
        pass
    if payloads:
        logger.info(
            "Feishu message backfill fetched=%s scheduled=%s skipped_app_or_bot=%s skipped_non_text=%s skipped_no_trigger=%s skipped_expired=%s.",
            stats.fetched_count,
            stats.scheduled_count,
            stats.skipped_app_or_bot,
            stats.skipped_non_text,
            stats.skipped_no_trigger,
            stats.skipped_expired,
        )
    return stats.scheduled_count


def _backfill_payload_admission(
    payload: dict[str, Any],
    settings: Settings,
    bot_identity: BotIdentity,
) -> PayloadAdmission:
    event = event_from_payload(payload)
    if event is None or not event.message_id or not event.chat_id:
        return PayloadAdmission(False, "skipped_no_trigger", event)
    if event.sender_type != "user" or _is_self_echo_or_bot_sender(event, bot_identity):
        return PayloadAdmission(False, "skipped_app_or_bot", event)
    if event.message_type != "text":
        return PayloadAdmission(False, "skipped_non_text", event)
    allowed_chat_ids = split_csv(settings.feishu_support_group_chat_id)
    if allowed_chat_ids and event.chat_id not in allowed_chat_ids:
        return PayloadAdmission(False, "skipped_no_trigger", event)

    gate = should_accept(event, settings, bot_identity)
    if gate.accepted:
        return PayloadAdmission(True, "accepted", event)
    if gate.status == "expired":
        return PayloadAdmission(False, "skipped_expired", event)
    if gate.status in {"ignored_bot_sender", "suppressed_bot_loop"}:
        return PayloadAdmission(False, "skipped_app_or_bot", event)
    return PayloadAdmission(False, "skipped_no_trigger", event)


def _is_self_echo_or_bot_sender(event: FeishuMessageEvent, bot_identity: BotIdentity) -> bool:
    if event.sender_type in {"app", "bot"}:
        return True
    if bot_identity.app_id and event.sender_id == bot_identity.app_id:
        return True
    if bot_identity.open_id and event.sender_id == bot_identity.open_id:
        return True
    return False


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
            await _schedule_websocket_payload(tasks, payload, settings, semaphore, bot_identity)

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
    configure_runtime_logging()
    raise SystemExit(asyncio.run(_amain()))


def configure_runtime_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


if __name__ == "__main__":
    main()
