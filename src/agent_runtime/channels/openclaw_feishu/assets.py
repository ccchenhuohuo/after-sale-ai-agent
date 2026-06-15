from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_runtime.copilot.case_context import SupportAsset


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
        "key",
    )
    file_key = _first_value(resource, "file_key", "fileKey", "image_key", "imageKey", "media_key", "mediaKey", "key")
    filename = _first_value(resource, "filename", "fileName", "name")
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
            "openclaw_resource_type": _first_value(resource, "type", "kind", "mediaType"),
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
    explicit = _first_value(resource, "media_type", "mediaType", "kind", "type")
    normalized = explicit.lower()
    if normalized in {"image", "video", "audio", "file", "text"}:
        return normalized
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
        return f"{message_id or 'openclaw'}:{media_type}:{resource_id}"
    return f"{message_id or 'openclaw'}:{media_type}:{index}"


def _first_value(resource: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = resource.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
