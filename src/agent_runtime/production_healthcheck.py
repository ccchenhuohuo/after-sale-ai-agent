#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


import httpx

from agent_runtime.copilot.reference_safety import redact_internal_references
from agent_runtime.feishu.event_sources import _split_csv_ordered
from agent_runtime.feishu.message_sender import FEISHU_BASE_URL, get_tenant_access_token
from agent_runtime.settings import Settings


EXPECTED_PHOENIX_HOST = "100.111.223.41"
AGENT_SERVICE = "agent-runtime-feishu-long.service"
PHOENIX_SERVICE = "phoenix.service"
SECRET_ENV_KEYS = {
    "LLM_API_KEY",
    "BAILIAN_API_KEY",
    "DASHSCOPE_API_KEY",
    "FEISHU_APP_SECRET",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_HUMAN_REVIEW_USER_OPEN_ID",
    "OPENAI_TRACING_API_KEY",
    "OPENAI_API_KEY",
    "OPENCLAW_FEISHU_BRIDGE_SECRET",
}
REQUIRED_ENV_KEYS = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "SUPPORT_AGENT_MODEL",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_SUPPORT_GROUP_CHAT_ID",
    "FEISHU_MESSAGE_ADMISSION_MODE",
    "FEISHU_THREAD_CONTEXT_ENABLED",
    "SKU_CATALOG_PATH",
    "HISTORY_RAG_INDEX_PATH",
    "FORMAL_KB_INDEX_PATH",
    "MEDIA_RAG_INDEX_PATH",
)
CRITICAL_ENV_KEYS = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "SUPPORT_AGENT_MODEL",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_SUPPORT_GROUP_CHAT_ID",
    "FEISHU_MESSAGE_ADMISSION_MODE",
    "SKU_CATALOG_PATH",
)


def run_healthcheck(
    *,
    root: Path | None = None,
    settings: Settings | None = None,
    probe_feishu: bool = False,
    probe_phoenix: bool = False,
    skip_systemd: bool = False,
    skip_journal: bool = False,
    journal_minutes: int = 30,
) -> dict[str, Any]:
    """Run a read-only production healthcheck and return a redacted JSON report."""
    root = (root or Path.cwd()).resolve()
    settings = settings or _load_settings(root)
    env_keys = _read_env_keys(root / ".env")
    checks: list[dict[str, Any]] = []

    checks.append(_check_env_presence(env_keys))
    checks.append(_check_group_whitelist(settings))
    checks.append(_check_admission_mode(settings))
    checks.append(_check_phoenix_endpoint(settings))
    checks.append(_check_deploy_revision(root))
    checks.extend(_check_rag_paths(root, settings))
    checks.append(_check_reply_ledger(root, settings))
    if not skip_systemd:
        checks.extend(_check_systemd_services([AGENT_SERVICE, PHOENIX_SERVICE]))
    if not skip_journal:
        checks.append(_check_journal(AGENT_SERVICE, journal_minutes=journal_minutes))
    if probe_phoenix:
        checks.append(_check_phoenix_http(settings))
    if probe_feishu:
        checks.append(asyncio.run(_check_feishu_permission(settings)))

    return {
        "ok": all(check.get("ok") is True for check in checks),
        "mode": "readonly",
        "generated_at": int(time.time()),
        "root": str(root),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only production checks for the Feishu support bot.")
    parser.add_argument("--root", default=".", help="Deployment root containing .env, data/, and .deploy-revision.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument("--probe-feishu", action="store_true", help="Call read-only Feishu message-list API to verify permissions.")
    parser.add_argument("--probe-phoenix", action="store_true", help="Call Phoenix HTTP endpoint.")
    parser.add_argument("--skip-systemd", action="store_true", help="Skip systemd service checks.")
    parser.add_argument("--skip-journal", action="store_true", help="Skip journalctl log counters.")
    parser.add_argument("--journal-minutes", type=int, default=30, help="Lookback window for journal counters.")
    args = parser.parse_args(argv)

    report = run_healthcheck(
        root=Path(args.root),
        probe_feishu=args.probe_feishu,
        probe_phoenix=args.probe_phoenix,
        skip_systemd=args.skip_systemd,
        skip_journal=args.skip_journal,
        journal_minutes=args.journal_minutes,
    )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None) + "\n")
    return 0 if report["ok"] else 1


def _load_settings(root: Path) -> Settings:
    env_path = root / ".env"
    if env_path.exists():
        return Settings(_env_file=env_path)
    return Settings()


def _read_env_keys(env_path: Path) -> set[str]:
    keys: set[str] = set()
    if not env_path.exists():
        return keys
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def _check_env_presence(env_keys: set[str]) -> dict[str, Any]:
    present: dict[str, bool] = {}
    for key in REQUIRED_ENV_KEYS:
        present[key] = key in env_keys or bool(os.getenv(key))
    missing = [key for key, exists in present.items() if not exists]
    missing_critical = [key for key in CRITICAL_ENV_KEYS if not present.get(key)]
    secret_keys_configured = {
        key: (key in env_keys or bool(os.getenv(key)))
        for key in sorted(SECRET_ENV_KEYS)
        if key in REQUIRED_ENV_KEYS or key in env_keys or os.getenv(key)
    }
    return {
        "name": "env_presence",
        "ok": not missing_critical,
        "missing": missing,
        "missing_critical": missing_critical,
        "present": present,
        "secret_keys_configured": secret_keys_configured,
    }


