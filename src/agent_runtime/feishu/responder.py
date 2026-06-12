from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_runtime.settings import Settings, get_settings


@dataclass(frozen=True)
class ReplyResult:
    status: str
    reply_message_id: str = ""
    raw_output: str = ""


def truncate_for_feishu(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[内容过长已截断]"
    safety_summary = _safety_summary(text)
    if not safety_summary:
        return text[: max(0, limit - len(marker))] + marker

    suffix = "\n\n[安全边界摘要]\n" + safety_summary + marker
    if len(suffix) >= limit:
        return suffix[: max(0, limit - len(marker))] + marker
    prefix_limit = max(0, limit - len(suffix))
    return text[:prefix_limit].rstrip() + suffix


def _safety_summary(text: str) -> str:
    sections = [
        _section_text(text, "正式依据"),
        _section_text(text, "历史参考"),
        _section_text(text, "工单草稿"),
    ]
    return "\n\n".join(section for section in sections if section)


def _section_text(text: str, field: str) -> str:
    start = _field_position(text, field)
    if start < 0:
        return ""
    next_positions = [
        position
        for position in (
            _field_position(text, "历史参考"),
            _field_position(text, "工单草稿"),
        )
        if position > start
    ]
    end = min(next_positions) if next_positions else len(text)
    return text[start:end].strip()


def _field_position(text: str, field: str) -> int:
    positions = [text.find(f"{field}："), text.find(f"{field}:")]
    found = [position for position in positions if position >= 0]
    return min(found) if found else -1


class FeishuResponder:
    async def reply_in_thread(
        self,
        source_message_id: str,
        text: str,
        idempotency_key: str,
    ) -> ReplyResult:
        raise NotImplementedError


class FeishuSdkResponder(FeishuResponder):
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        sdk: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._sdk = sdk

    async def reply_in_thread(
        self,
        source_message_id: str,
        text: str,
        idempotency_key: str,
    ) -> ReplyResult:
        sdk = self._sdk_module()
        client = self._sdk_client(sdk)
        body = (
            sdk.im.v1.ReplyMessageRequestBody.builder()
            .content(
                json.dumps(
                    {"text": truncate_for_feishu(text, self.settings.feishu_reply_max_chars)},
                    ensure_ascii=False,
                )
            )
            .msg_type("text")
            .reply_in_thread(True)
            .uuid(idempotency_key)
            .build()
        )
        request = (
            sdk.im.v1.ReplyMessageRequest.builder()
            .message_id(source_message_id)
            .request_body(body)
            .build()
        )
        response = await client.im.v1.message.areply(request)
        code = int(getattr(response, "code", 0) or 0)
        if code != 0:
            message = str(getattr(response, "msg", "") or "unknown error")
            raise RuntimeError(f"Feishu SDK reply failed: code={code} msg={message}")
        data = getattr(response, "data", None)
        reply_message_id = str(getattr(data, "message_id", "") or "")
        if not reply_message_id:
            raise RuntimeError("Feishu SDK reply returned no message_id")
        return ReplyResult(
            status="replied",
            reply_message_id=reply_message_id,
            raw_output=_response_raw_text(response),
        )

    def _sdk_module(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError("lark-oapi is required for Feishu SDK replies") from exc
        self._sdk = lark
        return lark

    def _sdk_client(self, sdk: Any) -> Any:
        if self._client is not None:
            return self._client
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret:
            raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required for Feishu SDK replies")
        self._client = (
            sdk.Client.builder()
            .app_id(self.settings.feishu_app_id)
            .app_secret(self.settings.feishu_app_secret)
            .log_level(sdk.LogLevel.ERROR)
            .build()
        )
        return self._client


def _response_raw_text(response: Any) -> str:
    raw = getattr(response, "raw", None)
    content = getattr(raw, "content", None)
    if isinstance(content, bytes):
        return content.decode(errors="ignore")
    if content is not None:
        return str(content)
    return ""
