from __future__ import annotations

import json
import logging
import re

from agents import Agent, ModelSettings, Runner, custom_span

from agent_runtime.copilot.case_context import (
    DataSourceCoverage,
    DataSourceCoverageItem,
    IngestionArtifact,
    RouteDecision,
    SupportCaseRequest,
    UnifiedCaseContext,
)
from agent_runtime.copilot.evidence import SupportEvidencePack
from agent_runtime.copilot.llm_payloads import safe_artifact_payload_for_llm, safe_request_payload_for_llm
from agent_runtime.llm import build_run_config
from agent_runtime.settings import Settings


logger = logging.getLogger(__name__)
SKU_RE = re.compile(r"\b[A-Z]{1,4}\d{2,5}[A-Z0-9-]*\b")


def build_context_assembler_agent(model_name: str) -> Agent:
    return Agent(
        name="AfterSales Context Assembler",
        instructions=(
            "你是售后上下文整合代理。你会收到原始文本、OCR 文本、视觉摘要和向量引用。"
            "你的任务是生成 UnifiedCaseContext：归一化用户问题，识别产品、故障、意图，"
            "保留 asset_refs/vector_refs，并列出缺失信息。不要回答售后问题。"
            "不要输出原始向量数组。"
        ),
        model=model_name,
        tools=[],
        output_type=UnifiedCaseContext,
        model_settings=ModelSettings(temperature=0.1),
    )


async def assemble_unified_case_context(
    request: SupportCaseRequest,
    route: RouteDecision,
    artifacts: list[IngestionArtifact],
    settings: Settings,
) -> UnifiedCaseContext:
    if settings.support_context_assembler_enabled:
        try:
            return await _assemble_with_agent(request, route, artifacts, settings)
        except Exception as exc:
            logger.warning("Context assembler agent failed; using deterministic fallback: %s", exc)
    return deterministic_assemble_unified_case_context(request, route, artifacts)


async def _assemble_with_agent(
    request: SupportCaseRequest,
    route: RouteDecision,
    artifacts: list[IngestionArtifact],
    settings: Settings,
) -> UnifiedCaseContext:
    model_name = settings.support_context_assembler_model or settings.support_agent_model
    agent = build_context_assembler_agent(model_name)
    payload = {
        "request": safe_request_payload_for_llm(request),
        "route": route.model_dump(),
        "artifacts": [safe_artifact_payload_for_llm(artifact) for artifact in artifacts],
    }
    with custom_span(
        "context_assembler_agent",
        {
            "request_id": request.request_id,
            "artifact_count": len(artifacts),
            "source": request.source,
            "model": model_name,
        },
    ):
        result = await Runner.run(
            agent,
            json.dumps(payload, ensure_ascii=False),
            run_config=build_run_config(
                settings,
                group_id=request.trace_group_id or request.session_id or request.request_id,
                metadata={"source": request.source, "stage": "context_assembler"},
            ),
        )
    if isinstance(result.final_output, UnifiedCaseContext):
        return result.final_output
    if isinstance(result.final_output, dict):
        return UnifiedCaseContext.model_validate(result.final_output)
    return UnifiedCaseContext.model_validate_json(str(result.final_output))


def deterministic_assemble_unified_case_context(
    request: SupportCaseRequest,
    route: RouteDecision,
    artifacts: list[IngestionArtifact],
) -> UnifiedCaseContext:
    text_parts = [artifact.text for artifact in artifacts if artifact.status == "ok" and artifact.text]
    visual_summaries = [artifact.summary for artifact in artifacts if artifact.status == "ok" and artifact.vector_id]
    vector_refs = [artifact.vector_id for artifact in artifacts if artifact.status == "ok" and artifact.vector_id]
    asset_refs = [asset.asset_id for asset in request.assets]
    normalized_query = _normalize_query([request.user_text, *text_parts, *visual_summaries])
    missing_information = _missing_information(route, artifacts, normalized_query)
    detected_product = _first_sku(normalized_query)
    detected_fault = _detected_fault(normalized_query)
    customer_intent = _customer_intent(normalized_query, route)
    confidence = route.confidence
    if missing_information:
        confidence = min(confidence, 0.55)

    return UnifiedCaseContext(
        request_id=request.request_id,
        source=request.source,
        original_user_text=request.user_text,
        normalized_query=normalized_query or "用户未提供可直接分析的售后问题。",
        extracted_texts=text_parts,
        visual_summaries=visual_summaries,
        asset_refs=asset_refs,
        vector_refs=vector_refs,
        artifact_ids=[artifact.artifact_id for artifact in artifacts],
        detected_product=detected_product,
        detected_fault=detected_fault,
        customer_intent=customer_intent,
        missing_information=missing_information,
        confidence=confidence,
        route=route,
    )


