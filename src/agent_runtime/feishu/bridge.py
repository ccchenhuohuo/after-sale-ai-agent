from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from agents import Runner, SQLiteSession, custom_span, flush_traces, trace
from agents.exceptions import OutputGuardrailTripwireTriggered
from agents.memory import SessionSettings

from agent_runtime.copilot.answer_contract import (
    FEISHU_VISIBLE_REPLY_FALLBACK,
    render_feishu_reply,
    render_support_answer,
    validate_answer_contract,
    validate_feishu_visible_reply,
)
from agent_runtime.copilot.evidence import evidence_pack_trace_attributes, short_hash
from agent_runtime.copilot.evidence_collection import collect_support_evidence
from agent_runtime.copilot.prompts import build_agent_input
from agent_runtime.copilot.support_copilot import build_support_copilot
from agent_runtime.feishu.admission import BotIdentity, should_accept
from agent_runtime.feishu.events import (
    FeishuMessageEvent,
    effective_thread_id,
    event_from_payload,
    queue_key_for_event,
    session_id_for_event,
)
from agent_runtime.feishu.parser import strip_trigger_prefix
from agent_runtime.feishu.queues import PerThreadQueue
from agent_runtime.feishu.responder import FeishuSdkResponder, ReplyResult
from agent_runtime.feishu.runtime_store import RuntimeStore
from agent_runtime.llm import build_run_config, configure_agents_runtime
from agent_runtime.settings import Settings, get_settings
from agent_runtime.tools.history_rag import history_rag_index_available
from agent_runtime.tools.media_rag import media_rag_index_available


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
    return {
        "entrypoint": "feishu",
        "chat_id_hash": _hash_id(event.chat_id),
        "thread_id_hash": _hash_id(thread_id),
        "message_id_hash": _hash_id(event.message_id),
        "event_id_hash": _hash_id(event.event_id),
        "queue_key_hash": _hash_id(queue_key_for_event(event)),
    }


def should_handle_event(event: FeishuMessageEvent, settings: Settings) -> bool:
    return should_accept(event, settings, bot_identity_for_settings(settings)).accepted


def build_feishu_user_input(event: FeishuMessageEvent, settings: Settings) -> str:
    text = strip_trigger_prefix(event.content, settings.support_agent_trigger_prefix)
    if settings.feishu_bot_mention_name:
        text = text.replace(f"@{settings.feishu_bot_mention_name}", "").replace(settings.feishu_bot_mention_name, "")
    return text.strip() or event.content.strip()


async def run_support_agent_for_event(event: FeishuMessageEvent, settings: Settings | None = None) -> str:
    settings = configure_agents_runtime(settings or get_settings())
    agent = build_support_copilot(settings.support_agent_model)
    session = SQLiteSession(
        session_id_for_event(event),
        db_path=settings.support_agent_session_db_path,
        session_settings=SessionSettings(limit=settings.support_agent_session_limit),
    )
    user_input = build_feishu_user_input(event, settings)
    thread_id = effective_thread_id(event)
    with custom_span(
        "support_turn",
        {
            "entrypoint": "feishu",
            "loop_version": "v2",
            "raw_issue_hash": short_hash(user_input),
            "chat_id_hash": hashlib.sha1(event.chat_id.encode("utf-8")).hexdigest()[:12],
            "thread_id_hash": hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:12],
            "message_id_hash": hashlib.sha1(event.message_id.encode("utf-8")).hexdigest()[:12],
        },
    ):
        evidence_pack = await collect_support_evidence(user_input, settings)
        try:
            result = await Runner.run(
                agent,
                build_agent_input(user_input, source="飞书客服话题群", evidence_pack=evidence_pack),
                context=evidence_pack,
                session=session,
                run_config=build_run_config(
                    settings,
                    group_id=f"feishu:{_hash_id(event.chat_id)}:thread:{_hash_id(thread_id)}",
                    metadata={
                        "source": "feishu-bot",
                        "entrypoint": "feishu",
                        "loop_version": "v2",
                        "chat_id_hash": hashlib.sha1(event.chat_id.encode("utf-8")).hexdigest()[:12],
                        "thread_id_hash": hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:12],
                        "message_id_hash": hashlib.sha1(event.message_id.encode("utf-8")).hexdigest()[:12],
                        "history_index_available": history_rag_index_available(settings),
                        "media_index_available": media_rag_index_available(settings),
                    },
                ),
            )
        finally:
            flush_traces()
        output = render_support_answer(result.final_output)
        with custom_span(
            "answer_contract_check",
            {
                **evidence_pack_trace_attributes(evidence_pack),
                "entrypoint": "feishu",
            },
        ):
            issues = validate_answer_contract(output, history_connected=history_rag_index_available(settings))
        visible_output = render_feishu_reply(result.final_output)
        visible_issues = validate_feishu_visible_reply(visible_output)
        with custom_span(
            "feishu_visible_reply_check",
            {
                "entrypoint": "feishu",
                "internal_issue_codes": [issue.code for issue in issues],
                "visible_issue_codes": [issue.code for issue in visible_issues],
            },
        ):
            pass
        flush_traces()
    if issues or visible_issues:
        logger.warning(
            "Feishu visible reply blocked by validation: internal_issue_codes=%s visible_issue_codes=%s message_id_hash=%s",
            [issue.code for issue in issues],
            [issue.code for issue in visible_issues],
            _hash_id(event.message_id),
        )
        return FEISHU_VISIBLE_REPLY_FALLBACK
    return visible_output


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
    with trace(
        settings.support_agent_trace_workflow_name,
        group_id=_trace_group_id(event),
        metadata={**trace_data, "source": "feishu-bridge", "loop_version": "v2"},
        disabled=settings.support_agent_tracing_disabled,
    ):
        with custom_span("feishu_event", {**trace_data, "status": "received"}):
            with custom_span("admission_gate", trace_data):
                gate = should_accept(event, settings, bot_identity or bot_identity_for_settings(settings), runtime_store)
            if not gate.accepted:
                with custom_span("feishu_event_status", {**trace_data, "status": gate.status}):
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

            with custom_span("dedup", trace_data):
                claim = runtime_store.claim_event(event)
            if not claim.should_process:
                with custom_span("feishu_event_status", {**trace_data, "status": claim.status}):
                    return claim.status

            queue_key = queue_key_for_event(event)
            logger.info(
                "Queued Feishu event: message_id_hash=%s queue_key_hash=%s claim_status=%s",
                _hash_id(event.message_id),
                _hash_id(queue_key),
                claim.status,
            )
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
        with custom_span("reply_in_thread", trace_data):
            reply_result = await reply_in_thread(event.message_id, answer, settings)
    except Exception as exc:
        runtime_store.record_reply(event, "reply_failed", error=str(exc))
        runtime_store.record_event_error("reply", event, str(exc))
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
        "reply_in_thread_result",
        {
            **trace_data,
            "reply_status": "replied",
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
