from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import time

from agents import custom_span, flush_traces
from agents.exceptions import OutputGuardrailTripwireTriggered

from agent_runtime.copilot.answer_contract import FEISHU_VISIBLE_REPLY_FALLBACK
from agent_runtime.copilot.case_context import SupportCaseRequest
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
from agent_runtime.feishu.event_sources import build_thread_context_for_event, merge_thread_context_into_request
from agent_runtime.feishu.queues import PerThreadQueue
from agent_runtime.feishu.reactions import WorkingReaction, create_working_reaction, delete_working_reaction
from agent_runtime.feishu.responder import FeishuSdkResponder, ReplyResult, truncate_for_feishu
from agent_runtime.feishu.runtime_store import RuntimeStore
from agent_runtime.llm import configure_agents_runtime
from agent_runtime.observability.tracing import (
    RuntimeTurnHandle,
    admission_trace,
    base_trace_attrs,
    runtime_trace,
    should_trace_admission,
)
from agent_runtime.settings import Settings, get_settings


logger = logging.getLogger(__name__)

_RUNTIME_STORES: dict[tuple[str, int, int, int], RuntimeStore] = {}
_QUEUE = PerThreadQueue(max_items=1000)


@dataclass
class _WorkingReactionGuard:
    reaction: WorkingReaction | None

    async def clear(self, settings: Settings) -> None:
        reaction = self.reaction
        if reaction is None:
            return
        self.reaction = None
        await delete_working_reaction(reaction, settings)


def clear_runtime_state_for_tests() -> None:
    _RUNTIME_STORES.clear()
    _QUEUE.clear()


def _runtime_store_for_settings(settings: Settings) -> RuntimeStore:
    key = (
        settings.feishu_runtime_db_path,
        settings.feishu_dedup_ttl_seconds,
        settings.feishu_dedup_max_items,
        settings.feishu_processing_stale_seconds,
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
    request = await _attach_thread_context_to_request(request, event, settings)
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
            "input_chars": len(request.user_text or ""),
        },
    )
    visible_reply = render_feishu_visible_runtime_reply(runtime_result)
    reply_text = visible_reply.safe_text
    if visible_reply.blocked:
        logger.warning(
            "Feishu visible reply blocked by validation: issue_codes=%s message_id_hash=%s",
            [issue.code for issue in visible_reply.issues],
            _hash_id(event.message_id),
        )
    return append_human_review_mention(reply_text, runtime_result, settings)


async def _attach_thread_context_to_request(
    request: SupportCaseRequest,
    event: FeishuMessageEvent,
    settings: Settings,
) -> SupportCaseRequest:
    if not settings.feishu_thread_context_enabled:
        return request
    try:
        context = await build_thread_context_for_event(event, settings, bot_identity_for_settings(settings))
    except Exception as exc:
        logger.warning(
            "Failed to fetch Feishu thread context; using current message only. "
            "message_id_hash=%s thread_id_hash=%s error_type=%s",
            _hash_id(event.message_id),
            _hash_id(effective_thread_id(event)),
            type(exc).__name__,
            exc_info=True,
        )
        return request
    return merge_thread_context_into_request(request, context)


def append_human_review_mention(text: str, runtime_result: object, settings: Settings) -> str:
    if not settings.feishu_human_review_mention_enabled:
        return text
    if not settings.feishu_human_review_user_open_id:
        return text
    if not _needs_human_review_mention(runtime_result):
        return text

    mention = _feishu_at_tag(settings.feishu_human_review_user_open_id, settings.feishu_human_review_user_name)
    if settings.feishu_human_review_user_open_id in text:
        return text
    mention_line = f"资料不足，麻烦 {mention} 人工复核。"
    reserve_chars = len(mention_line) + 2
    if settings.feishu_reply_max_chars > reserve_chars and len(text) + reserve_chars > settings.feishu_reply_max_chars:
        text = truncate_for_feishu(text, settings.feishu_reply_max_chars - reserve_chars)
    return f"{text}\n\n{mention_line}"


def _needs_human_review_mention(runtime_result: object) -> bool:
    answer = getattr(runtime_result, "answer", None)
    coverage = getattr(runtime_result, "coverage", None)
    recommended_action = getattr(answer, "recommended_action", "") or getattr(coverage, "recommended_action", "")
    mention_enabled = bool(getattr(answer, "mention_enabled", False) or getattr(coverage, "mention_enabled", False))
    return recommended_action == "human_review" and mention_enabled


