from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_runtime.copilot.case_context import SupportAsset
from agent_runtime.copilot.evidence import short_hash


def support_assets_from_openclaw_payload(
    payload: Mapping[str, Any],
    *,
    message_id: str = "",
) -> list[SupportAsset]:
    assets = support_assets_from_openclaw_resources(
        _first_present(payload, "resources", "Resources"),
        message_id=message_id,
    )
    assets.extend(
        _support_assets_from_media_payload(
            payload,
            message_id=message_id,
            start_index=len(assets),
        )
    )
    return assets


def support_asset_from_openclaw_resource(
    resource: Mapping[str, Any],
    *,
    message_id: str = "",
    index: int = 0,
) -> SupportAsset:
    media_type = _media_type_for_resource(resource)
    resource_id = _first_value(
        resource,
        "asset_id",
        "id",
        "resource_id",
        "resourceId",
        "file_key",
        "fileKey",
        "image_key",
        "imageKey",
        "media_key",
        "mediaKey",
        "placeholder",
        "key",
    )
    file_key = _first_value(resource, "file_key", "fileKey", "image_key", "imageKey", "media_key", "mediaKey", "key")
    filename = _first_value(resource, "filename", "fileName", "name", "placeholder")
    mime_type = _first_value(resource, "mime_type", "mimeType", "mime", "contentType")
    status = _first_value(resource, "status", "download_status", "downloadStatus")
    error = _first_value(resource, "error", "download_error", "downloadError")
    return SupportAsset(
        asset_id=_asset_id(message_id, media_type, resource_id, index),
        media_type=media_type,
        source="openclaw_feishu",
        filename=filename,
        mime_type=mime_type,
        file_key=file_key,
        message_id=message_id,
        url=_first_value(resource, "url", "downloadUrl", "download_url"),
        local_path=_first_value(resource, "local_path", "localPath", "path", "filePath"),
        metadata={
            "source_type": "openclaw_feishu",
            "asset_role": _first_value(resource, "asset_role", "assetRole", "role"),
            "description": _first_value(resource, "description", "summary", "caption", "alt"),
            "feishu_message_type": _first_value(resource, "message_type", "messageType"),
            "openclaw_resource_type": _first_value(resource, "type", "kind", "mediaType", "resourceType"),
            "download_status": status,
            "download_error": error,
        },
    )


def support_assets_from_openclaw_resources(
    resources: object,
    *,
    message_id: str = "",
) -> list[SupportAsset]:
    if not isinstance(resources, list):
        return []
    assets: list[SupportAsset] = []
    for index, resource in enumerate(resources):
        if not isinstance(resource, Mapping):
            continue
        assets.append(support_asset_from_openclaw_resource(resource, message_id=message_id, index=index))
    return assets


def _media_type_for_resource(resource: Mapping[str, Any]) -> str:
    explicit = _first_value(resource, "media_type", "mediaType", "kind", "type", "resourceType")
    normalized = explicit.lower()
    if normalized == "sticker":
        return "image"
    if normalized in {"image", "video", "audio", "file", "text"}:
        return normalized
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "audio"
    mime_type = _first_value(resource, "mime_type", "mimeType", "mime", "contentType").lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return "file" if explicit or mime_type else "unknown"


def _asset_id(message_id: str, media_type: str, resource_id: str, index: int) -> str:
    if resource_id:
        return f"openclaw_asset:{short_hash(message_id or 'openclaw')}:{media_type}:{short_hash(resource_id)}"
    return f"openclaw_asset:{short_hash(message_id or 'openclaw')}:{media_type}:{index}"


def _support_assets_from_media_payload(
    payload: Mapping[str, Any],
    *,
    message_id: str,
    start_index: int,
) -> list[SupportAsset]:
    paths = _string_list(payload.get("MediaPaths")) or _single_as_list(payload.get("MediaPath"))
    urls = _string_list(payload.get("MediaUrls")) or _single_as_list(payload.get("MediaUrl"))
    types = _string_list(payload.get("MediaTypes")) or _single_as_list(payload.get("MediaType"))
    count = max(len(paths), len(urls), len(types))
    assets: list[SupportAsset] = []
    for offset in range(count):
        local_path = paths[offset] if offset < len(paths) else ""
        url = urls[offset] if offset < len(urls) else ""
        media_type = types[offset] if offset < len(types) else ""
        resource = {
            "contentType": media_type,
            "path": local_path,
            "url": url,
            "status": "ok" if local_path or url else "",
            "description": "OpenClaw resolved media payload",
        }
        assets.append(
            support_asset_from_openclaw_resource(
                resource,
                message_id=message_id,
                index=start_index + offset,
            )
        )
    return assets


def _first_present(resource: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in resource:
            return resource[key]
    return None


def _single_as_list(value: object) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _first_value(resource: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = resource.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