def _check_group_whitelist(settings: Settings) -> dict[str, Any]:
    chat_ids = _split_csv_ordered(settings.feishu_support_group_chat_id)
    return {
        "name": "feishu_group_whitelist",
        "ok": bool(chat_ids),
        "chat_count": len(chat_ids),
    }


def _check_admission_mode(settings: Settings) -> dict[str, Any]:
    mode = settings.feishu_message_admission_mode
    return {
        "name": "feishu_admission_mode",
        "ok": mode in {"mention_only", "listen_new_topics"},
        "mode": mode,
        "thread_context_enabled": settings.feishu_thread_context_enabled,
        "thread_context_max_messages": settings.feishu_thread_context_max_messages,
    }


def _check_phoenix_endpoint(settings: Settings) -> dict[str, Any]:
    endpoint = settings.phoenix_collector_endpoint
    return {
        "name": "phoenix_endpoint",
        "ok": (not settings.phoenix_tracing_enabled) or EXPECTED_PHOENIX_HOST in endpoint,
        "tracing_enabled": settings.phoenix_tracing_enabled,
        "expected_host": EXPECTED_PHOENIX_HOST,
        "configured_expected_host": EXPECTED_PHOENIX_HOST in endpoint,
    }


def _check_deploy_revision(root: Path) -> dict[str, Any]:
    head = _run_git(root, "rev-parse", "HEAD")
    status = _run_git(root, "status", "--porcelain")
    deploy_revision_path = root / ".deploy-revision"
    deploy_revision_text = deploy_revision_path.read_text(encoding="utf-8").strip() if deploy_revision_path.exists() else ""
    deploy_revision = _deploy_revision_commit(deploy_revision_text)
    return {
        "name": "deploy_revision",
        "ok": bool(head["ok"]) and bool(deploy_revision) and deploy_revision == head["stdout"] and status["stdout"] == "",
        "git_available": bool(head["ok"]),
        "deploy_revision_present": bool(deploy_revision),
        "deploy_revision_matches_head": bool(deploy_revision and deploy_revision == head["stdout"]),
        "worktree_clean": status["stdout"] == "",
        "dirty_file_count": len(status["stdout"].splitlines()) if status["stdout"] else 0,
        "head": head["stdout"][:40] if head["ok"] else "",
    }


def _deploy_revision_commit(value: str) -> str:
    for line in value.splitlines():
        if line.startswith("commit="):
            return line.split("=", 1)[1].strip()
    return value.strip()


def _check_rag_paths(root: Path, settings: Settings) -> list[dict[str, Any]]:
    return [
        _check_path(root, "sku_catalog", settings.sku_catalog_path, expect_file=True),
        _check_path(root, "history_rag_index", settings.history_rag_index_path, expect_file=False),
        _check_path(root, "formal_kb_index", settings.formal_kb_index_path, expect_file=False),
        _check_path(root, "media_rag_index", settings.media_rag_index_path, expect_file=False),
    ]


def _check_path(root: Path, name: str, configured_path: str, *, expect_file: bool) -> dict[str, Any]:
    path = _resolve(root, configured_path)
    exists = path.is_file() if expect_file else path.exists()
    return {
        "name": name,
        "ok": exists,
        "configured": bool(configured_path),
        "exists": exists,
        "path_type": "file" if expect_file else "path",
        "file_count": _count_files(path) if path.exists() else 0,
        "bytes": _path_size(path) if path.exists() else 0,
    }


def _check_reply_ledger(root: Path, settings: Settings) -> dict[str, Any]:
    db_path = _resolve(root, settings.feishu_runtime_db_path)
    if not db_path.exists():
        return {"name": "reply_ledger", "ok": False, "exists": False}
    try:
        with sqlite3.connect(db_path) as connection:
            status_counts = {
                str(status): int(count)
                for status, count in connection.execute(
                    "SELECT status, COUNT(*) FROM reply_ledger GROUP BY status ORDER BY status"
                ).fetchall()
            }
            recent_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM reply_ledger WHERE updated_at >= ?",
                    (time.time() - 3600,),
                ).fetchone()[0]
            )
            stale_processing = int(
                connection.execute(
                    "SELECT COUNT(*) FROM seen_events WHERE status = 'processing' AND updated_at < ?",
                    (time.time() - max(1, settings.feishu_processing_stale_seconds),),
                ).fetchone()[0]
            )
    except sqlite3.Error as exc:
        return {
            "name": "reply_ledger",
            "ok": False,
            "exists": True,
            "error": redact_internal_references(f"{type(exc).__name__}: {exc}", max_chars=300),
        }
    return {
        "name": "reply_ledger",
        "ok": True,
        "exists": True,
        "status_counts": status_counts,
        "recent_1h_count": recent_count,
        "stale_processing_count": stale_processing,
    }


