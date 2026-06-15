from __future__ import annotations

import json
import re
from typing import Any

from agent_runtime.copilot.case_context import SupportAsset, SupportCaseRequest
from agent_runtime.copilot.evidence import short_hash
from agent_runtime.feishu.events import FeishuMessageEvent, effective_thread_id, session_id_for_event
from agent_runtime.feishu.parser import strip_trigger_prefix
from agent_runtime.settings import Settings


def build_support_case_request_from_event(event: FeishuMessageEvent, settings: Settings) -> SupportCaseRequest:
    user_text = build_feishu_user_text(event, settings)
    thread_id = effective_thread_id(event)
    return SupportCaseRequest(
        request_id=f"feishu:{short_hash(event.message_id or event.event_id)}",
        source="feishu",
        channel="legacy_feishu",
        source_platform="feishu",
        user_text=user_text,
        assets=extract_assets_from_event(event),
        chat_id=event.chat_id,
        thread_id=thread_id,
        message_id=event.message_id,
        sender_id=event.sender_id,
        session_id=session_id_for_event(event),
        trace_group_id=f"feishu:{short_hash(event.chat_id)}:thread:{short_hash(thread_id)}",
        metadata={
            "message_type": event.message_type,
            "chat_type": event.chat_type,
            "root_id": event.root_id,
            "parent_id": event.parent_id,
        },
    )


def build_feishu_user_text(event: FeishuMessageEvent, settings: Settings) -> str:
    text = event.content.strip()
    if settings.feishu_bot_mention_name:
        text = text.replace(f"@{settings.feishu_bot_mention_name}", "").replace(settings.feishu_bot_mention_name, "")
    for mention_name in event.mention_names:
        if mention_name:
            text = text.replace(f"@{mention_name}", "").replace(mention_name, "")
    text = re.sub(r"@_user_\d+", "", text)
    text = strip_trigger_prefix(text, settings.support_agent_trigger_prefix)
    return text.strip() or event.content.strip()


def extract_assets_from_event(event: FeishuMessageEvent) -> list[SupportAsset]:
    content = _content_object(event.raw_content)
    assets: list[SupportAsset] = []
    if event.message_type in {"image", "video", "file", "audio"}:
        asset = _asset_from_message_type(event, content)
        if asset is not None:
            assets.append(asset)
    assets.extend(_assets_from_rich_content(event, content))
    return _dedupe_assets(assets)


def _asset_from_message_type(event: FeishuMessageEvent, content: dict[str, Any]) -> SupportAsset | None:
    media_type = _media_type_from_message_type(event.message_type)
    key = _first_present(content, ("image_key", "file_key", "video_key", "media_key", "audio_key"))
    if not key and event.message_type not in {"image", "video", "file", "audio"}:
        return None
    return SupportAsset(
        asset_id=f"{event.message_id}:{event.message_type}:{key or 'asset'}",
        media_type=media_type,
        source="feishu",
        filename=str(content.get("file_name") or content.get("name") or ""),
        mime_type=str(content.get("mime_type") or ""),
        file_key=key,
        message_id=event.message_id,
        url=str(content.get("url") or ""),
        metadata={"feishu_message_type": event.message_type},
    )


def _assets_from_rich_content(event: FeishuMessageEvent, content: dict[str, Any]) -> list[SupportAsset]:
    blocks = content.get("content")
    if not isinstance(blocks, list):
        return []
    assets: list[SupportAsset] = []
    for block_index, block in enumerate(blocks):
        if not isinstance(block, list):
            continue
        for item_index, item in enumerate(block):
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").lower()
            if tag not in {"img", "image", "media", "video", "file"}:
                continue
            key = _first_present(item, ("image_key", "file_key", "media_key", "video_key"))
            media_type = "video" if tag == "video" else "file" if tag == "file" else "image"
            assets.append(
                SupportAsset(
                    asset_id=f"{event.message_id}:rich:{block_index}:{item_index}:{key or tag}",
                    media_type=media_type,
                    source="feishu",
                    filename=str(item.get("file_name") or item.get("name") or ""),
                    mime_type=str(item.get("mime_type") or ""),
                    file_key=key,
                    message_id=event.message_id,
                    url=str(item.get("url") or ""),
                    metadata={"feishu_message_type": event.message_type, "rich_tag": tag},
                )
            )
    return assets


def _dedupe_assets(assets: list[SupportAsset]) -> list[SupportAsset]:
    output: list[SupportAsset] = []
    seen: set[str] = set()
    for asset in assets:
        if asset.asset_id in seen:
            continue
        seen.add(asset.asset_id)
        output.append(asset)
    return output


def _content_object(raw_content: object) -> dict[str, Any]:
    if isinstance(raw_content, dict):
        return raw_content
    if isinstance(raw_content, str):
        try:
            payload = json.loads(raw_content)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _media_type_from_message_type(message_type: str) -> str:
    if message_type in {"image", "video", "audio", "file"}:
        return message_type
    return "unknown"


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return ""
