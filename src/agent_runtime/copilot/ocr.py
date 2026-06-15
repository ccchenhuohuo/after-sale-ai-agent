from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from agent_runtime.settings import Settings


ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class OcrProviderResult:
    status: str
    text: str = ""
    model_name: str = ""
    error: str = ""
    source_kind: str = ""


def extract_ocr_text(asset_path_or_url: str, settings: Settings) -> OcrProviderResult:
    provider = settings.support_ocr_provider.strip().lower()
    if provider in {"", "disabled", "none"}:
        return OcrProviderResult(status="unsupported", model_name=provider or "disabled", error="OCR provider disabled")
    if provider not in {"bailian_vl", "dashscope_vl", "qwen_vl"}:
        return OcrProviderResult(
            status="unsupported",
            model_name=settings.support_ocr_model,
            error=f"Unsupported OCR provider: {settings.support_ocr_provider}",
        )
    return _extract_with_bailian_vl(asset_path_or_url, settings)


def _extract_with_bailian_vl(asset_path_or_url: str, settings: Settings) -> OcrProviderResult:
    api_key = settings.resolved_bailian_api_key
    model_name = settings.support_ocr_model or "qwen-vl-plus"
    if not api_key:
        return OcrProviderResult(status="unsupported", model_name=model_name, error="Bailian/DashScope API key not configured")

    image_url, source_kind, input_error = _image_url_payload(asset_path_or_url, settings)
    if input_error:
        return OcrProviderResult(status="unsupported", model_name=model_name, error=input_error, source_kind=source_kind)

    endpoint = _chat_completions_url(settings)
    try:
        response = httpx.post(
            endpoint,
            json={
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_url}},
                            {"type": "text", "text": _ocr_prompt()},
                        ],
                    }
                ],
                "temperature": 0,
                "max_tokens": 1200,
            },
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=settings.support_ocr_timeout_seconds,
        )
        response.raise_for_status()
        text = _extract_chat_completion_text(response.json())
    except Exception as exc:
        return OcrProviderResult(
            status="error",
            model_name=model_name,
            error=f"{type(exc).__name__}: {exc}",
            source_kind=source_kind,
        )

    normalized = _clean_ocr_text(text)
    if not normalized:
        return OcrProviderResult(status="empty", model_name=model_name, source_kind=source_kind)
    return OcrProviderResult(status="ok", text=normalized, model_name=model_name, source_kind=source_kind)


def _image_url_payload(value: str, settings: Settings) -> tuple[str, str, str]:
    if value.startswith(("http://", "https://", "data:image/")):
        return value, "url", ""
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return "", "local_file", "OCR input file does not exist"
    if path.stat().st_size > settings.support_ocr_image_max_bytes:
        return "", "local_file", "OCR input exceeds SUPPORT_OCR_IMAGE_MAX_BYTES"
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if not mime_type.startswith("image/"):
        return "", "local_file", f"OCR input is not an image: {mime_type}"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}", "local_file", ""


def _chat_completions_url(settings: Settings) -> str:
    base_url = (settings.support_ocr_base_url or settings.bailian_embedding_base_url).rstrip("/")
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


def _clean_ocr_text(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines()]
    clean = "\n".join(line for line in lines if line)
    if clean.strip().upper() == "NO_TEXT_DETECTED":
        return ""
    return clean[:4000]


def _ocr_prompt() -> str:
    return (
        "请只输出可进入售后分析上下文的文字概述，不要回答售后问题。"
        "优先保留客户原话、产品型号/SKU、故障现象、诉求、订单或图片中的关键字段。"
        "如果是聊天截图，忽略截图中的客服回复、AI助手回复、按钮、导航、水印和重复 UI 文本，"
        "不要转写完整聊天记录，不要输出客服话术，不要包含退款/换新/补发/处理时效等承诺性建议。"
        "如果是铭牌、发票、报错截图或说明文字图，保留可读的关键字段和值。"
        "如果没有可用于售后分析的可读文字，只输出：NO_TEXT_DETECTED。"
    )