def _check_systemd_services(service_names: list[str]) -> list[dict[str, Any]]:
    if shutil.which("systemctl") is None:
        return [
            {
                "name": f"systemd:{service_name}",
                "ok": False,
                "available": False,
                "status": "systemctl_not_found",
            }
            for service_name in service_names
        ]
    checks = []
    for service_name in service_names:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = (result.stdout or result.stderr or "").strip()
        checks.append(
            {
                "name": f"systemd:{service_name}",
                "ok": result.returncode == 0 and status == "active",
                "available": True,
                "status": status or f"exit_{result.returncode}",
            }
        )
    return checks


def _check_journal(service_name: str, *, journal_minutes: int) -> dict[str, Any]:
    if shutil.which("journalctl") is None:
        return {"name": "journal_errors", "ok": False, "available": False}
    result = subprocess.run(
        [
            "journalctl",
            "-u",
            service_name,
            f"--since=-{max(1, journal_minutes)} min",
            "--no-pager",
            "--output=cat",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    text = result.stdout or ""
    counters = {
        "traceback": text.count("Traceback"),
        "reply_failed": text.count("reply_failed"),
        "backfill_failed": text.count("backfill failed"),
        "feishu_400": text.count("status=400"),
        "feishu_230027": text.count("230027"),
    }
    return {
        "name": "journal_errors",
        "ok": result.returncode == 0 and counters["traceback"] == 0 and counters["reply_failed"] == 0,
        "available": result.returncode == 0,
        "window_minutes": max(1, journal_minutes),
        "counters": counters,
    }


def _check_phoenix_http(settings: Settings) -> dict[str, Any]:
    endpoint = settings.phoenix_collector_endpoint
    base_url = endpoint.split("/v1/traces", 1)[0].rstrip("/") or endpoint
    request = urllib.request.Request(base_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = int(getattr(response, "status", 0) or 0)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "name": "phoenix_http",
            "ok": False,
            "status": "failed",
            "error": redact_internal_references(f"{type(exc).__name__}: {exc}", max_chars=300),
        }
    return {
        "name": "phoenix_http",
        "ok": 200 <= status < 500,
        "status": status,
    }


async def _check_feishu_permission(settings: Settings) -> dict[str, Any]:
    chat_ids = _split_csv_ordered(settings.feishu_support_group_chat_id)
    if not chat_ids:
        return {"name": "feishu_permission_probe", "ok": False, "chat_count": 0, "results": []}
    try:
        token = await get_tenant_access_token(settings)
    except Exception as exc:
        return {
            "name": "feishu_permission_probe",
            "ok": False,
            "chat_count": len(chat_ids),
            "error": redact_internal_references(f"{type(exc).__name__}: {exc}", max_chars=300),
            "results": [],
        }

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for chat_id in chat_ids:
            results.append(await _probe_feishu_chat(client, token, chat_id))
    return {
        "name": "feishu_permission_probe",
        "ok": all(item.get("ok") is True for item in results),
        "chat_count": len(chat_ids),
        "results": results,
    }


async def _probe_feishu_chat(client: httpx.AsyncClient, token: str, chat_id: str) -> dict[str, Any]:
    try:
        response = await client.get(
            f"{FEISHU_BASE_URL}/im/v1/messages",
            params={
                "container_id_type": "chat",
                "container_id": chat_id,
                "page_size": 1,
                "only_thread_root_messages": True,
                "sort_type": "ByCreateTimeDesc",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = response.json()
    except Exception as exc:
        return {
            "ok": False,
            "chat_id_hash": _short_hash(chat_id),
            "error": redact_internal_references(f"{type(exc).__name__}: {exc}", max_chars=300),
        }
    code = payload.get("code", 0) if isinstance(payload, dict) else "unknown"
    msg = str(payload.get("msg") or "")[:300] if isinstance(payload, dict) else "non_object_response"
    try:
        normalized_code = int(code or 0)
    except (TypeError, ValueError):
        normalized_code = -1
    return {
        "ok": response.status_code < 400 and normalized_code == 0,
        "chat_id_hash": _short_hash(chat_id),
        "status": response.status_code,
        "code": code,
        "msg": redact_internal_references(msg, max_chars=300),
        "missing_scope": "im:message.group_msg" if normalized_code == 230027 else "",
    }


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _count_files(path: Path) -> int:
    if path.is_file():
        return 1
    return sum(1 for item in path.rglob("*") if item.is_file())


def _run_git(root: Path, *args: str) -> dict[str, Any]:
    if shutil.which("git") is None:
        return {"ok": False, "stdout": "", "stderr": "git_not_found"}
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return {
        "ok": result.returncode == 0,
        "stdout": (result.stdout or "").strip(),
        "stderr": redact_internal_references(result.stderr, max_chars=300),
    }


def _short_hash(value: str) -> str:
    import hashlib

    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12] if value else ""


if __name__ == "__main__":
    raise SystemExit(main())
