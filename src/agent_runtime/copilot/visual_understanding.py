from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from agent_runtime.settings import Settings


ROOT = Path(__file__).resolve().parents[3]
VISUAL_FIELD_KEYS = (
    "visible_product",
    "visible_part",
    "damage_type",
    "damage_location",
    "severity",
    "text_seen",
    "customer_claim_supported",
    "confidence",
    "recommended_follow_up",
)


@dataclass(frozen=True)
class VisualUnderstandingResult:
    status: str
    summary: str = ""
    fields: dict[str, Any] | None = None
    model_name: str = ""
    error: str = ""
    source_kind: str = ""


def understand_visual_inputs(
    image_paths_or_urls: list[str],
    settings: Settings,
    *,
    asset_role: str = "",
    user_text: str = "",
) -> VisualUnderstandingResult:
    provider = settings.support_visual_understanding_provider.strip().lower()
    model_name = settings.support_visual_understanding_model or "qwen-vl-plus"
    if provider in {"", "disabled", "none"}:
        return VisualUnderstandingResult(
            status="unsupported",
            model_name=provider or "disabled",
            error="Visual understanding provider disabled",
        )
    if provider in {"fake", "local_fake"}:
        return _fake_visual_understanding(
            asset_role=asset_role,
            user_text=user_text,
            model_name="fake-visual-understanding",
        )
    if provider not in {"bailian_vl", "dashscope_vl", "qwen_vl"}:
        return VisualUnderstandingResult(
            status="unsupported",
            model_name=model_name,
            error=f"Unsupported visual understanding provider: {settings.support_visual_understanding_provider}",
        )
    return _understand_with_bailian_vl(
        image_paths_or_urls,
        settings,
        asset_role=asset_role,
        user_text=user_text,
        model_name=model_name,
    )


def _understand_with_bailian_vl(
    image_paths_or_urls: list[str],
    settings: Settings,
    *,
    asset_role: str,
    user_text: str,
    model_name: str,
) -> VisualUnderstandingResult:
    api_key = settings.resolved_bailian_api_key
    if not api_key:
        return VisualUnderstandingResult(
            status="unsupported",
            model_name=model_name,
            error="Bailian/DashScope API key not configured",
        )

    image_payloads: list[str] = []
    source_kinds: list[str] = []
    max_images = max(1, settings.support_visual_understanding_max_images)
    for value in image_paths_or_urls[:max_images]:
        image_url, source_kind, input_error = _image_url_payload(value, settings)
        if input_error:
            return VisualUnderstandingResult(
                status="unsupported",
                model_name=model_name,
                error=input_error,
                source_kind=source_kind,
            )
        image_payloads.append(image_url)
        source_kinds.append(source_kind)
    if not image_payloads:
        return VisualUnderstandingResult(status="empty", model_name=model_name, error="No visual input provided")

    content: list[dict[str, Any]] = [{"type": "image_url", "image_url": {"url": url}} for url in image_payloads]
    content.append({"type": "text", "text": _visual_prompt(asset_role=asset_role, user_text=user_text)})
    try:
        response = httpx.post(
            _chat_completions_url(settings),
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": content}],
                "temperature": 0,
                "max_tokens": 1400,
                "response_format": {"type": "json_object"},
            },
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=settings.support_visual_understanding_timeout_seconds,
        )
        response.raise_for_status()
        text = _extract_chat_completion_text(response.json())
    except Exception as exc:
        return VisualUnderstandingResult(
            status="error",
            model_name=model_name,
            error=f"{type(exc).__name__}: {exc}",
            source_kind=",".join(sorted(set(source_kinds))),
        )

    fields = _parse_visual_fields(text)
    summary = _summary_from_fields(fields, fallback=text)
    if not summary:
        return VisualUnderstandingResult(
            status="empty",
            model_name=model_name,
            source_kind=",".join(sorted(set(source_kinds))),
        )
    return VisualUnderstandingResult(
        status="ok",
        summary=summary,
        fields=fields,
        model_name=model_name,
        source_kind=",".join(sorted(set(source_kinds))),
    )


