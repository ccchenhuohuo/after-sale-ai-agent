from __future__ import annotations

import json
import logging
import re

from agents import Agent, ModelSettings, Runner, custom_span

from agent_runtime.copilot.case_context import (
    AssetRouteDecision,
    RouteDecision,
    SupportAsset,
    SupportCaseRequest,
)
from agent_runtime.copilot.llm_payloads import safe_asset_payload_for_llm
from agent_runtime.llm import build_run_config
from agent_runtime.observability.tracing import hash_trace_id
from agent_runtime.settings import Settings


logger = logging.getLogger(__name__)


TEXT_LIKE_HINTS = ("chat", "screenshot", "截图", "聊天", "invoice", "发票", "error", "报错", "文字", "text")
PRODUCT_HINTS = ("product", "产品", "damage", "broken", "损坏", "断裂", "label", "铭牌", "包装", "package")
LOW_QUALITY_HINTS = ("low_quality", "blur", "模糊", "unknown")
VALID_ASSET_ROLES = {
    "chat_screenshot",
    "invoice",
    "error_screenshot",
    "text_document",
    "product_photo",
    "damage_photo",
    "label_photo",
    "packaging_photo",
    "video",
    "unknown",
}


def build_case_intake_router_agent(model_name: str) -> Agent:
    return Agent(
        name="AfterSales Case Intake Router",
        instructions=(
            "你是售后 Copilot 的输入分类代理。你的唯一任务是判断用户输入和附件应进入哪些处理链路。"
            "不要回答售后问题，不要给处理建议，不要生成客服话术。"
            "必须输出 RouteDecision。图片分类可以多标签：文字截图可 OCR，产品/损坏/铭牌图片可视觉向量化，"
            "带文字的产品图片可以同时 OCR 和视觉向量化。视频需要采样和视觉向量化。"
        ),
        model=model_name,
        tools=[],
        output_type=RouteDecision,
        model_settings=ModelSettings(temperature=0),
    )


async def route_support_case(request: SupportCaseRequest, settings: Settings) -> RouteDecision:
    if settings.support_intake_router_enabled:
        try:
            return await _route_with_agent(request, settings)
        except Exception as exc:
            logger.warning("Intake router agent failed; using deterministic fallback: %s", exc)
    return deterministic_route_support_case(request)


async def _route_with_agent(request: SupportCaseRequest, settings: Settings) -> RouteDecision:
    model_name = settings.support_intake_router_model or settings.support_agent_model_flash or settings.support_agent_model
    agent = build_case_intake_router_agent(model_name)
    payload = {
        "user_text": request.user_text,
        "assets": [safe_asset_payload_for_llm(asset) for asset in request.assets],
    }
    with custom_span(
        "intake_router_agent",
        {
            "request_id_hash": hash_trace_id(request.request_id),
            "asset_count": len(request.assets),
            "source": request.source,
            "model": model_name,
        },
    ):
        result = await Runner.run(
            agent,
            json.dumps(payload, ensure_ascii=False),
            run_config=build_run_config(
                settings,
                group_id=f"intake:{hash_trace_id(request.trace_group_id or request.session_id or request.request_id)}",
                metadata={"source": request.source, "stage": "intake_router"},
            ),
        )
    if isinstance(result.final_output, RouteDecision):
        return result.final_output
    if isinstance(result.final_output, dict):
        return RouteDecision.model_validate(result.final_output)
    return RouteDecision.model_validate_json(str(result.final_output))


def deterministic_route_support_case(request: SupportCaseRequest) -> RouteDecision:
    text = _normalize(request.user_text)
    asset_decisions = [_route_asset(asset) for asset in request.assets]
    has_text = bool(text)
    modality = _input_modality(has_text, asset_decisions)
    needs_clarification = modality == "needs_clarification" or any(item.confidence < 0.35 for item in asset_decisions)
    questions = []
    if not has_text and not asset_decisions:
        questions.append("请补充售后问题描述或清晰图片/视频。")
    if any(item.confidence < 0.35 for item in asset_decisions):
        questions.append("请补充更清晰的图片、视频或产品型号信息。")

    confidence = 0.9 if has_text and not asset_decisions else 0.72
    if asset_decisions:
        confidence = min(confidence, min(item.confidence for item in asset_decisions))
    if needs_clarification:
        confidence = min(confidence, 0.45)

    return RouteDecision(
        input_modality=modality,
        confidence=confidence,
        reason=_route_reason(has_text, asset_decisions),
        user_text=text,
        asset_decisions=asset_decisions,
        needs_clarification=needs_clarification,
        clarification_questions=questions,
    )


