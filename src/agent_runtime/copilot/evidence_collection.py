from __future__ import annotations

import asyncio
import re
import time
from typing import Callable, TypeVar

from agents import custom_span

from agent_runtime.copilot.evidence import (
    HistoryEvidence,
    MediaEvidence,
    OfficialKbEvidence,
    SkuEvidence,
    SupportEvidencePack,
    evidence_pack_trace_attributes,
    short_hash,
)
from agent_runtime.copilot.case_context import UnifiedCaseContext
from agent_runtime.observability.tracing import elapsed_ms, tool_like_attrs
from agent_runtime.settings import Settings, get_settings
from agent_runtime.tools.formal_kb import formal_kb_index_available
from agent_runtime.tools.history_rag import history_rag_index_available
from agent_runtime.tools.media_rag import media_rag_index_available
from agent_runtime.tools.rag import search_history_evidence, search_media_evidence, search_official_kb_evidence
from agent_runtime.tools.sku_catalog import resolve_sku_evidence


T = TypeVar("T")
SKU_RE = re.compile(r"\b[A-Z]{1,4}\d{2,5}[A-Z0-9-]*\b")


async def collect_support_evidence(raw_issue: str | UnifiedCaseContext, settings: Settings | None = None) -> SupportEvidencePack:
    settings = settings or get_settings()
    normalized_issue = _normalize_issue(_issue_text(raw_issue))
    vector_refs = _vector_refs(raw_issue)
    query_hash = short_hash(normalized_issue)
    with custom_span(
        "input_normalize",
        {
            "raw_issue_hash": query_hash,
            "query_chars": len(normalized_issue),
            "sku_token_count": len(SKU_RE.findall(normalized_issue.upper())),
        },
    ):
        issue_type = infer_issue_type(normalized_issue)

    sku_items = await _span_to_thread(
        "sku_resolve",
        {"raw_issue_hash": query_hash},
        resolve_sku_evidence,
        normalized_issue,
        5,
        settings,
        tool_name="sku_catalog",
        query=normalized_issue,
        provider="local_csv",
        index_available=True,
    )
    product_model = _best_product_model(sku_items)

    with custom_span(
        "evidence_collect",
        {
            "raw_issue_hash": query_hash,
            "issue_type": issue_type,
            "product_model_hash": short_hash(product_model) if product_model else "",
            "history_provider": settings.history_rag_provider,
            "media_provider": settings.media_rag_provider,
        },
    ):
        branch_data = {
            "raw_issue_hash": query_hash,
            "product_model_hash": short_hash(product_model) if product_model else "",
            "issue_type": issue_type,
        }
        official_task = _span_to_thread(
            "official_kb_search",
            branch_data,
            search_official_kb_evidence,
            normalized_issue,
            product_model,
            None,
            issue_type,
            settings,
            tool_name="official_kb",
            query=normalized_issue,
            provider=settings.formal_kb_provider,
            index_available=formal_kb_index_available(settings),
        )
        history_task = _span_to_thread(
            "history_search",
            branch_data,
            search_history_evidence,
            normalized_issue,
            product_model,
            issue_type,
            settings,
            tool_name="history_rag",
            query=normalized_issue,
            provider=settings.history_rag_provider,
            index_available=history_rag_index_available(settings),
        )
        media_task = _span_to_thread(
            "media_search",
            branch_data,
            search_media_evidence,
            normalized_issue,
            product_model,
            settings,
            vector_refs,
            tool_name="media_rag",
            query=normalized_issue,
            provider=settings.media_rag_provider,
            index_available=media_rag_index_available(settings),
        )
        official_result, history_result, media_result = await asyncio.gather(
            official_task,
            history_task,
            media_task,
            return_exceptions=True,
        )

    official_items = _coerce_official_result(official_result, normalized_issue)
    history_items = _coerce_history_result(history_result, normalized_issue)
    media_items = _coerce_media_result(media_result, normalized_issue)
    pack = SupportEvidencePack(
        raw_issue_hash=query_hash,
        query_chars=len(normalized_issue),
        issue_type=issue_type,
        product_model=product_model,
        sku=sku_items,
        official=official_items,
        history=history_items,
        media=media_items,
    )
    with custom_span("evidence_pack", evidence_pack_trace_attributes(pack)):
        return pack


def infer_issue_type(raw_issue: str) -> str:
    text = raw_issue.lower()
    quality_terms = ("质量", "掉", "脱落", "断", "裂", "胶水", "变形", "冒烟", "过热", "漏电", "烧焦")
    troubleshooting_terms = ("无法", "不能", "失败", "异常", "报错", "不亮", "连不上", "识别不了", "失效")
    product_usage_terms = ("怎么", "如何", "怎样", "设置", "使用", "安装", "配对", "教程")
    if any(term in text for term in quality_terms):
        return "quality_issue"
    if any(term in text for term in troubleshooting_terms):
        return "troubleshooting"
    if any(term in text for term in product_usage_terms):
        return "product_usage"
    return "unknown"