def _feishu_at_tag(user_open_id: str, name: str) -> str:
    safe_name = (name or user_open_id).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<at user_id="{user_open_id}">{safe_name}</at>'


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
    working_reaction = _WorkingReactionGuard(await create_working_reaction(event.message_id, settings))
    group_id = _trace_group_id(event)
    try:
        with runtime_trace(
            settings,
            entrypoint="feishu",
            group_id=group_id,
            attrs={
                **_root_trace_attrs(event, settings=settings, trace_kind="runtime", event_status="processing"),
                **trace_data,
                "source": "feishu-bridge",
                "loop_version": "v2",
            },
        ) as runtime_turn:
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
                        status = await _process_message_event_unlocked(
                            event, settings, runtime_store, trace_data, runtime_turn, working_reaction
                        )
                    else:
                        async with semaphore:
                            status = await _process_message_event_unlocked(
                                event, settings, runtime_store, trace_data, runtime_turn, working_reaction
                            )
                with custom_span("feishu_event_status", {**trace_data, "status": status}):
                    return status
            finally:
                lock.release()
    finally:
        await working_reaction.clear(settings)
        flush_traces()


def _root_trace_attrs(
    event: FeishuMessageEvent,
    *,
    settings: Settings,
    trace_kind: str,
    event_status: str,
) -> dict[str, object]:
    thread_id = effective_thread_id(event)
    full_io_attrs = {}
    if settings.support_agent_trace_include_sensitive_data:
        input_value = _event_input_for_trace(event)
        full_io_attrs = {
            "input.value": input_value,
            "user.input": event.content or "",
        }
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
            **full_io_attrs,
        },
    )


def _event_input_for_trace(event: FeishuMessageEvent) -> str:
    if event.content:
        return event.content
    if event.raw_content is not None:
        return json.dumps(event.raw_content, ensure_ascii=False, default=str)
    return f"[{event.message_type or 'unknown'} message without extracted text]"


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
            **_root_trace_attrs(event, settings=settings, trace_kind="admission", event_status=status),
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
    runtime_turn: RuntimeTurnHandle | None = None,
    working_reaction: _WorkingReactionGuard | None = None,
) -> str:
    trace_data = trace_data or _event_trace_data(event)
    try:
        with custom_span("agent_run", trace_data):
            answer = await _run_support_agent_with_timeout(event, settings)
    except asyncio.TimeoutError as exc:
        runtime_store.record_event_error("agent_timeout", event, str(exc))
        logger.warning(
            "Feishu agent run timed out; using visible fallback. message_id_hash=%s timeout_seconds=%s",
            _hash_id(event.message_id),
            settings.feishu_agent_run_timeout_seconds,
        )
        answer = FEISHU_VISIBLE_REPLY_FALLBACK
        _set_turn_output(runtime_turn, answer, status="agent_timeout_fallback")
    except OutputGuardrailTripwireTriggered as exc:
        runtime_store.record_event_error("agent_guardrail", event, str(exc))
        answer = FEISHU_VISIBLE_REPLY_FALLBACK
        _set_turn_output(runtime_turn, answer, status="agent_guardrail_fallback")
    except Exception as exc:
        runtime_store.record_event_error("agent", event, str(exc))
        answer = FEISHU_VISIBLE_REPLY_FALLBACK
        _set_turn_output(runtime_turn, answer, status="agent_error_fallback")
    else:
        _set_turn_output(runtime_turn, answer, status="prepared")
    try:
        reply_started_at = time.perf_counter()
        with custom_span("channel_reply", {**trace_data, "reply_target": "thread"}):
            reply_result = await reply_in_thread(event.message_id, answer, settings)
    except Exception as exc:
        if working_reaction is not None:
            await working_reaction.clear(settings)
        _set_turn_output(runtime_turn, answer, status="reply_failed")
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
    if working_reaction is not None:
        await working_reaction.clear(settings)
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
    _set_turn_output(runtime_turn, answer, status="replied")
    runtime_store.record_reply(event, "replied", reply_message_id=reply_message_id)
    return "replied"


async def _run_support_agent_with_timeout(event: FeishuMessageEvent, settings: Settings) -> str:
    timeout = settings.feishu_agent_run_timeout_seconds
    if timeout <= 0:
        return await run_support_agent_for_event(event, settings)
    return await asyncio.wait_for(run_support_agent_for_event(event, settings), timeout=timeout)


def _set_turn_output(runtime_turn: RuntimeTurnHandle | None, text: str, *, status: str) -> None:
    if runtime_turn is None:
        return
    runtime_turn.set_output(text, output_kind="feishu_visible_reply", status=status)


def _reply_idempotency_key(message_id: str, text: str) -> str:
    digest = hashlib.sha1((message_id + text[:32]).encode("utf-8")).hexdigest()[:24]
    return f"support-copilot-{digest}"
