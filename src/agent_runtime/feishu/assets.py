from __future__ import annotations

import json
import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

import httpx

from agent_runtime.copilot.case_context import SupportAsset, SupportCaseRequest
from agent_runtime.feishu.message_sender import FEISHU_BASE_URL, get_tenant_access_token
from agent_runtime.settings import Settings


logger = logging.getLogger(__name__)

FEISHU_DOWNLOAD_TYPES = {
    "image": "image",
    "video": "file",
    "audio": "file",
    "file": "file",
}


async def download_feishu_assets_for_request(
    request: SupportCaseRequest,
    settings: Settings,
) -> SupportCaseRequest:
    if not settings.feishu_asset_download_enabled or not request.assets:
        return request

    try:
        token = await get_tenant_access_token(settings)
    except Exception as exc:
        logger.warning("Failed to fetch Feishu token for asset download: %s", exc)
        return request.model_copy(
            update={
                "assets": [
                    _asset_with_download_error(asset, f"tenant token unavailable: {type(exc).__name__}")
                    for asset in request.assets
                ]
            }
        )

    cache_dir = Path(settings.feishu_asset_cache_dir)
    downloaded: list[SupportAsset] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for asset in request.assets:
            downloaded.append(await _download_asset(client, token, asset, cache_dir, settings))
    return request.model_copy(update={"assets": downloaded})


async def _download_asset(
    client: httpx.AsyncClient,
    token: str,
    asset: SupportAsset,
    cache_dir: Path,
    settings: Settings,
) -> SupportAsset:
    if asset.local_path or asset.url:
        return _asset_with_download_status(asset, "skipped", "asset already has local_path or url")
    if not asset.message_id or not asset.file_key:
        return _asset_with_download_error(asset, "missing message_id or file_key")

    resource_type = FEISHU_DOWNLOAD_TYPES.get(asset.media_type)
    if resource_type is None:
        return _asset_with_download_error(asset, f"unsupported media_type={asset.media_type}")

    target_dir = cache_dir / _safe_path_part(asset.message_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / _download_filename(asset, "")
    if target_path.exists() and target_path.stat().st_size > 0:
        return _asset_with_download_status(asset.model_copy(update={"local_path": str(target_path)}), "cached", "")

    url = (
        f"{FEISHU_BASE_URL}/im/v1/messages/{quote(asset.message_id, safe='')}"
        f"/resources/{quote(asset.file_key, safe='')}"
    )
    try:
        async with client.stream(
            "GET",
            url,
            params={"type": resource_type},
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if _is_json_response(response):
                body = await _read_limited_response(response, settings.feishu_asset_download_max_bytes)
                return _asset_with_download_error(asset, _json_error_message_from_bytes(response, body))
            response.raise_for_status()
            target_path = target_dir / _download_filename(asset, response.headers.get("content-type", ""))
            status = await _write_limited_response(response, target_path, settings.feishu_asset_download_max_bytes)
            if status:
                return _asset_with_download_error(asset, status)
    except Exception as exc:
        logger.warning(
            "Failed to download Feishu asset: message_id_hash=%s file_key_hash=%s error=%s",
            _short_hash(asset.message_id),
            _short_hash(asset.file_key),
            exc,
        )
        return _asset_with_download_error(asset, f"{type(exc).__name__}: {exc}")

    return _asset_with_download_status(
        asset.model_copy(
            update={
                "local_path": str(target_path),
                "mime_type": asset.mime_type or response.headers.get("content-type", "").split(";", 1)[0],
            }
        ),
        "ok",
        "",
    )


def _download_filename(asset: SupportAsset, content_type: str) -> str:
    name = _safe_path_part(asset.filename) if asset.filename else ""
    suffix = Path(name).suffix if name else ""
    if not name:
        name = _safe_path_part(asset.file_key) or "asset"
    if suffix:
        return name
    extension = _extension_for(asset, content_type)
    return f"{name}{extension}"


def _extension_for(asset: SupportAsset, content_type: str) -> str:
    mime_type = (asset.mime_type or content_type).split(";", 1)[0].strip().lower()
    if mime_type:
        extension = mimetypes.guess_extension(mime_type)
        if extension:
            return extension
    if asset.media_type == "image":
        return ".jpg"
    if asset.media_type == "video":
        return ".mp4"
    if asset.media_type == "audio":
        return ".mp3"
    return ".bin"


def _asset_with_download_status(asset: SupportAsset, status: str, error: str) -> SupportAsset:
    metadata = {**asset.metadata, "download_status": status}
    if error:
        metadata["download_error"] = error
    else:
        metadata.pop("download_error", None)
    return asset.model_copy(update={"metadata": metadata})


def _asset_with_download_error(asset: SupportAsset, error: str) -> SupportAsset:
    return _asset_with_download_status(asset, "error", error)


def _safe_path_part(value: str) -> str:
    text = value.strip() or "asset"
    text = text.replace("/", "_").replace("\\", "_")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    return text[:160] or "asset"


def _is_json_response(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return "application/json" in content_type


def _json_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Feishu resource API returned JSON status={response.status_code}"
    code = payload.get("code") if isinstance(payload, dict) else None
    msg = payload.get("msg") if isinstance(payload, dict) else None
    return f"Feishu resource API error code={code} msg={msg}"


async def _read_limited_response(response: httpx.Response, max_bytes: int) -> bytes:
    chunks = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            break
        chunks.append(chunk)
    return b"".join(chunks)


async def _write_limited_response(response: httpx.Response, target_path: Path, max_bytes: int) -> str:
    size = 0
    try:
        with target_path.open("wb") as handle:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    handle.close()
                    target_path.unlink(missing_ok=True)
                    return "resource exceeds FEISHU_ASSET_DOWNLOAD_MAX_BYTES"
                handle.write(chunk)
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    return ""


def _json_error_message_from_bytes(response: httpx.Response, body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return f"Feishu resource API returned JSON status={response.status_code}"
    code = payload.get("code") if isinstance(payload, dict) else None
    msg = payload.get("msg") if isinstance(payload, dict) else None
    return f"Feishu resource API error code={code} msg={msg}"


def _short_hash(value: str) -> str:
    import hashlib

    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12] if value else ""
