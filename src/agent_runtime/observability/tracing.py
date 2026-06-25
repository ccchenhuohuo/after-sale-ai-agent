from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from typing import Any

from agents import custom_span, trace
from agents.tracing import get_current_trace
from opentelemetry import trace as otel_trace

from agent_runtime.settings import Settings


RUNTIME_WORKFLOW_NAMES = {
    "feishu": "Feishu Support Runtime Turn",
    "openclaw_feishu": "OpenClaw Support Runtime Turn",
    "terminal": "Terminal Support Runtime Turn",
}
ADMISSION_WORKFLOW_NAME = "Feishu Bridge Admission"

IGNORED_STATUSES = {
    "ignored",
    "ignored_bot_sender",
    "skipped_app_or_bot",
    "skipped_no_trigger",
    "skipped_non_text",
    "skipped_expired",
    "suppressed_bot_loop",
    "expired",
}

TURN_ONLY_TRACE_ATTRS = {
    "input.value",
    "output.value",
    "user.input",
    "request.assets",
}


def hash_trace_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12] if value else ""


def runtime_workflow_name(entrypoint: str) -> str:
    return RUNTIME_WORKFLOW_NAMES.get(entrypoint, "Support Runtime Turn")


def base_trace_attrs(
    *,
    trace_kind: str,
    entrypoint: str,
    event_source: str = "",
    event_status: str = "",
    request_id: str = "",
    session_id: str = "",
    chat_id: str = "",
    thread_id: str = "",
    message_id: str = "",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    effective_session_id = session_id or thread_id or chat_id or request_id
    attrs: dict[str, object] = {
        "trace_kind": trace_kind,
        "entrypoint": entrypoint,
        "event_source": event_source,
        "event_status": event_status,
        "request_id_hash": hash_trace_id(request_id),
        "session_id_hash": hash_trace_id(effective_session_id),
        "chat_id_hash": hash_trace_id(chat_id),
        "thread_id_hash": hash_trace_id(thread_id),
        "message_id_hash": hash_trace_id(message_id),
        "session.id": hash_trace_id(effective_session_id),
    }
    if extra:
        attrs.update(extra)
    return safe_trace_attrs(attrs)


def safe_trace_attrs(attrs: dict[str, object]) -> dict[str, object]:
    return {key: safe_trace_value(value) for key, value in attrs.items() if value is not None}


def safe_trace_value(value: object) -> object:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [safe_trace_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): safe_trace_value(item) for key, item in value.items()}
    return str(value)


def set_current_otel_attrs(attrs: dict[str, object]) -> None:
    if not attrs:
        return
    try:
        current_span = otel_trace.get_current_span()
        for key, value in safe_trace_attrs(attrs).items():
            current_span.set_attribute(key, _otel_attr_value(value))
    except Exception:
        pass


def _otel_attr_value(value: object) -> str | int | float | bool:
    if isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def trace_metadata_attrs(attrs: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in safe_trace_attrs(attrs).items() if key not in TURN_ONLY_TRACE_ATTRS}


class RuntimeTurnHandle:
    def __init__(self, span: Any | None = None, *, include_full_io: bool = False):
        self._span = span
        self._include_full_io = include_full_io

    @property
    def active(self) -> bool:
        return self._span is not None

    def update(self, attrs: dict[str, object]) -> None:
        if self._span is None:
            return
        safe_attrs = safe_trace_attrs(attrs)
        self._span.span_data.data.update(safe_attrs)
        set_current_otel_attrs(safe_attrs)

    def set_output(self, text: str, *, output_kind: str, status: str) -> None:
        attrs: dict[str, object] = {
            "output.kind": output_kind,
            "output.status": status,
            "output_chars": len(text or ""),
        }
        if self._include_full_io:
            attrs["output.value"] = text or ""
        self.update(attrs)


def status_attrs(status: str, **extra: object) -> dict[str, object]:
    attrs = {"status": status}
    attrs.update(extra)
    return safe_trace_attrs(attrs)


