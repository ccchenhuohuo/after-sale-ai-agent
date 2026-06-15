import re
from typing import Optional

from agents import function_tool

from agent_runtime.copilot.evidence import (
    HistoryEvidence,
    MediaEvidence,
    OfficialKbEvidence,
    short_hash,
)
from agent_runtime.settings import Settings
from agent_runtime.tools.formal_kb import search_formal_kb
from agent_runtime.tools.history_rag import search_history_rag
from agent_runtime.tools.media_rag import search_media_rag
from agent_runtime.tools.sku_catalog import search_sku_catalog_text


URL_RE = re.compile(r"https?://\S+")


def hybrid_search_kb_text(
    query: str,
    product_model: Optional[str] = None,
    module: Optional[str] = None,
    issue_type: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> str:
    return search_formal_kb(
        query,
        product_model=product_model,
        module=module,
        issue_type=issue_type,
        settings=settings,
    )


def search_official_kb_evidence(
    query: str,
    product_model: Optional[str] = None,
    module: Optional[str] = None,
    issue_type: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> list[OfficialKbEvidence]:
    message = hybrid_search_kb_text(
        query,
        product_model=product_model,
        module=module,
        issue_type=issue_type,
        settings=settings,
    )
    if not message.startswith("命中正式依据"):
        status = "error" if "服务不可用" in message or "检索异常" in message else "empty"
        return [
            OfficialKbEvidence(
                status=status,
                evidence_level="error" if status == "error" else "empty",
                verified=False,
                query_hash=short_hash(query),
                message=message,
            )
        ]

    blocks = [block.strip() for block in re.split(r"\n(?=- 来源ID：)", message) if "- 来源ID：" in block]
    if not blocks:
        return [
            OfficialKbEvidence(
                status="hit",
                evidence_level="formal",
                verified=True,
                query_hash=short_hash(query),
                snippet=message[:300],
                reference_url=_first_url(message),
                message=message,
            )
        ]

    evidence: list[OfficialKbEvidence] = []
    for block in blocks:
        evidence.append(
            OfficialKbEvidence(
                status="hit",
                evidence_level="formal",
                verified=True,
                query_hash=short_hash(query),
                title=_extract_field(block, "标题"),
                section=_extract_field(block, "章节"),
                reference_url=_extract_field(block, "链接") or _first_url(block),
                snippet=_extract_field(block, "摘要"),
                score=_extract_float(_extract_field(block, "重排分")),
                matched_reasons=[_extract_field(block, "相似原因")] if _extract_field(block, "相似原因") else [],
                message=block,
                source_type=_extract_field(block, "类型") or "official_kb",
            )
        )
    return evidence


def _extract_field(block: str, field_name: str) -> str:
    match = re.search(rf"{re.escape(field_name)}：([^\n]+)", block)
    return match.group(1).strip() if match else ""


def _first_url(text: str) -> str:
    match = URL_RE.search(text)
    return match.group(0).strip() if match else ""


def _extract_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def search_history_evidence(
    query: str,
    product_model: Optional[str] = None,
    issue_type: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> list[HistoryEvidence]:
    query_hash = short_hash(query)
    result = search_history_rag(query, product_model=product_model, issue_type=issue_type, settings=settings)
    if not result.startswith(("命中已审核群聊历史 FAQ", "命中未审核历史参考")):
        status = "error" if "服务不可用" in result else "empty"
        return [
            HistoryEvidence(
                status=status,
                evidence_level="error" if status == "error" else "empty",
                verified=False,
                query_hash=query_hash,
                message=result,
            )
        ]

    blocks = [block.strip() for block in re.split(r"\n(?=- 话题ID：)", result) if "- 话题ID：" in block]
    if not blocks:
        return [
            HistoryEvidence(
                status="hit",
                evidence_level="reviewed_case",
                verified=True,
                query_hash=query_hash,
                summary=result[:300],
                reference_url=_first_url(result),
                message=result,
            )
        ]

    evidence: list[HistoryEvidence] = []
    for block in blocks:
        evidence.append(
            HistoryEvidence(
                status="hit",
                evidence_level="reviewed_case",
                verified=True,
                query_hash=query_hash,
                topic_id=_extract_field(block, "- 话题ID") or _extract_field(block, "话题ID"),
                sku=_extract_field(block, "SKU"),
                summary=_extract_field(block, "问题摘要"),
                solution_type=_extract_field(block, "处理结论"),
                reference_url=_extract_field(block, "话题链接") or _first_url(block),
                matched_reasons=[_extract_field(block, "相似原因")] if _extract_field(block, "相似原因") else [],
                message=block,
            )
        )
    return evidence


def search_media_evidence(
    query: str,
    product_model: Optional[str] = None,
    settings: Optional[Settings] = None,
    vector_refs: Optional[list[str]] = None,
) -> list[MediaEvidence]:
    query_hash = short_hash(query)
    result = search_media_rag(query, product_model=product_model, settings=settings, vector_refs=vector_refs)
    if not result.startswith("命中未审核媒体观察证据"):
        status = "error" if "服务不可用" in result else "empty"
        return [
            MediaEvidence(
                status=status,
                evidence_level="error" if status == "error" else "empty",
                verified=False,
                query_hash=query_hash,
                message=result,
            )
        ]

    blocks = [block.strip() for block in re.split(r"\n(?=- 话题ID：)", result) if "- 话题ID：" in block]
    if not blocks:
        return [
            MediaEvidence(
                status="hit",
                evidence_level="unreviewed_media",
                verified=False,
                query_hash=query_hash,
                summary=result[:300],
                reference_url=_first_url(result),
                message=result,
            )
        ]

    evidence: list[MediaEvidence] = []
    for block in blocks:
        evidence.append(
            MediaEvidence(
                status="hit",
                evidence_level="unreviewed_media",
                verified=False,
                query_hash=query_hash,
                topic_id=_extract_field(block, "- 话题ID") or _extract_field(block, "话题ID"),
                sku=_extract_field(block, "SKU"),
                media_type=_extract_field(block, "媒体类型"),
                media_id=_extract_field(block, "媒体ID"),
                summary=_extract_field(block, "媒体观察摘要"),
                reference_url=_extract_field(block, "话题链接") or _extract_field(block, "消息链接") or _first_url(block),
                matched_reasons=[_extract_field(block, "相似原因")] if _extract_field(block, "相似原因") else [],
                message=block,
            )
        )
    return evidence


def search_issue_history_text(
    query: str,
    product_model: Optional[str] = None,
    issue_type: Optional[str] = None,
) -> str:
    sku_match = search_sku_catalog_text(product_model or query, limit=3)
    text_history = search_history_rag(query, product_model=product_model, issue_type=issue_type)
    media_evidence = search_media_rag(query, product_model=product_model)
    return "\n".join(
        [
            "混合 RAG 证据打包结果",
            "SKU 精准匹配：",
            sku_match,
            "",
            "文本历史参考：",
            text_history,
            "",
            "媒体观察证据：",
            media_evidence,
            "",
            "证据边界：",
            "- SKU 精准匹配只用于产品识别、SPU/品名/负责人流转，不能作为故障原因、售后政策或处理结论依据。",
            "- 文本历史参考来自已审核群聊历史 FAQ，可作为可靠售后参考；不是正式政策源。",
            "- 媒体观察证据来自飞书 raw media 元数据和话题上下文，默认未审核，需人工打开原话题确认，不能作为正式依据。",
            "- 任何退款、换新、补发、维修时效或最终判责，必须等待正式依据或人工复核。",
        ]
    )


@function_tool
def hybrid_search_kb(
    query: str,
    product_model: Optional[str] = None,
    module: Optional[str] = None,
    issue_type: Optional[str] = None,
) -> str:
    """
    Query official product knowledge.

    Query the formal KB/MRD/manual/policy file index when configured. Return an
    explicit missing result when the formal source index is absent or no chunk
    matches.
    """
    return hybrid_search_kb_text(query, product_model=product_model, module=module, issue_type=issue_type)


@function_tool
def search_issue_history(
    query: str,
    product_model: Optional[str] = None,
    issue_type: Optional[str] = None,
) -> str:
    """
    Query reviewed historical QA cases and support-group experience.

    Group chat history that enters the history FAQ index is treated as reviewed
    and reliable support experience. It is still not a formal policy source.
    """
    return search_issue_history_text(query, product_model=product_model, issue_type=issue_type)