def build_data_source_coverage(
    context: UnifiedCaseContext,
    evidence_pack: SupportEvidencePack,
) -> DataSourceCoverage:
    items = [
        _coverage_item(
            "sku_catalog",
            "SKU 目录",
            "hit" if evidence_pack.sku_hit_count else "miss",
            "identity_only",
            evidence_pack.sku_hit_count,
            "高" if evidence_pack.sku_hit_count else "低",
            "用于产品识别和负责人候选，不能单独作为故障或政策依据。",
        ),
        _coverage_item(
            "official_kb",
            "正式知识库",
            "hit" if evidence_pack.official_hit_count else "missing",
            "formal" if evidence_pack.official_hit_count else "missing",
            evidence_pack.official_hit_count,
            "高" if evidence_pack.official_hit_count else "未知",
            "正式产品文档/MRD 尚未接入或未命中。" if not evidence_pack.official_hit_count else "已命中正式依据。",
        ),
        _coverage_item(
            "history_faq",
            "群聊历史 FAQ",
            "hit" if evidence_pack.history_hit_count else "miss",
            "unreviewed",
            evidence_pack.history_hit_count,
            "中" if evidence_pack.history_hit_count else "低",
            "当前主要 grounded 数据源；raw 群聊历史需人工确认。",
        ),
        _coverage_item(
            "media_evidence",
            "媒体观察证据",
            "hit" if evidence_pack.media_hit_count else "miss",
            "media_observation",
            evidence_pack.media_hit_count,
            "中" if evidence_pack.media_hit_count else "低",
            "媒体证据只能作为视觉线索，不能单独作为正式结论。",
        ),
        _coverage_item(
            "product_mrd",
            "产品 MRD/手册",
            "missing",
            "missing",
            0,
            "未知",
            "产品 MRD/手册数据源暂未接入。",
        ),
    ]
    action, reason = _recommended_action(context, evidence_pack)
    return DataSourceCoverage(
        items=items,
        recommended_action=action,
        owner_candidate=_owner_candidate(evidence_pack),
        mention_enabled=False,
        reason=reason,
    )


def render_case_context_for_prompt(context: UnifiedCaseContext, coverage: DataSourceCoverage | None = None) -> str:
    lines = [
        "统一售后上下文",
        f"- 输入类型：{context.route.input_modality}",
        f"- 归一化问题：{context.normalized_query}",
        f"- 产品候选：{context.detected_product or '未识别'}",
        f"- 故障/意图：{context.detected_fault or context.customer_intent or '未识别'}",
        f"- 附件引用：{', '.join(context.asset_refs) if context.asset_refs else '无'}",
        f"- 向量引用：{', '.join(context.vector_refs) if context.vector_refs else '无'}",
        f"- 缺失信息：{'；'.join(context.missing_information) if context.missing_information else '无'}",
    ]
    if coverage is not None:
        used = [item.source_name for item in coverage.items if item.status == "hit"]
        missing = [item.source_name for item in coverage.items if item.status in {"missing", "not_configured"}]
        lines.extend(
            [
                "",
                "数据源覆盖",
                f"- 已命中/可参考：{', '.join(used) if used else '无'}",
                f"- 缺失/未接入：{', '.join(missing) if missing else '无'}",
                f"- 建议动作：{coverage.recommended_action}",
                f"- 人工复核窗口：{'开启' if coverage.mention_enabled else '仅建议，不实际艾特'}",
                f"- 原因：{coverage.reason}",
            ]
        )
    return "\n".join(lines)


def _coverage_item(
    source_id: str,
    source_name: str,
    status: str,
    authority: str,
    hit_count: int,
    confidence: str,
    message: str,
) -> DataSourceCoverageItem:
    return DataSourceCoverageItem(
        source_id=source_id,
        source_name=source_name,
        status=status,
        authority=authority,
        hit_count=hit_count,
        confidence=confidence,
        message=message,
    )


def _recommended_action(context: UnifiedCaseContext, evidence_pack: SupportEvidencePack) -> tuple[str, str]:
    if context.missing_information:
        return "ask_clarification", "输入信息或多模态处理结果不足，需要先补充关键信息。"
    if evidence_pack.has_formal_evidence:
        return "answer", "已命中正式依据，可以给出相对肯定的客服参考。"
    if evidence_pack.history_hit_count or evidence_pack.media_hit_count:
        return "answer", "已命中历史或媒体参考，可给出保守参考并标注需人工确认。"
    return "human_review", "没有命中可支撑处理口径的数据源，建议人工复核但不实际艾特负责人。"


def _owner_candidate(evidence_pack: SupportEvidencePack) -> str:
    for item in evidence_pack.sku:
        if item.product_owner_name:
            return item.product_owner_name
    return ""


def _normalize_query(parts: list[str]) -> str:
    clean_parts = [part.strip() for part in parts if part and part.strip()]
    return " ".join(dict.fromkeys(clean_parts))


def _missing_information(route: RouteDecision, artifacts: list[IngestionArtifact], normalized_query: str) -> list[str]:
    missing = list(route.clarification_questions)
    if not normalized_query.strip():
        missing.append("需要补充售后问题描述。")
    for artifact in artifacts:
        if artifact.status not in {"unsupported", "error"}:
            continue
        if artifact.artifact_type == "ocr":
            missing.append("图片文字内容暂未完成 OCR 识别。")
        elif artifact.artifact_type in {"image_embedding", "video_sampling"}:
            missing.append("图片/视频视觉语义结果暂未生成。")
    return list(dict.fromkeys(missing))


def _first_sku(text: str) -> str:
    match = SKU_RE.search(text.upper())
    return match.group(0) if match else ""


def _detected_fault(text: str) -> str:
    if any(term in text for term in ("不亮", "无法", "不能", "失效", "报错", "连不上")):
        return "troubleshooting"
    if any(term in text for term in ("损坏", "断裂", "裂", "脱落", "质量", "胶水")):
        return "quality_issue"
    return ""


def _customer_intent(text: str, route: RouteDecision) -> str:
    if route.input_modality == "needs_clarification":
        return "needs_clarification"
    if any(term in text for term in ("退款", "退货", "换新", "补发")):
        return "after_sales_action"
    if any(term in text for term in ("怎么", "如何", "教程", "设置")):
        return "product_usage"
    return "support_question"