def tool_like_attrs(
    *,
    tool_name: str,
    status: str,
    query_hash: str = "",
    query_chars: int = 0,
    latency_ms: float = 0.0,
    result_count: int = 0,
    top_score: float = 0.0,
    index_available: bool = False,
    provider: str = "",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    attrs: dict[str, object] = {
        "operation": "tool_like_retrieval",
        "tool_name": tool_name,
        "status": status,
        "query_hash": query_hash,
        "query_chars": query_chars,
        "latency_ms": latency_ms,
        "result_count": result_count,
        "top_score": top_score,
        "index_available": index_available,
        "provider": provider,
    }
    if extra:
        attrs.update(extra)
    return safe_trace_attrs(attrs)


def ingestion_tool_attrs(
    *,
    tool_name: str,
    status: str,
    asset_id: str = "",
    media_type: str = "",
    provider: str = "",
    model_name: str = "",
    latency_ms: float = 0.0,
    vector_id: str = "",
    error_type: str = "",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    attrs: dict[str, object] = {
        "operation": "ingestion_tool",
        "tool_name": tool_name,
        "status": status,
        "asset_id_hash": hash_trace_id(asset_id),
        "media_type": media_type,
        "provider": provider,
        "model_name": model_name,
        "latency_ms": latency_ms,
        "vector_id_hash": hash_trace_id(vector_id),
        "error_type": error_type,
    }
    if extra:
        attrs.update(extra)
    return safe_trace_attrs(attrs)


def should_trace_admission(settings: Settings, *, status: str, stable_key: str = "") -> bool:
    mode = settings.support_trace_admission_mode.lower().strip()
    if mode == "off":
        return False
    if status == "duplicate" and not settings.support_trace_duplicate_events:
        return False
    if status in IGNORED_STATUSES and not settings.support_trace_ignored_events:
        return False
    if mode == "full":
        return True
    if mode != "sample":
        return False
    if status not in {"accepted", "processing"}:
        return _sampled(stable_key or status, settings.support_trace_admission_sample_rate)
    return False


def _sampled(value: str, rate: float) -> bool:
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    bucket = int(digest, 16) / 0xFFFFFFFF
    return bucket < rate


@contextmanager
def admission_trace(settings: Settings, attrs: dict[str, object]):
    with trace(
        ADMISSION_WORKFLOW_NAME,
        group_id=str(attrs.get("trace_group_id") or ""),
        metadata=safe_trace_attrs(attrs),
        disabled=settings.support_agent_tracing_disabled,
    ):
        yield


@contextmanager
def runtime_trace(settings: Settings, *, entrypoint: str, group_id: str, attrs: dict[str, object]):
    if get_current_trace() is not None:
        yield RuntimeTurnHandle()
        return
    with trace(
        runtime_workflow_name(entrypoint),
        group_id=group_id,
        metadata=trace_metadata_attrs(attrs),
        disabled=settings.support_agent_tracing_disabled,
    ):
        turn_safe_attrs = safe_trace_attrs(attrs)
        if not settings.support_agent_trace_include_sensitive_data:
            turn_safe_attrs = {key: value for key, value in turn_safe_attrs.items() if key not in TURN_ONLY_TRACE_ATTRS}
        turn_attrs = {
            "span.kind": "runtime_turn",
            **turn_safe_attrs,
        }
        with custom_span("support_runtime_turn", turn_attrs) as turn_span:
            set_current_otel_attrs(turn_attrs)
            yield RuntimeTurnHandle(
                turn_span,
                include_full_io=settings.support_agent_trace_include_sensitive_data,
            )


@contextmanager
def span(name: str, attrs: dict[str, object] | None = None):
    safe_attrs = safe_trace_attrs(attrs or {})
    with custom_span(name, safe_attrs):
        set_current_otel_attrs(safe_attrs)
        yield


@contextmanager
def span_if_tracing(name: str, attrs: dict[str, object] | None = None):
    if get_current_trace() is None:
        yield None
        return
    safe_attrs = safe_trace_attrs(attrs or {})
    with custom_span(name, safe_attrs) as active_span:
        set_current_otel_attrs(safe_attrs)
        yield active_span


def elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)
