from __future__ import annotations

import ipaddress
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from agent_runtime.copilot.case_context import SupportAsset
from agent_runtime.settings import Settings


ROOT = Path(__file__).resolve().parents[3]
AssetInputKind = Literal["image", "video"]


@dataclass(frozen=True)
class AssetInputValidationResult:
    ok: bool
    value: str = ""
    source_kind: Literal["local_file", "url", ""] = ""
    error: str = ""


def validate_support_asset_input(
    asset: SupportAsset,
    settings: Settings,
    *,
    expected_kind: AssetInputKind,
    allow_url: bool = True,
    local_only: bool = False,
) -> AssetInputValidationResult:
    local_error = ""
    if asset.local_path:
        local_result = _validate_local_path(asset.local_path, asset, settings, expected_kind)
        if local_result.ok:
            return local_result
        local_error = local_result.error

    if local_only:
        if asset.url:
            return AssetInputValidationResult(
                ok=False,
                error="远程 URL 暂不支持当前附件处理链路。",
            )
        return AssetInputValidationResult(ok=False, error=local_error or "附件缺少可用的本地文件。")

    if allow_url and asset.url:
        url_result = _validate_url(asset.url, settings)
        if url_result.ok:
            return url_result
        return AssetInputValidationResult(ok=False, error=local_error or url_result.error)

    return AssetInputValidationResult(ok=False, error=local_error or "附件缺少可用的本地文件或白名单 URL。")


def allowed_local_dirs(settings: Settings) -> list[Path]:
    dirs = [_resolve_path(settings.feishu_asset_cache_dir)]
    dirs.extend(_resolve_path(part) for part in _split_config_values(settings.support_asset_allowed_local_dirs))
    unique_dirs: list[Path] = []
    seen = set()
    for directory in dirs:
        key = str(directory)
        if key and key not in seen:
            unique_dirs.append(directory)
            seen.add(key)
    return unique_dirs


def _validate_local_path(
    raw_path: str,
    asset: SupportAsset,
    settings: Settings,
    expected_kind: AssetInputKind,
) -> AssetInputValidationResult:
    path = _resolve_path(raw_path)
    if not _is_within_allowed_dirs(path, allowed_local_dirs(settings)):
        return AssetInputValidationResult(ok=False, error="附件本地文件不在允许的缓存目录内。")
    if not path.exists():
        return AssetInputValidationResult(ok=False, error="附件本地文件不存在。")
    if not path.is_file():
        return AssetInputValidationResult(ok=False, error="附件本地路径不是普通文件。")
    try:
        size = path.stat().st_size
    except OSError:
        return AssetInputValidationResult(ok=False, error="无法读取附件本地文件信息。")
    if size > settings.support_asset_input_max_bytes:
        return AssetInputValidationResult(ok=False, error="附件本地文件超过允许大小。")
    if not _matches_expected_media(path.name, asset.mime_type, expected_kind):
        return AssetInputValidationResult(ok=False, error=f"附件本地文件不是可处理的 {expected_kind} 类型。")
    return AssetInputValidationResult(ok=True, value=str(path), source_kind="local_file")


def _validate_url(raw_url: str, settings: Settings) -> AssetInputValidationResult:
    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").strip().lower()
    if parsed.scheme.lower() != "https" or not hostname:
        return AssetInputValidationResult(ok=False, error="附件 URL 必须是白名单 HTTPS 地址。")
    if _is_forbidden_host(hostname):
        return AssetInputValidationResult(ok=False, error="附件 URL 指向不允许的主机。")
    allowed_hosts = _split_config_values(settings.support_asset_allowed_url_hosts)
    if not _host_allowed(hostname, allowed_hosts):
        return AssetInputValidationResult(ok=False, error="附件 URL 主机未在允许列表内。")
    return AssetInputValidationResult(ok=True, value=raw_url, source_kind="url")


def _resolve_path(value: str) -> Path:
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _split_config_values(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[,;\n]+", value)
    return [part.strip() for part in parts if part.strip()]


def _is_within_allowed_dirs(path: Path, dirs: list[Path]) -> bool:
    return any(_is_relative_to(path, directory) for directory in dirs)


def _is_relative_to(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _matches_expected_media(filename: str, mime_type: str, expected_kind: AssetInputKind) -> bool:
    guessed = (mime_type or mimetypes.guess_type(filename)[0] or "").split(";", 1)[0].lower()
    return guessed.startswith(f"{expected_kind}/")


def _is_forbidden_host(hostname: str) -> bool:
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _host_allowed(hostname: str, allowed_hosts: list[str]) -> bool:
    for allowed in allowed_hosts:
        host = allowed.lower()
        if host.startswith("*.") and hostname.endswith(host[1:]):
            return True
        if hostname == host:
            return True
    return False
