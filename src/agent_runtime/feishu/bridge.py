from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from agents import custom_span, flush_traces
from agents.exceptions import OutputGuardrailTripwireTriggered

from agent_runtime.copilot.answer_contract import FEISHU_VISIBLE_REPLY_FALLBACK
from agent_runtime.copilot.runtime import build_support_runtime_session, run_support_case_request
from agent_runtime.channels.feishu_reply import render_feishu_visible_runtime_reply
from agent_runtime.feishu.adapter import build_feishu_user_text, build_support_case_request_from_event
from agent_runtime.feishu.admission import BotIdentity, should_accept
from agent_runtime.feishu.assets import download_feishu_assets_for_request
from agent_runtime.feishu.events import (
    FeishuMessageEvent,
    effective_thread_id,
    event_from_payload,
    queue_key_for_event,
    session_id_for_event,
)
from agent_runtime.feishu.queues import PerThreadQueue
from agent_runtime.feishu.responder import FeishuSdkResponder, ReplyResult
from agent_runtime.feishu.runtime_store import RuntimeStore
from agent_runtime.llm import configure_agents_runtime
from agent_runtime.observability.tracing import (
    admission_trace,
    base_trace_attrs,
    runtime_trace,
    should_trace_admission,
)
from agent_runtime.settings import Settings, get_settings


logger = logging.getLogger(__name__)

_RUNTIME_STORES: dict[tuple[str, int, int], RuntimeStore] = {}
_QUEUE = PerThreadQueue(max_items=1000)


def clear_runtime_state_for_tests() -> None:
    _RUNTIME_STORES.clear()
    _QUEUE.clear()


def _runtime_store_for_settings(settings: Settings) -> RuntimeStore:
    key = (
        settings.feishu_runtime_db_path,
        settings.feishu_dedup_ttl_seconds,
        settings.feishu_dedup_max_items,
    )
    store = _RUNTIME_STORES.get(key)
    if store is None:
        store = RuntimeStore(*key)
        _RUNTIME_STORES[key] = store
    return store


def bot_identity_for_settings(settings: Settings) -> BotIdentity:
    names = (settings.feishu_bot_mention_name,) if settings.feishu_bot_mention_name else ()
    return BotIdentity(app_id=settings.feishu_app_id, open_id=settings.feishu_bot_open_id, names=names)


def _hash_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12] if value else ""


def _trace_group_id(event: FeishuMessageEvent) -> str:
    return f"feishu:{_hash_id(event.chat_id)}:thread:{_hash_id(effective_thread_id(event))}"


def _event_trace_data(event: FeishuMessageEvent) -> dict[str, object]:
    thread_id = effective_thread_id(event)
    session_id = session_id_for_event(event)
    return {
        "entrypoint": "feishu",
        "content_chars": len(event.content or ""),
        "chat_id_hash": _hash_id(event.chat_id),
        "thread_id_hash": _hash_id(thread_id),
        "message_id_hash": _hash_id(event.message_id),
        "event_id_hash": _hash_id(event.event_id),
        "session_id_hash": _hash_id(session_id),
        "queue_key_hash": _hash_id(queue_key_for_event(event)),
        "event_source": event.event_source,
    }


def should_handle_event(event: FeishuMessageEvent, settings: Settings) -> bool:
    return should_accept(event, settings, bot_identity_for_settings(settings)).accepted


def build_feishu_user_input(event: FeishuMessageEvent, settings: Settings) -> str:
    return build_feishu_user_text(event, settings)