def _issue_text(raw_issue: str | UnifiedCaseContext) -> str:
    if isinstance(raw_issue, UnifiedCaseContext):
        return raw_issue.normalized_query
    return raw_issue


def _vector_refs(raw_issue: str | UnifiedCaseContext) -> list[str]:
    if isinstance(raw_issue, UnifiedCaseContext):
        return list(raw_issue.vector_refs)
    return []


def _normalize_issue(raw_issue: str) -> str:
    return re.sub(r"\s+", " ", raw_issue or "").strip()


async def _to_thread(func: Callable[..., T], *args: object) -> T:
    return await asyncio.to_thread(func, *args)


async def _span_to_thread(
    name: str,
    data: dict[str, object],
    func: Callable[..., T],
    *args: object,
    tool_name: str,
    query: str,
    provider: str,
    index_available: bool,
) -> T:
    started_at = time.perf_counter()
    span = custom_span(name, data)
    with span:
        try:
            result = await _to_thread(func, *args)
        except Exception as exc:
            span.span_data.data.update(
                tool_like_attrs(
                    tool_name=tool_name,
                    status="error",
                    query_hash=short_hash(query),
                    query_chars=len(query),
                    latency_ms=elapsed_ms(started_at),
                    index_available=index_available,
                    provider=provider,
                    extra={"error_type": type(exc).__name__},
                )
            )
            raise
        span.span_data.data.update(
            tool_like_attrs(
                tool_name=tool_name,
                status=_retrieval_status(result),
                query_hash=short_hash(query),
                query_chars=len(query),
                latency_ms=elapsed_ms(started_at),
                result_count=_result_count(result),
                top_score=_top_score(result),
                index_available=index_available,
                provider=provider,
            )
        )
        return result


def _retrieval_status(result: object) -> str:
    if isinstance(result, list):
        if not result:
            return "empty"
        statuses = {str(getattr(item, "status", "")) for item in result}
        if "hit" in statuses:
            return "hit"
        if "error" in statuses:
            return "error"
        return "empty"
    return "ok"


def _result_count(result: object) -> int:
    if isinstance(result, list):
        return len([item for item in result if getattr(item, "status", "") == "hit"])
    return 0


def _top_score(result: object) -> float:
    if not isinstance(result, list):
        return 0.0
    scores = []
    for item in result:
        score = getattr(item, "score", 0.0) or getattr(item, "rerank_score", 0.0) or 0.0
        try:
            scores.append(float(score))
        except (TypeError, ValueError):
            continue
    return max(scores) if scores else 0.0


def _best_product_model(items: list[SkuEvidence]) -> str:
    for item in items:
        if item.status == "hit" and item.sku:
            return item.sku
    for item in items:
        if item.status == "hit" and item.spu:
            return item.spu
    return ""


def _coerce_official_result(result: object, query: str) -> list[OfficialKbEvidence]:
    if isinstance(result, Exception):
        return [
            OfficialKbEvidence(
                status="error",
                evidence_level="error",
                verified=False,
                query_hash=short_hash(query),
                message=f"未查询到可信正式依据：正式知识库检索异常（{type(result).__name__}）。",
            )
        ]
    if isinstance(result, list):
        return result
    return [
        OfficialKbEvidence(
            status="error",
            evidence_level="error",
            verified=False,
            query_hash=short_hash(query),
            message=f"未查询到可信正式依据：正式知识库返回异常类型（{type(result).__name__}）。",
        )
    ]


def _coerce_history_result(result: object, query: str) -> list[HistoryEvidence]:
    if isinstance(result, Exception):
        return [
            HistoryEvidence(
                status="error",
                evidence_level="error",
                verified=False,
                query_hash=short_hash(query),
                message=f"未查询到可信历史参考：历史话题检索异常（{type(result).__name__}）。",
            )
        ]
    if isinstance(result, list):
        return result
    return [
        HistoryEvidence(
            status="error",
            evidence_level="error",
            verified=False,
            query_hash=short_hash(query),
            message=f"未查询到可信历史参考：历史话题返回异常类型（{type(result).__name__}）。",
        )
    ]


def _coerce_media_result(result: object, query: str) -> list[MediaEvidence]:
    if isinstance(result, Exception):
        return [
            MediaEvidence(
                status="error",
                evidence_level="error",
                verified=False,
                query_hash=short_hash(query),
                message=f"未查询到可信媒体观察证据：媒体检索异常（{type(result).__name__}）。",
            )
        ]
    if isinstance(result, list):
        return result
    return [
        MediaEvidence(
            status="error",
            evidence_level="error",
            verified=False,
            query_hash=short_hash(query),
            message=f"未查询到可信媒体观察证据：媒体返回异常类型（{type(result).__name__}）。",
        )
    ]
