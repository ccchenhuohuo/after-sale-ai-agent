from __future__ import annotations

import asyncio
import hashlib
import logging

from agents import Runner, SQLiteSession, flush_traces
from agents.memory import SessionSettings

from agent_runtime.copilot.answer_contract import validate_answer_contract
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
        session_settings=SessionSettings(limit=settings.support_agent_session_limit),
    )
    user_input = build_feishu_user_input(event, settings)
    thread_id = effective_thread_id(event)
    result = await Runner.run(
        agent,
        build_agent_input(user_input, source="飞书客服话题群"),
        session=session,
        run_config=build_run_config(
            settings,
            group_id=f"feishu:{event.chat_id}:thread:{thread_id}",
            metadata={
                "source": "feishu-bot",
                "chat_id_hash": hashlib.sha1(event.chat_id.encode("utf-8")).hexdigest()[:12],
                "thread_id_hash": hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:12],
                "message_id_hash": hashlib.sha1(event.message_id.encode("utf-8")).hexdigest()[:12],
            },
        ),
    )
    flush_traces()
    output = result.final_output.strip()
    issues = validate_answer_contract(output, history_connected=history_rag_index_available(settings))
    if issues:
        output += "\n\n内部校验提醒：\n" + "\n".join(f"- {issue.code}: {issue.message}" for issue in issues)
    return output


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
    gate = should_accept(event, settings, bot_identity or bot_identity_for_settings(settings), runtime_store)
    if not gate.accepted:
        logger.info(
            "Ignored Feishu event: status=%s message_id=%s chat_id=%s thread_id=%s root_id=%s sender_type=%s",
            gate.status,
            event.message_id,
            event.chat_id,
            event.thread_id,
            event.root_id,
            event.sender_type,
        )
        return gate.status
    if not runtime_store.try_record_event(event):
        return "duplicate"
    queue_key = queue_key_for_event(event)
    logger.info("Queued Feishu event: message_id=%s queue_key=%s", event.message_id, queue_key)
    async with _QUEUE.lock_for_event(event):
        logger.info("Processing Feishu event: message_id=%s queue_key=%s", event.message_id, queue_key)
        if semaphore is None:
            return await _process_message_event_unlocked(event, settings, runtime_store)
        async with semaphore:
            return await _process_message_event_unlocked(event, settings, runtime_store)


async def _process_message_event_unlocked(
    event: FeishuMessageEvent,
    settings: Settings,
    runtime_store: RuntimeStore,
) -> str:
    try:
        answer = await run_support_agent_for_event(event, settings)
    except Exception as exc:
        runtime_store.record_event_error("agent", event, str(exc))
        answer = "AI 客服参考生成失败，请人工处理。\n错误：" + str(exc)
    try:
        reply_result = await reply_in_thread(event.message_id, answer, settings)
    except Exception as exc:
        runtime_store.record_reply(event, "reply_failed", error=str(exc))
        runtime_store.record_event_error("reply", event, str(exc))
        logger.exception(
            "Failed to reply to Feishu thread; skipping any top-level fallback. message_id=%s thread_id=%s root_id=%s",
            event.message_id,
            event.thread_id,
            event.root_id,
        )
        return "reply_failed"
    reply_message_id = reply_result.reply_message_id if isinstance(reply_result, ReplyResult) else ""
    runtime_store.record_reply(event, "replied", reply_message_id=reply_message_id)
    return "replied"


def _reply_idempotency_key(message_id: str, text: str) -> str:
    digest = hashlib.sha1((message_id + text[:32]).encode("utf-8")).hexdigest()[:24]
    return f"support-copilot-{digest}"