async def run_support_agent_for_event(event: FeishuMessageEvent, settings: Settings | None = None) -> str:
    settings = configure_agents_runtime(settings or get_settings())
    request = build_support_case_request_from_event(event, settings)
    request = await download_feishu_assets_for_request(request, settings)
    thread_id = effective_thread_id(event)
    session = build_support_runtime_session(settings, session_id_for_event(event))
    runtime_result = await run_support_case_request(
        request,
        settings,
        entrypoint="feishu",
        source_label="飞书客服话题群",
        session=session,
        run_config_group_id=f"feishu:{_hash_id(event.chat_id)}:thread:{_hash_id(thread_id)}",
        run_config_metadata={
            "source": "feishu-bot",
            "chat_id_hash": _hash_id(event.chat_id),
            "thread_id_hash": _hash_id(thread_id),
            "message_id_hash": _hash_id(event.message_id),
            "sender_id_hash": _hash_id(event.sender_id),
            "session_id_hash": _hash_id(session_id_for_event(event)),
            "input_chars": len(event.content or ""),
        },
    )
    visible_reply = render_feishu_visible_runtime_reply(runtime_result)
    if visible_reply.blocked:
        logger.warning(
            "Feishu visible reply blocked by validation: issue_codes=%s message_id_hash=%s",
            [issue.code for issue in visible_reply.issues],
            _hash_id(event.message_id),
        )
        return FEISHU_VISIBLE_REPLY_FALLBACK
    return visible_reply.text


async def reply_in_thread(message_id: str, text: str, settings: Settings | None = None) -> ReplyResult:
    settings = settings or get_settings()
    idempotency_key = _reply_idempotency_key(message_id, text)
    return await FeishuSdkResponder(settings).reply_in_thread(message_id, text, idempotency_key)


async def process_message_event(
    event: FeishuMessageEvent,
    settings: Settings | None = None,
    semaphore: asyncio.Semaphore | None = None,
    runtime_store: RuntimeStore | None = None,
    bot_identity: BotIdentity | None = None,
) -> str:
    settings = settings or get_settings()
    runtime_store = runtime_store or _runtime_store_for_settings(settings)
    trace_data = _event_trace_data(event)
    gate = should_accept(event, settings, bot_identity or bot_identity_for_settings(settings), runtime_store)
    if not gate.accepted:
        _maybe_trace_admission(settings, event, trace_data, gate.status, dedup_status="")
        logger.info(
            "Ignored Feishu event: status=%s message_id_hash=%s chat_id_hash=%s thread_id_hash=%s root_id_hash=%s sender_type=%s",
            gate.status,
            _hash_id(event.message_id),
            _hash_id(event.chat_id),
            _hash_id(event.thread_id),
            _hash_id(event.root_id),
            event.sender_type,
        )
        return gate.status

    claim = runtime_store.claim_event(event)
    if not claim.should_process:
        _maybe_trace_admission(settings, event, trace_data, claim.status, dedup_status=claim.status)
        return claim.status

    queue_key = queue_key_for_event(event)
    logger.info(
        "Queued Feishu event: message_id_hash=%s queue_key_hash=%s claim_status=%s",
        _hash_id(event.message_id),
        _hash_id(queue_key),
        claim.status,
    )
    group_id = _trace_group_id(event)
    try:
        with runtime_trace(
            settings,
            entrypoint="feishu",
            group_id=group_id,
            attrs={
                **_root_trace_attrs(event, trace_kind="runtime", event_status="processing"),
                **trace_data,
                "source": "feishu-bridge",
                "loop_version": "v2",
            },
        ):
            with custom_span("feishu_event", {**trace_data, "status": "accepted"}):
                pass
            with custom_span("admission_gate", {**trace_data, "status": gate.status}):
                pass
            with custom_span("dedup", {**trace_data, "status": claim.status}):
                pass
            lock = _QUEUE.lock_for_event(event)
            wait_started_at = time.perf_counter()
            with custom_span("queue_wait", {**trace_data, "claim_status": claim.status}):
                await lock.acquire()
            queue_wait_ms = round((time.perf_counter() - wait_started_at) * 1000, 2)
            try:
                logger.info(
                    "Processing Feishu event: message_id_hash=%s queue_key_hash=%s queue_wait_ms=%s",
                    _hash_id(event.message_id),
                    _hash_id(queue_key),
                    queue_wait_ms,
                )
                with custom_span("queue_processing", {**trace_data, "queue_wait_ms": queue_wait_ms}):
                    if semaphore is None:
                        status = await _process_message_event_unlocked(event, settings, runtime_store, trace_data)
                    else:
                        async with semaphore:
                            status = await _process_message_event_unlocked(event, settings, runtime_store, trace_data)
                with custom_span("feishu_event_status", {**trace_data, "status": status}):
                    return status
            finally:
                lock.release()
    finally:
        flush_traces()