def _fake_visual_understanding(*, asset_role: str, user_text: str, model_name: str) -> VisualUnderstandingResult:
    role = asset_role or "unknown"
    if role == "video":
        fields: dict[str, Any] = {
            "visible_product": "未知产品",
            "visible_part": "视频关键帧中的产品状态",
            "damage_type": "无法在离线假数据中确认",
            "damage_location": "未知",
            "severity": "unknown",
            "text_seen": "",
            "customer_claim_supported": "unknown",
            "confidence": 0.45,
            "recommended_follow_up": "请人工查看原视频或补充关键帧截图。",
        }
    else:
        fields = {
            "visible_product": "疑似售后产品",
            "visible_part": "外观/结构件",
            "damage_type": "疑似划痕或裂痕",
            "damage_location": "客户图片标注位置",
            "severity": "medium",
            "text_seen": "",
            "customer_claim_supported": "unknown",
            "confidence": 0.6,
            "recommended_follow_up": "请补充产品型号、订单号和更清晰的损伤细节照片。",
        }
    if user_text:
        fields["customer_claim_supported"] = "partial"
    return VisualUnderstandingResult(status="ok", summary=_summary_from_fields(fields), fields=fields, model_name=model_name)


def _image_url_payload(value: str, settings: Settings) -> tuple[str, str, str]:
    if value.startswith(("http://", "https://", "data:image/")):
        return value, "url", ""
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return "", "local_file", "Visual understanding input file does not exist"
    if path.stat().st_size > settings.support_visual_understanding_image_max_bytes:
        return "", "local_file", "Visual understanding input exceeds SUPPORT_VISUAL_UNDERSTANDING_IMAGE_MAX_BYTES"
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if not mime_type.startswith("image/"):
        return "", "local_file", f"Visual understanding input is not an image: {mime_type}"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}", "local_file", ""


def _chat_completions_url(settings: Settings) -> str:
    base_url = (
        settings.support_visual_understanding_base_url
        or settings.support_ocr_base_url
        or settings.bailian_embedding_base_url
    ).rstrip("/")
    return f"{base_url}/chat/completions"


def _extract_chat_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [item.get("text", "") for item in content if isinstance(item, dict)]
        return "\n".join(text for text in texts if text)
    return ""


def _parse_visual_fields(text: str) -> dict[str, Any]:
    payload = _extract_json_object(text)
    if not payload:
        return {}
    fields: dict[str, Any] = {}
    for key in VISUAL_FIELD_KEYS:
        value = payload.get(key)
        if value is None:
            continue
        if key == "confidence":
            fields[key] = _coerce_confidence(value)
        elif isinstance(value, (list, tuple)):
            fields[key] = "；".join(str(item).strip() for item in value if str(item).strip())[:1000]
        elif isinstance(value, dict):
            fields[key] = json.dumps(value, ensure_ascii=False)[:1000]
        else:
            fields[key] = str(value).strip()[:1000]
    return fields


def _extract_json_object(text: str) -> dict[str, Any]:
    clean = str(text or "").strip()
    if not clean:
        return {}
    clean = re.sub(r"^```(?:json)?\s*", "", clean)
    clean = re.sub(r"\s*```$", "", clean)
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if match:
        clean = match.group(0)
    try:
        payload = json.loads(clean)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _coerce_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(confidence, 1.0))


def _summary_from_fields(fields: dict[str, Any] | None, *, fallback: str = "") -> str:
    fields = fields or {}
    parts = []
    if fields.get("visible_product") or fields.get("visible_part"):
        parts.append(f"图片显示{fields.get('visible_product') or '产品'}的{fields.get('visible_part') or '局部'}")
    if fields.get("damage_type") or fields.get("damage_location"):
        parts.append(f"可见{fields.get('damage_location') or '局部'}存在{fields.get('damage_type') or '异常'}")
    if fields.get("text_seen"):
        parts.append(f"可读文字：{fields['text_seen']}")
    if fields.get("customer_claim_supported"):
        parts.append(f"与客户描述匹配度：{fields['customer_claim_supported']}")
    if fields.get("recommended_follow_up"):
        parts.append(f"建议补充：{fields['recommended_follow_up']}")
    summary = "；".join(part for part in parts if part).strip("；")
    if not summary:
        summary = str(fallback or "").strip()
    return summary[:1200]


def _visual_prompt(*, asset_role: str, user_text: str) -> str:
    return (
        "你是售后 Copilot 的视觉理解模块，只做图片/视频关键帧观察，不回答售后问题。"
        "请根据图片整理结构化视觉事实，禁止判断责任、禁止承诺退款/换新/补发/维修时效。"
        "如果无法确认，字段写 unknown。请只输出 JSON object，字段固定为："
        "visible_product, visible_part, damage_type, damage_location, severity, text_seen, "
        "customer_claim_supported, confidence, recommended_follow_up。"
        "severity 只能是 low/medium/high/unknown；confidence 是 0 到 1 的数字。"
        "customer_claim_supported 表示图片是否支持用户描述，只能是 yes/no/partial/unknown。"
        f"附件类型判断：{asset_role or 'unknown'}。"
        f"用户文本：{user_text or '无'}。"
    )
