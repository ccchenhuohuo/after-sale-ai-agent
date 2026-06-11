#!/usr/bin/env python3
"""Ingest Feishu support chats into the existing after-sales Base.

The script keeps raw messages, event candidates, media rows, and action logs
separate so the same data can later feed RAG without losing source traceability.

This is an ops ingestion utility, not a runtime Agent tool. It defaults to
dry-run mode; pass --apply explicitly before any Feishu Base or Drive writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

BASE_TOKEN = ""
GROUP_CHAT_ID = ""
LUZ_CHAT_ID = ""

TABLES = {
    "events": "",
    "raw_messages": "",
    "media": "",
    "actions": "",
}

IMAGE_KEY_RE = re.compile(r"img_[A-Za-z0-9_~.-]+")
FILE_KEY_RE = re.compile(r"file_[A-Za-z0-9_~.-]+")
MODEL_RE = re.compile(r"\b[A-Za-z]\d{2,4}[A-Za-z0-9-]*\b")

ISSUE_KEYWORDS = [
    "质量",
    "售后",
    "发错",
    "错发",
    "漏发",
    "补发",
    "退货",
    "退款",
    "换新",
    "故障",
    "异常",
    "坏",
    "无法",
    "不能",
    "不亮",
    "不响",
    "断",
    "充电",
    "发热",
    "冒烟",
    "异响",
    "白印",
    "卡",
    "兼容",
    "连接",
    "绑定",
    "速度",
    "客户",
]


@dataclass
class Source:
    name: str
    chat_id: str


@dataclass(frozen=True)
class IngestConfig:
    base_token: str
    group_chat_id: str
    luz_chat_id: str
    table_events: str
    table_raw_messages: str
    table_media: str
    table_actions: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "IngestConfig":
        return cls(
            base_token=args.base_token or os.getenv("FEISHU_SUPPORT_BASE_TOKEN", ""),
            group_chat_id=args.group_chat_id or os.getenv("FEISHU_SUPPORT_GROUP_CHAT_ID", ""),
            luz_chat_id=args.luz_chat_id or os.getenv("FEISHU_SUPPORT_LUZ_CHAT_ID", ""),
            table_events=args.table_events or os.getenv("FEISHU_SUPPORT_TABLE_EVENTS", ""),
            table_raw_messages=args.table_raw_messages or os.getenv("FEISHU_SUPPORT_TABLE_RAW_MESSAGES", ""),
            table_media=args.table_media or os.getenv("FEISHU_SUPPORT_TABLE_MEDIA", ""),
            table_actions=args.table_actions or os.getenv("FEISHU_SUPPORT_TABLE_ACTIONS", ""),
        )

    def missing_source_fields(self) -> list[str]:
        missing = []
        if not self.group_chat_id:
            missing.append("FEISHU_SUPPORT_GROUP_CHAT_ID")
        if not self.luz_chat_id:
            missing.append("FEISHU_SUPPORT_LUZ_CHAT_ID")
        return missing

    def missing_write_fields(self) -> list[str]:
        required = {
            "FEISHU_SUPPORT_BASE_TOKEN": self.base_token,
            "FEISHU_SUPPORT_TABLE_EVENTS": self.table_events,
            "FEISHU_SUPPORT_TABLE_RAW_MESSAGES": self.table_raw_messages,
            "FEISHU_SUPPORT_TABLE_MEDIA": self.table_media,
            "FEISHU_SUPPORT_TABLE_ACTIONS": self.table_actions,
        }
        return [name for name, value in required.items() if not value]

    def masked_base_token(self) -> str:
        return f"...{self.base_token[-6:]}" if self.base_token else "(未配置)"


def configure_targets(config: IngestConfig) -> None:
    global BASE_TOKEN, GROUP_CHAT_ID, LUZ_CHAT_ID, TABLES
    BASE_TOKEN = config.base_token
    GROUP_CHAT_ID = config.group_chat_id
    LUZ_CHAT_ID = config.luz_chat_id
    TABLES = {
        "events": config.table_events,
        "raw_messages": config.table_raw_messages,
        "media": config.table_media,
        "actions": config.table_actions,
    }


def print_target_summary(config: IngestConfig, args: argparse.Namespace) -> None:
    print(
        "Feishu ingest target summary:\n"
        f"- apply: {args.apply}\n"
        f"- sync_drive_images: {args.sync_drive_images}\n"
        f"- window: {args.start} -> {args.end}\n"
        f"- base_token: {config.masked_base_token()}\n"
        f"- group_chat_id: {config.group_chat_id or '(未配置)'}\n"
        f"- luz_chat_id: {config.luz_chat_id or '(未配置)'}\n"
        f"- table_events: {config.table_events or '(未配置)'}\n"
        f"- table_raw_messages: {config.table_raw_messages or '(未配置)'}\n"
        f"- table_media: {config.table_media or '(未配置)'}\n"
        f"- table_actions: {config.table_actions or '(未配置)'}",
        flush=True,
    )


def ensure_config(config: IngestConfig, *, apply: bool, sync_drive_images: bool) -> None:
    missing = config.missing_source_fields()
    if missing and not sync_drive_images:
        raise SystemExit("Missing Feishu source config: " + ", ".join(missing))
    if apply:
        missing_write = config.missing_write_fields()
        if missing_write:
            raise SystemExit("Missing Feishu write config for --apply: " + ", ".join(missing_write))


def summarize_drive_image_sync_plan(run_dir: Path) -> Dict[str, Any]:
    if not run_dir.exists():
        raise RuntimeError(f"Run directory does not exist: {run_dir}")
    items = list(iter_run_media_items(run_dir))
    queued = 0
    missing_local = 0
    for item in items:
        file_key = str(item.get("file_key") or "")
        if not file_key:
            continue
        if local_image_path(run_dir, file_key):
            queued += 1
        else:
            missing_local += 1
    return {
        "run_dir": str(run_dir),
        "apply": False,
        "total_run_media": len(items),
        "local_images_available": queued,
        "missing_local": missing_local,
        "note": "Dry run only. Pass --apply to upload files and patch media records.",
    }


def run_json(args: List[str], *, cwd: Path = ROOT) -> Dict[str, Any]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"$ {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON from {' '.join(args)}, got:\n{result.stdout}") from exc


def run(args: List[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"$ {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def normalize_datetime(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", value):
        return f"{value}:00"
    return value


def compact_text(value: str, limit: int = 8000) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[: limit - 20]}... [truncated]"


def stable_id(prefix: str, *parts: str, date_hint: str = "") -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]
    if date_hint:
        return f"{prefix}-{date_hint.replace('-', '')}-{digest}"
    return f"{prefix}-{digest}"


def extract_resource_keys(message: Dict[str, Any]) -> Dict[str, List[str]]:
    content = str(message.get("content") or "")
    images = set(IMAGE_KEY_RE.findall(content))
    files = set(FILE_KEY_RE.findall(content))
    if message.get("msg_type") == "image":
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and isinstance(parsed.get("image_key"), str):
                images.add(parsed["image_key"])
        except json.JSONDecodeError:
            pass
    return {"images": sorted(images), "files": sorted(files)}


def fetch_messages(source: Source, start: str, end: str) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    page_token = ""
    page = 0
    while True:
        page += 1
        args = [
            "lark-cli",
            "im",
            "+chat-messages-list",
            "--as",
            "user",
            "--chat-id",
            source.chat_id,
            "--start",
            start,
            "--end",
            end,
            "--sort",
            "asc",
            "--page-size",
            "50",
            "--format",
            "json",
        ]
        if page_token:
            args.extend(["--page-token", page_token])
        payload = run_json(args)
        batch = payload.get("data", {}).get("messages", [])
        for message in batch:
            message["source_name"] = source.name
            messages.append(message)
        print(f"Fetched {source.name} page {page}: +{len(batch)} messages", flush=True)
        if not payload.get("data", {}).get("has_more"):
            break
        page_token = payload.get("data", {}).get("page_token") or ""
        if not page_token:
            break
    return messages


def load_existing_records(table_id: str, key_field: str) -> Dict[str, str]:
    existing: Dict[str, str] = {}
    offset = 0
    while True:
        payload = run_json(
            [
                "lark-cli",
                "base",
                "+record-list",
                "--as",
                "user",
                "--base-token",
                BASE_TOKEN,
                "--table-id",
                table_id,
                "--field-id",
                key_field,
                "--offset",
                str(offset),
                "--limit",
                "200",
                "--format",
                "json",
            ]
        )
        data = payload.get("data", {})
        rows = data.get("data", [])
        record_ids = data.get("record_id_list", [])
        for row, record_id in zip(rows, record_ids):
            if row and row[0]:
                existing[str(row[0])] = record_id
        if not data.get("has_more") or not rows:
            break
        offset += len(rows)
    return existing


def load_record_rows(table_id: str, fields: List[str]) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    offset = 0
    while True:
        args = [
            "lark-cli",
            "base",
            "+record-list",
            "--as",
            "user",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            table_id,
            "--offset",
            str(offset),
            "--limit",
            "200",
            "--format",
            "json",
        ]
        for field in fields:
            args.extend(["--field-id", field])
        payload = run_json(args)
        data = payload.get("data", {})
        rows = data.get("data", [])
        record_ids = data.get("record_id_list", [])
        for row, record_id in zip(rows, record_ids):
            records[record_id] = dict(zip(fields, row))
        if not data.get("has_more") or not rows:
            break
        offset += len(rows)
    return records


def batch_create(table_id: str, fields: List[str], rows: List[List[Any]], out_dir: Path, name: str) -> List[str]:
    if not rows:
        return []
    record_ids: List[str] = []
    for index in range(0, len(rows), 200):
        chunk = rows[index : index + 200]
        payload_path = out_dir / f"{name}-{index // 200 + 1}.json"
        payload_path.write_text(json.dumps({"fields": fields, "rows": chunk}, ensure_ascii=False), encoding="utf-8")
        response = run_json(
            [
                "lark-cli",
                "base",
                "+record-batch-create",
                "--as",
                "user",
                "--base-token",
                BASE_TOKEN,
                "--table-id",
                table_id,
                "--json",
                f"@{payload_path.relative_to(ROOT)}",
            ]
        )
        record_ids.extend(response.get("data", {}).get("record_id_list", []))
    return record_ids


def raw_message_row(message: Dict[str, Any]) -> List[Any]:
    sender = message.get("sender") or {}
    resource_keys = extract_resource_keys(message)
    mentions = message.get("mentions") or []
    raw_json = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return [
        message.get("msg_type") or "text",
        normalize_datetime(message.get("create_time") or ""),
        sender.get("id", ""),
        compact_text(raw_json, 8000),
        message.get("message_id", ""),
        str(message.get("message_position") or ""),
        message.get("reply_to", ""),
        json.dumps(mentions, ensure_ascii=False),
        message.get("chat_id", ""),
        message.get("message_app_link", ""),
        compact_text(str(message.get("content") or ""), 8000),
        json.dumps(resource_keys, ensure_ascii=False),
        sender.get("name", ""),
        message.get("thread_id", ""),
    ]


def is_event_candidate(message: Dict[str, Any]) -> bool:
    if message.get("deleted"):
        return False
    if message.get("reply_to"):
        return False
    text = str(message.get("content") or "")
    if not text.strip():
        return False
    if MODEL_RE.search(text):
        return True
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in ISSUE_KEYWORDS)


def event_row(message: Dict[str, Any], raw_record_id: str) -> tuple[str, List[Any]]:
    text = str(message.get("content") or "")
    sender = message.get("sender") or {}
    created = normalize_datetime(message.get("create_time") or "")
    event_id = stable_id("EVT", message.get("message_id", ""), date_hint=created[:10])
    summary = compact_text(text, 120)
    return event_id, [
        created[:10],
        [{"id": raw_record_id}],
        "其他",
        "",
        summary,
        message.get("message_app_link", ""),
        [{"id": sender["id"]}] if sender.get("id") else None,
        "",
        compact_text(text, 2000),
        "待人工复核",
        True,
        0,
        summary,
        "待人工复核；当前终端测试版本不生成处理建议。",
        created,
        created,
        event_id,
    ]


def action_type(text: str) -> str:
    if any(word in text for word in ("换新", "更换")):
        return "换新"
    if "补发" in text:
        return "补发配件"
    if any(word in text for word in ("退款", "退货")):
        return "退款退货"
    if any(word in text for word in ("排查", "测试", "确认", "提供", "发视频", "拍")):
        return "客户排查"
    if any(word in text for word in ("？", "吗", "是否", "有没有", "什么")):
        return "追问"
    return "工程判断"


def action_row(message: Dict[str, Any], event_record_id: str) -> List[Any]:
    sender = message.get("sender") or {}
    return [
        compact_text(str(message.get("content") or ""), 2000),
        action_type(str(message.get("content") or "")),
        message.get("message_app_link", ""),
        normalize_datetime(message.get("create_time") or ""),
        message.get("message_id", ""),
        [{"id": event_record_id}],
        [{"id": sender["id"]}] if sender.get("id") else None,
    ]


def download_image(message_id: str, file_key: str, out_dir: Path) -> Optional[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    output_prefix = out_dir / file_key
    try:
        run(
            [
                "lark-cli",
                "im",
                "+messages-resources-download",
                "--as",
                "user",
                "--message-id",
                message_id,
                "--file-key",
                file_key,
                "--type",
                "image",
                "--output",
                str(output_prefix.relative_to(ROOT)),
            ]
        )
    except RuntimeError:
        return None
    candidates = sorted(out_dir.glob(f"{file_key}*"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else output_prefix if output_prefix.exists() else None


def upload_attachment(record_id: str, file_path: Path) -> bool:
    try:
        payload = run_json(
            [
                "lark-cli",
                "base",
                "+record-upload-attachment",
                "--as",
                "user",
                "--base-token",
                BASE_TOKEN,
                "--table-id",
                TABLES["media"],
                "--record-id",
                record_id,
                "--field-id",
                "附件",
                "--file",
                str(file_path.relative_to(ROOT)),
            ]
        )
        ignored = payload.get("data", {}).get("ignored_fields", [])
        return not ignored
    except RuntimeError:
        return False


def create_drive_folder(name: str) -> Dict[str, str]:
    payload = run_json(["lark-cli", "drive", "+create-folder", "--as", "user", "--name", name])
    data = payload.get("data", {})
    folder_token = data.get("folder_token") or data.get("token") or ""
    if not folder_token:
        raise RuntimeError(f"Could not read folder token from Drive response: {payload}")
    return {"folder_token": folder_token, "url": data.get("url", ""), "name": data.get("name", name)}


def upload_drive_file(file_path: Path, folder_token: str, name: str) -> Dict[str, str]:
    payload = run_json(
        [
            "lark-cli",
            "drive",
            "+upload",
            "--as",
            "user",
            "--folder-token",
            folder_token,
            "--file",
            str(file_path.relative_to(ROOT)),
            "--name",
            name,
        ]
    )
    data = payload.get("data", {})
    file_token = data.get("file_token") or data.get("token") or ""
    if not file_token:
        raise RuntimeError(f"Could not read file token from Drive response: {payload}")
    return {"file_token": file_token, "url": data.get("url", ""), "file_name": data.get("file_name", name)}


def update_media_archive(record_id: str, file_token: str, file_url: str) -> None:
    patch = {
        "分析状态": "已归档",
        "归档文件Token": file_token,
        "归档文件链接": file_url,
        "关键证据": "图片已上传至飞书云盘归档；原消息链接和资源键保留在本记录，后续可作为AI检索证据来源。",
    }
    run_json(
        [
            "lark-cli",
            "base",
            "+record-upsert",
            "--as",
            "user",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            TABLES["media"],
            "--record-id",
            record_id,
            "--json",
            json.dumps(patch, ensure_ascii=False),
        ]
    )


def iter_run_media_items(run_dir: Path) -> Iterable[Dict[str, Any]]:
    for payload_path in sorted(run_dir.glob("media-*.json")):
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        fields = payload.get("fields", [])
        for row in payload.get("rows", []):
            item = dict(zip(fields, row))
            item["_payload_path"] = str(payload_path)
            yield item


def local_image_path(run_dir: Path, file_key: str) -> Optional[Path]:
    image_dir = run_dir / "images"
    candidates = sorted(image_dir.glob(f"{file_key}*"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def has_cell_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def sync_drive_images(run_dir: Path, folder_token: str = "", folder_name: str = "", parallel: int = 4) -> Dict[str, Any]:
    if not run_dir.exists():
        raise RuntimeError(f"Run directory does not exist: {run_dir}")
    if not folder_token:
        folder = create_drive_folder(folder_name or f"售后AI图片归档-{run_dir.name}")
        folder_token = folder["folder_token"]
        folder_url = folder.get("url", "")
    else:
        folder_url = ""

    media_rows = load_record_rows(
        TABLES["media"],
        ["资源唯一键", "归档文件Token", "归档文件链接", "分析状态"],
    )
    media_record_by_key = {
        str(row.get("资源唯一键")): record_id
        for record_id, row in media_rows.items()
        if has_cell_value(row.get("资源唯一键"))
    }
    current_by_key = {
        str(row.get("资源唯一键")): row
        for row in media_rows.values()
        if has_cell_value(row.get("资源唯一键"))
    }

    items = list(iter_run_media_items(run_dir))
    sync_log_path = run_dir / "drive-image-sync.jsonl"
    uploaded = 0
    skipped = 0
    missing_local = 0
    missing_record = 0
    failed = 0
    jobs: List[Dict[str, Any]] = []

    for item in items:
        media_uid = str(item.get("资源唯一键") or "")
        file_key = str(item.get("file_key") or "")
        if not media_uid or not file_key:
            continue
        record_id = media_record_by_key.get(media_uid)
        if not record_id:
            missing_record += 1
            continue
        current = current_by_key.get(media_uid, {})
        if has_cell_value(current.get("归档文件Token")) or has_cell_value(current.get("归档文件链接")):
            skipped += 1
            continue
        file_path = local_image_path(run_dir, file_key)
        if not file_path:
            missing_local += 1
            continue
        suffix = file_path.suffix or ".jpg"
        jobs.append(
            {
                "media_uid": media_uid,
                "record_id": record_id,
                "file_key": file_key,
                "file_path": file_path,
                "drive_name": f"{stable_id('IMG', media_uid)}{suffix}",
            }
        )

    def sync_one(job: Dict[str, Any]) -> Dict[str, Any]:
        uploaded_file = upload_drive_file(job["file_path"], folder_token, job["drive_name"])
        update_media_archive(job["record_id"], uploaded_file["file_token"], uploaded_file.get("url", ""))
        return {
            "media_uid": job["media_uid"],
            "record_id": job["record_id"],
            "file_key": job["file_key"],
            "local_file": str(job["file_path"].relative_to(ROOT)),
            "drive_file_token": uploaded_file["file_token"],
            "drive_file_url": uploaded_file.get("url", ""),
        }

    worker_count = max(1, parallel)
    print(
        "Drive image sync queue: "
        f"jobs={len(jobs)}, skipped={skipped}, missing_local={missing_local}, "
        f"missing_record={missing_record}, parallel={worker_count}",
        flush=True,
    )

    with sync_log_path.open("a", encoding="utf-8") as sync_log, ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_by_job = {executor.submit(sync_one, job): job for job in jobs}
        for future in as_completed(future_by_job):
            job = future_by_job[future]
            try:
                log_entry = future.result()
            except RuntimeError as exc:
                failed += 1
                log_entry = {"media_uid": job["media_uid"], "record_id": job["record_id"], "error": str(exc)}
            else:
                uploaded += 1
            sync_log.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            sync_log.flush()
            if (uploaded + failed) % 10 == 0:
                print(f"Drive image sync progress: uploaded={uploaded}, failed={failed}", flush=True)

    return {
        "run_dir": str(run_dir),
        "folder_token": folder_token,
        "folder_url": folder_url,
        "total_run_media": len(items),
        "queued": len(jobs),
        "uploaded": uploaded,
        "skipped": skipped,
        "missing_local": missing_local,
        "missing_record": missing_record,
        "failed": failed,
        "sync_log": str(sync_log_path),
    }


def build_media_row(
    message: Dict[str, Any],
    file_key: str,
    raw_record_id: str,
    event_record_id: Optional[str],
    status: str,
) -> tuple[str, List[Any]]:
    media_uid = f"{message.get('message_id')}:{file_key}"
    media_id = stable_id("MEDIA", media_uid)
    return media_uid, [
        media_id,
        message.get("message_id", ""),
        file_key,
        media_uid,
        "image",
        status,
        message.get("message_app_link", ""),
        [{"id": raw_record_id}],
        [{"id": event_record_id}] if event_record_id else None,
        "图片已下载到本地采集审计目录，待同步到飞书云盘归档。" if status == "未下载" else "图片下载失败，保留资源键供后续重试。",
    ]


def parse_args() -> argparse.Namespace:
    default_end = date.today() + timedelta(days=1)
    default_start = default_end - timedelta(days=30)
    parser = argparse.ArgumentParser(description="Ingest Feishu support messages into the after-sales Base.")
    parser.add_argument("--start", default=default_start.isoformat(), help="Start date, e.g. 2026-04-26")
    parser.add_argument("--end", default=default_end.isoformat(), help="End date, e.g. 2026-05-27")
    parser.add_argument("--base-token", default="", help="Override FEISHU_SUPPORT_BASE_TOKEN.")
    parser.add_argument("--group-chat-id", default="", help="Override FEISHU_SUPPORT_GROUP_CHAT_ID.")
    parser.add_argument("--luz-chat-id", default="", help="Override FEISHU_SUPPORT_LUZ_CHAT_ID.")
    parser.add_argument("--table-events", default="", help="Override FEISHU_SUPPORT_TABLE_EVENTS.")
    parser.add_argument("--table-raw-messages", default="", help="Override FEISHU_SUPPORT_TABLE_RAW_MESSAGES.")
    parser.add_argument("--table-media", default="", help="Override FEISHU_SUPPORT_TABLE_MEDIA.")
    parser.add_argument("--table-actions", default="", help="Override FEISHU_SUPPORT_TABLE_ACTIONS.")
    parser.add_argument("--no-images", action="store_true", help="Do not download/upload image attachments.")
    parser.add_argument("--sync-drive-images", action="store_true", help="Only sync downloaded run images to Feishu Drive and patch media rows.")
    parser.add_argument("--run-dir", help="Existing data/feishu_ingest run directory for --sync-drive-images.")
    parser.add_argument("--drive-folder-token", default="", help="Existing Feishu Drive folder token for image archives.")
    parser.add_argument("--drive-folder-name", default="", help="Folder name to create when no --drive-folder-token is provided.")
    parser.add_argument("--parallel", type=int, default=4, help="Parallel Drive image upload workers.")
    parser.add_argument("--apply", action="store_true", help="Write to Feishu Base/Drive. Omit for dry-run mode.")
    parser.add_argument("--dry-run", action="store_true", help="Deprecated no-op; dry-run is the default unless --apply is passed.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = IngestConfig.from_args(args)
    configure_targets(config)
    print_target_summary(config, args)
    ensure_config(config, apply=args.apply, sync_drive_images=args.sync_drive_images)

    if args.sync_drive_images:
        if not args.run_dir:
            raise SystemExit("--sync-drive-images requires --run-dir")
        run_dir = Path(args.run_dir)
        if not run_dir.is_absolute():
            run_dir = ROOT / run_dir
        if not args.apply:
            result = summarize_drive_image_sync_plan(run_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        result = sync_drive_images(run_dir, args.drive_folder_token, args.drive_folder_name, args.parallel)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "data" / "feishu_ingest" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    sources = [
        Source("产品质量问题 发错货 售后及其他产品反馈", config.group_chat_id),
        Source("鲁志强 P2P", config.luz_chat_id),
    ]

    all_messages: List[Dict[str, Any]] = []
    for source in sources:
        messages = fetch_messages(source, args.start, args.end)
        all_messages.extend(messages)
        (out_dir / f"{source.name}.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")

    unique_messages: Dict[str, Dict[str, Any]] = {}
    for message in all_messages:
        message_id = message.get("message_id")
        if message_id:
            unique_messages[message_id] = message
    messages = list(unique_messages.values())
    messages.sort(key=lambda item: (item.get("create_time", ""), item.get("message_id", "")))
    (out_dir / "messages.cleaned.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Fetched {len(all_messages)} messages; {len(messages)} unique messages.")
    if not args.apply:
        print(f"Dry run output: {out_dir}")
        return

    existing_messages = load_existing_records(TABLES["raw_messages"], "message_id")
    existing_events = load_existing_records(TABLES["events"], "事件ID")
    existing_media = load_existing_records(TABLES["media"], "资源唯一键")
    existing_actions = load_existing_records(TABLES["actions"], "来源message_id")
    print(
        "Existing Base keys: "
        f"messages={len(existing_messages)}, events={len(existing_events)}, "
        f"media={len(existing_media)}, actions={len(existing_actions)}"
    )

    raw_fields = [
        "msg_type",
        "create_time",
        "sender_open_id",
        "raw_json",
        "message_id",
        "message_position",
        "reply_to",
        "mentions",
        "chat_id",
        "message_app_link",
        "content_text",
        "resource_keys",
        "sender_name",
        "thread_id",
    ]
    new_raw_messages = [message for message in messages if message.get("message_id") not in existing_messages]
    raw_rows = [raw_message_row(message) for message in new_raw_messages]
    raw_record_ids = batch_create(TABLES["raw_messages"], raw_fields, raw_rows, out_dir, "raw-messages")
    for message, record_id in zip(new_raw_messages, raw_record_ids):
        existing_messages[message["message_id"]] = record_id
    print(f"Created raw message records: {len(raw_record_ids)}")

    event_fields = [
        "工单创建日期",
        "关联消息",
        "问题大类",
        "问题子类",
        "事件标题",
        "原始消息链接",
        "提报人",
        "产品型号/SKU",
        "客户症状摘要",
        "状态",
        "需要人工复核",
        "置信度",
        "AI问题总结",
        "处理建议/结论",
        "首次反馈时间",
        "最后更新时间",
        "事件ID",
    ]
    event_message_ids: List[str] = []
    event_rows: List[List[Any]] = []
    event_ids: List[str] = []
    for message in messages:
        message_id = message.get("message_id", "")
        raw_record_id = existing_messages.get(message_id)
        if not raw_record_id or not is_event_candidate(message):
            continue
        event_id, row = event_row(message, raw_record_id)
        if event_id in existing_events:
            continue
        event_ids.append(event_id)
        event_message_ids.append(message_id)
        event_rows.append(row)

    new_event_record_ids = batch_create(TABLES["events"], event_fields, event_rows, out_dir, "events")
    event_record_by_message: Dict[str, str] = {}
    for event_id, message_id, record_id in zip(event_ids, event_message_ids, new_event_record_ids):
        existing_events[event_id] = record_id
        event_record_by_message[message_id] = record_id
    print(f"Created event candidate records: {len(new_event_record_ids)}")

    action_fields = ["动作内容", "动作类型", "来源消息链接", "动作时间", "来源message_id", "关联事件", "动作人"]
    action_rows: List[List[Any]] = []
    for message in messages:
        reply_to = message.get("reply_to")
        if not reply_to:
            continue
        event_record_id = event_record_by_message.get(reply_to)
        if event_record_id and message.get("message_id") not in existing_actions:
            action_rows.append(action_row(message, event_record_id))
    action_record_ids = batch_create(TABLES["actions"], action_fields, action_rows, out_dir, "actions")
    print(f"Created action log records: {len(action_record_ids)}")

    media_fields = [
        "媒体ID",
        "message_id",
        "file_key",
        "资源唯一键",
        "媒体类型",
        "分析状态",
        "原消息链接",
        "关联消息",
        "关联事件",
        "关键证据",
    ]
    media_rows: List[List[Any]] = []
    media_keys: List[str] = []
    media_files: Dict[str, Path] = {}
    image_dir = out_dir / "images"
    for message in messages:
        message_id = message.get("message_id", "")
        raw_record_id = existing_messages.get(message_id)
        if not raw_record_id:
            continue
        event_record_id = event_record_by_message.get(message_id)
        for image_key in extract_resource_keys(message)["images"]:
            media_uid = f"{message_id}:{image_key}"
            if media_uid in existing_media:
                continue
            status = "未下载"
            local_file: Optional[Path] = None
            if not args.no_images:
                local_file = download_image(message_id, image_key, image_dir)
                status = "未下载" if local_file else "分析失败"
            unique_key, row = build_media_row(message, image_key, raw_record_id, event_record_id, status)
            media_keys.append(unique_key)
            media_rows.append(row)
            if local_file:
                media_files[unique_key] = local_file
            if len(media_rows) % 25 == 0 and media_rows:
                print(f"Prepared media rows: {len(media_rows)}", flush=True)

    media_record_ids = batch_create(TABLES["media"], media_fields, media_rows, out_dir, "media")
    print(f"Created media records: {len(media_record_ids)}")
    if media_files and not args.no_images:
        drive_result = sync_drive_images(out_dir, args.drive_folder_token, args.drive_folder_name, args.parallel)
        print(f"Drive image sync: {json.dumps(drive_result, ensure_ascii=False)}")
    print(f"Audit output: {out_dir}")


if __name__ == "__main__":
    main()
