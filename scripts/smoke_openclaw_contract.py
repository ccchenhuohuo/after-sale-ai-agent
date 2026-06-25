#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fastapi.testclient import TestClient

import agent_runtime.channels.openclaw_feishu.webhook as openclaw_webhook
from agent_runtime.feishu.webhook import app
from agent_runtime.settings import Settings


def run_contract_smoke(settings: Settings | None = None, *, allow_unconfigured_secret: bool = False) -> dict[str, Any]:
    settings = settings or Settings()
    if (
        allow_unconfigured_secret
        and settings.openclaw_feishu_require_secret
        and not settings.openclaw_feishu_bridge_secret
    ):
        settings = settings.model_copy(update={"openclaw_feishu_require_secret": False})
    headers = {"content-type": "application/json"}
    if settings.openclaw_feishu_bridge_secret:
        headers["x-openclaw-feishu-secret"] = settings.openclaw_feishu_bridge_secret

    original_get_settings = openclaw_webhook.get_settings
    openclaw_webhook.get_settings = lambda: settings
    try:
        client = TestClient(app)
        health_response = client.get("/channels/openclaw-feishu/health")
        health = _json_response(health_response, "health")
        if health_response.status_code != 200:
            raise AssertionError(f"health returned HTTP {health_response.status_code}: {health}")

        response = client.post(
            "/channels/openclaw-feishu/support-case",
            headers=headers,
            json=_contract_payload(),
        )
        payload = _json_response(response, "support-case")
        if response.status_code != 200:
            raise AssertionError(f"support-case returned HTTP {response.status_code}: {payload}")
    finally:
        openclaw_webhook.get_settings = original_get_settings

    _assert_contract_response(payload)
    return {
        "ok": True,
        "mode": payload["mode"],
        "replyInThread": payload["replyInThread"],
        "replyToMessageId": payload["replyToMessageId"],
        "recommendedAction": payload.get("metadata", {}).get("recommendedAction", ""),
        "requiresSecret": bool(health.get("requiresSecret")),
        "allowUnconfiguredSecret": allow_unconfigured_secret,
        "textPreview": str(payload.get("fallbackText") or payload.get("text") or "")[:160],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Python-side OpenClaw Feishu contract smoke.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON report.")
    parser.add_argument(
        "--allow-unconfigured-secret",
        action="store_true",
        help="Allow local contract-only smoke when the bridge secret is not configured.",
    )
    args = parser.parse_args(argv)

    try:
        report = run_contract_smoke(allow_unconfigured_secret=args.allow_unconfigured_secret)
    except Exception as exc:
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if report["ok"] else 1


def _contract_payload() -> dict[str, Any]:
    return {
        "contractOnly": True,
        "batchId": "smoke-openclaw-feishu-001",
        "messages": [
            {
                "chatId": "oc_smoke_chat",
                "chatType": "group",
                "messageId": "om_smoke_text",
                "threadId": "omt_smoke_thread",
                "senderId": "ou_smoke_sender",
                "content": "客户反馈 L023 不亮，补充了一张疑似损坏图片。",
                "contentType": "text",
                "resources": [],
            },
            {
                "chatId": "oc_smoke_chat",
                "chatType": "group",
                "messageId": "om_smoke_image",
                "threadId": "omt_smoke_thread",
                "senderId": "ou_smoke_sender",
                "content": "",
                "contentType": "image",
                "resources": [
                    {
                        "type": "image",
                        "imageKey": "img_smoke_damage",
                        "fileName": "damage.jpg",
                        "mimeType": "image/jpeg",
                        "status": "error",
                        "downloadError": "smoke: no real Feishu download in local contract check",
                        "description": "产品损坏照片",
                    },
                ],
            },
        ],
    }


def _json_response(response, label: str) -> dict[str, Any]:
    try:
        value = response.json()
    except Exception as exc:
        raise AssertionError(f"{label} returned non-JSON response: {response.text[:200]}") from exc
    if not isinstance(value, dict):
        raise AssertionError(f"{label} returned non-object JSON: {value!r}")
    return value


def _assert_contract_response(payload: dict[str, Any]) -> None:
    expected = {
        "channel": "feishu",
        "mode": "thread_reply",
        "replyInThread": True,
        "chatId": "oc_smoke_chat",
        "threadId": "omt_smoke_thread",
        "replyToMessageId": "om_smoke_image",
    }
    for field, expected_value in expected.items():
        actual = payload.get(field)
        if actual != expected_value:
            raise AssertionError(f"Expected {field}={expected_value!r}, got {actual!r}")
    for field in ("text", "fallbackText"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise AssertionError(f"Expected {field} to be a non-empty string")


if __name__ == "__main__":
    raise SystemExit(main())