def _route_asset(asset: SupportAsset) -> AssetRouteDecision:
    hint_text = _asset_hint_text(asset)
    explicit_role = str(asset.metadata.get("asset_role") or "").strip()
    if explicit_role:
        role = explicit_role
    elif asset.media_type == "video":
        role = "video"
    elif _contains_any(hint_text, ("label", "铭牌")):
        role = "label_photo"
    elif _contains_any(hint_text, ("damage", "broken", "损坏", "断裂", "裂", "坏")):
        role = "damage_photo"
    elif _contains_any(hint_text, ("package", "包装")):
        role = "packaging_photo"
    elif _contains_any(hint_text, TEXT_LIKE_HINTS):
        role = "chat_screenshot" if _contains_any(hint_text, ("chat", "聊天")) else "text_document"
    elif _contains_any(hint_text, PRODUCT_HINTS):
        role = "product_photo"
    else:
        role = "unknown"

    low_quality = _contains_any(hint_text, LOW_QUALITY_HINTS) or bool(asset.metadata.get("low_quality"))
    requires_ocr = role in {"chat_screenshot", "invoice", "error_screenshot", "text_document", "label_photo"}
    requires_visual_embedding = role in {"product_photo", "damage_photo", "label_photo", "packaging_photo", "video"}
    requires_video_sampling = asset.media_type == "video" or role == "video"

    if asset.media_type == "image" and role == "unknown":
        requires_ocr = True
        requires_visual_embedding = True
    if asset.media_type == "video":
        requires_visual_embedding = True
        requires_video_sampling = True
    if asset.media_type == "file" and _contains_any(hint_text, ("pdf", "doc", "文档")):
        requires_ocr = True

    confidence = 0.3 if low_quality else 0.55 if role == "unknown" else 0.78
    return AssetRouteDecision(
        asset_id=asset.asset_id,
        media_type=asset.media_type,
        asset_role=role if role in VALID_ASSET_ROLES else "unknown",
        requires_ocr=requires_ocr,
        requires_visual_embedding=requires_visual_embedding,
        requires_video_sampling=requires_video_sampling,
        confidence=confidence,
        reason=_asset_route_reason(asset, role, low_quality),
    )


def _input_modality(has_text: bool, asset_decisions: list[AssetRouteDecision]) -> str:
    if not has_text and not asset_decisions:
        return "needs_clarification"
    if has_text and not asset_decisions:
        return "text"
    if has_text and asset_decisions:
        return "mixed"
    media_types = {item.media_type for item in asset_decisions}
    if media_types == {"image"}:
        return "image"
    if "video" in media_types:
        return "video"
    return "unknown"


def _route_reason(has_text: bool, asset_decisions: list[AssetRouteDecision]) -> str:
    if has_text and not asset_decisions:
        return "输入只有文本，直接进入上下文。"
    if has_text and asset_decisions:
        return "输入包含文本和附件，需要合并文本、OCR/视觉结果进入上下文。"
    if asset_decisions:
        return "输入包含附件，需要先完成附件分流和 ingestion。"
    return "输入缺少可处理内容。"


def _asset_route_reason(asset: SupportAsset, role: str, low_quality: bool) -> str:
    if low_quality:
        return "附件质量或类型不明确，需要补充信息。"
    if asset.media_type == "video":
        return "视频附件需要采样并进行视觉语义处理。"
    if role == "unknown":
        return "图片类型不明确，保守同时尝试 OCR 和视觉处理。"
    return f"附件识别为 {role}，按对应链路处理。"


def _asset_hint_text(asset: SupportAsset) -> str:
    values = [
        asset.filename,
        asset.mime_type,
        asset.file_key,
        str(asset.metadata.get("asset_role") or ""),
        str(asset.metadata.get("description") or ""),
        str(asset.metadata.get("source_type") or ""),
    ]
    return " ".join(value for value in values if value).lower()


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.lower() in text for needle in needles)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