def _root_trace_attrs(event: FeishuMessageEvent, *, trace_kind: str, event_status: str) -> dict[str, object]:
    thread_id = effective_thread_id(event)
    return base_trace_attrs(
        trace_kind=trace_kind,
        entrypoint="feishu",
        event_source=event.event_source,
        event_status=event_status,
        session_id=session_id_for_event(event),
        chat_id=event.chat_id,
        thread_id=thread_id,
        message_id=event.message_id,
        extra={
            "trace_group_id": _trace_group_id(event),
            "input_chars": len(event.content or ""),
        },
    )


def _maybe_trace_admission(
    settings: Settings,
    event: FeishuMessageEvent,
    trace_data: dict[str, object],
    status: str,
    *,
    dedup_status: str,
) -> None:
    if not should_trace_admission(settings, status=status, stable_key=event.message_id or event.event_id):
        return
    with admission_trace(
        settings,
        {
            **_root_trace_attrs(event, trace_kind="admission", event_status=status),
            **trace_data,
            "source": "feishu-bridge",
            "loop_version": "v2",
        },
    ):
        with custom_span("feishu_event", {**trace_data, "status": "received"}):
            pass
        with custom_span("admission_gate", {**trace_data, "status": "accepted" if dedup_status else status}):
            pass
        if dedup_status:
            with custom_span("dedup", {**trace_data, "status": dedup_status}):
                pass
        with custom_span("feishu_event_status", {**trace_data, "status": status}):
            pass


async def _process_message_event_unlocked(
    event: FeishuMessageEvent,
    settings: Settings,
    runtime_store: RuntimeStore,
    trace_data: dict[str, object] | None = None,
) -> str:
    trace_data = trace_data or _event_trace_data(event)
    try:
        with custom_span("agent_run", trace_data):
            answer = await run_support_agent_for_event(event, settings)
    except OutputGuardrailTripwireTriggered as exc:
        runtime_store.record_event_error("agent_guardrail", event, str(exc))
        answer = FEISHU_VISIBLE_REPLY_FALLBACK
    except Exception as exc:
        runtime_store.record_event_error("agent", event, str(exc))
        answer = FEISHU_VISIBLE_REPLY_FALLBACK
    try:
        reply_started_at = time.perf_counter()
        with custom_span("channel_reply", {**trace_data, "reply_target": "thread"}):
            reply_result = await reply_in_thread(event.message_id, answer, settings)
    except Exception as exc:
        runtime_store.record_reply(event, "reply_failed", error=str(exc))
        runtime_store.record_event_error("reply", event, str(exc))
        with custom_span(
            "channel_reply_result",
            {
                **trace_data,
                "reply_status": "reply_failed",
                "reply_target": "thread",
                "error_type": type(exc).__name__,
            },
        ):
            pass
        logger.exception(
            "Failed to reply to Feishu thread; skipping any top-level fallback. message_id_hash=%s thread_id_hash=%s root_id_hash=%s",
            _hash_id(event.message_id),
            _hash_id(event.thread_id),
            _hash_id(event.root_id),
        )
        return "reply_failed"
    reply_message_id = reply_result.reply_message_id if isinstance(reply_result, ReplyResult) else ""
    reply_latency_ms = round((time.perf_counter() - reply_started_at) * 1000, 2)
    with custom_span(
        "channel_reply_result",
        {
            **trace_data,
            "reply_status": "replied",
            "reply_target": "thread",
            "reply_message_id_hash": _hash_id(reply_message_id),
            "reply_latency_ms": reply_latency_ms,
        },
    ):
        pass
    runtime_store.record_reply(event, "replied", reply_message_id=reply_message_id)
    return "replied"


def _reply_idempotency_key(message_id: str, text: str) -> str:
    digest = hashlib.sha1((message_id + text[:32]).encode("utf-8")).hexdigest()[:24]
    return f"support-copilot-{digest}"
