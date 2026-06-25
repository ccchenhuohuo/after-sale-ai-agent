#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import ast
import json
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import numpy as np
from fastapi.testclient import TestClient

from agent_runtime.channels.feishu_reply import render_feishu_visible_runtime_reply
from agent_runtime.copilot.answer_contract import FEISHU_VISIBLE_REPLY_FALLBACK, SupportAnswer
from agent_runtime.copilot.case_context import SupportAsset, SupportCaseRequest
from agent_runtime.copilot.runtime import run_support_case_request
from agent_runtime.feishu.events import event_from_payload
from agent_runtime.feishu.webhook import app
from agent_runtime.settings import Settings


CheckFn = Callable[[], Any]


def run_smoke(*, live: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    scenarios: list[dict[str, Any]] = []
    failed = False

    with tempfile.TemporaryDirectory(prefix="support-copilot-smoke-") as temp_name:
        temp_dir = Path(temp_name)
        settings = _build_smoke_settings(temp_dir)
        _write_fixture_data(temp_dir)

        for name, check in (
            ("imports_and_settings", lambda: _check_imports_and_settings(settings)),
            ("core_import_boundary", _check_core_import_boundary),
            ("openclaw_http_contract", _check_openclaw_http_contract),
            ("legacy_feishu_parse", _check_legacy_feishu_parse),
        ):
            record = _run_check(name, check)
            checks.append(record)
            failed = failed or not record["ok"]

        if live:
            live_record = _run_check("live_smoke_guard", _check_live_smoke_enabled)
            checks.append(live_record)
            failed = failed or not live_record["ok"]

        with _patched_runner_run(_fixed_support_answer):
            for request in _smoke_requests(temp_dir):
                record = _run_async_scenario(request, settings)
                scenarios.append(record)
                failed = failed or not record["ok"]

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    report = {
        "ok": not failed,
        "mode": "live" if live else "offline",
        "checks": checks,
        "scenarios": scenarios,
        "duration_ms": duration_ms,
    }
    leak_record = _run_check("smoke_report_leak_check", lambda: _check_report_no_sensitive_artifacts(report))
    checks.append(leak_record)
    report["ok"] = report["ok"] and leak_record["ok"]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline smoke checks for the support copilot.")
    parser.add_argument("--live", action="store_true", help="Require RUN_LIVE_SMOKE=1 before live smoke wiring.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON report.")
    args = parser.parse_args(argv)

    report = run_smoke(live=args.live)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if report["ok"] else 1


def _run_check(name: str, fn: CheckFn) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        details = fn()
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "name": name,
        "ok": True,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "details": details or {},
    }


def _run_async_scenario(request: SupportCaseRequest, settings: Settings) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = asyncio.run(
            run_support_case_request(
                request,
                settings,
                entrypoint="smoke",
                source_label="离线冒烟测试",
                session=None,
                run_config_group_id=f"smoke:{request.request_id}",
                run_config_metadata={"source": "offline-smoke"},
            )
        )
        visible_reply = render_feishu_visible_runtime_reply(result)
        _assert_runtime_result(request, result, visible_reply.safe_text)
    except Exception as exc:
        return {
            "name": request.request_id,
            "ok": False,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }

    context = result.case_result.context
    visual_artifacts = [artifact for artifact in result.case_result.artifacts if artifact.artifact_type == "visual_summary"]
    visual_ok = [artifact for artifact in visual_artifacts if artifact.status == "ok"]
    return {
        "name": request.request_id,
        "ok": True,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "route": {
            "input_modality": result.case_result.route.input_modality,
            "needs_clarification": result.case_result.route.needs_clarification,
            "asset_decisions": [
                {
                    "asset_id": decision.asset_id,
                    "media_type": decision.media_type,
                    "asset_role": decision.asset_role,
                    "requires_ocr": decision.requires_ocr,
                    "requires_visual_embedding": decision.requires_visual_embedding,
                    "requires_video_sampling": decision.requires_video_sampling,
                }
                for decision in result.case_result.route.asset_decisions
            ],
        },
        "ingestion_artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "status": artifact.status,
                "asset_id": artifact.asset_id,
                "has_vector_ref": bool(artifact.vector_id),
            }
            for artifact in result.case_result.artifacts
        ],
        "context": {
            "normalized_query": context.normalized_query,
            "visual_summaries": context.visual_summaries,
            "visual_summary_status": visual_artifacts[-1].status if visual_artifacts else "not_required",
            "visual_summary_excerpt": visual_ok[-1].summary[:160] if visual_ok else "",
            "asset_refs": context.asset_refs,
            "vector_refs": context.vector_refs,
            "missing_information": context.missing_information,
            "confidence": context.confidence,
        },
        "coverage": {
            "recommended_action": result.coverage.recommended_action,
            "mention_enabled": result.coverage.mention_enabled,
            "hit_sources": [item.source_id for item in result.coverage.items if item.status == "hit"],
            "missing_sources": [item.source_id for item in result.coverage.items if item.status != "hit"],
        },
        "final_answer": {
            "recommended_action": result.answer.recommended_action,
            "mention_enabled": result.answer.mention_enabled,
            "contract_issues": [issue.code for issue in result.contract_issues],
            "visible_reply_chars": len(visible_reply.safe_text),
        },
    }


def _build_smoke_settings(temp_dir: Path) -> Settings:
    data_dir = temp_dir / "data"
    return Settings(
        llm_api_key="",
        support_agent_tracing_disabled=True,
        support_intake_router_enabled=False,
        support_context_assembler_enabled=False,
        support_ocr_provider="disabled",
        support_visual_understanding_provider="fake",
        support_video_ffmpeg_path=str(temp_dir / "missing-ffmpeg"),
        support_video_sample_dir=str(data_dir / "video_samples"),
        support_vector_artifact_dir=str(data_dir / "vectors"),
        support_asset_allowed_local_dirs=str(temp_dir / "assets"),
        support_asset_allowed_url_hosts="assets.example.test",
        support_agent_session_db_path=str(data_dir / "agent_sessions.sqlite3"),
        feishu_runtime_db_path=str(data_dir / "runtime.sqlite3"),
        feishu_asset_cache_dir=str(data_dir / "feishu_assets"),
        sku_catalog_path=str(data_dir / "sku.csv"),
        history_rag_index_path=str(data_dir / "history_index"),
        history_rag_provider="local_hash",
        history_rag_require_remote_models=False,
        history_rag_top_k=3,
        history_rag_top_n=2,
        formal_kb_source_dir=str(data_dir / "formal_source"),
        formal_kb_index_path=str(data_dir / "formal_index"),
        formal_kb_provider="local_hash",
        formal_kb_require_remote_models=False,
        formal_kb_top_k=3,
        formal_kb_top_n=2,
        media_rag_index_path=str(data_dir / "media_index"),
        media_rag_provider="local_hash",
        media_rag_require_vl_models=False,
        media_rag_top_k=3,
        media_rag_top_n=2,
    )


def _write_fixture_data(temp_dir: Path) -> None:
    data_dir = temp_dir / "data"
    asset_dir = temp_dir / "assets"
    data_dir.mkdir(parents=True)
    asset_dir.mkdir(parents=True)
    (asset_dir / "damage.jpg").write_bytes(b"\xff\xd8\xff\xe0smoke-jpeg")

    (data_dir / "sku.csv").write_text(
        "\n".join(
            [
                "sku_code,spu,sku_name_cn,product_name_cn,product_owner_name",
                "L023,L023,L023 直播补光灯,直播补光灯,售后负责人A",
                "S043,S043,S043 监视器,监视器,售后负责人B",
            ]
        ),
        encoding="utf-8",
    )

    history_dir = data_dir / "history_index"
    history_dir.mkdir()
    history_chunks = [
        {
            "chunk_id": "hist_l023_1",
            "topic_id": "thread:l023",
            "topic_link": "https://feishu.test/l023",
            "sku": "L023",
            "issue_category": "troubleshooting",
            "solution_type": "排查后人工确认",
            "text": "L023 客户反馈不亮，历史客服建议先确认充电线、插头、接口和按键状态。",
        }
    ]
    _write_jsonl(history_dir / "history_chunks.jsonl", history_chunks)
    np.save(history_dir / "embeddings.npy", np.zeros((len(history_chunks), 768), dtype=np.float32))

    formal_dir = data_dir / "formal_index"
    formal_dir.mkdir()
    formal_chunks = [
        {
            "chunk_id": "formal_l023_1",
            "source_id": "kb-l023-power",
            "source_type": "official_kb",
            "evidence_level": "formal",
            "verified": True,
            "title": "L023 不亮排查 SOP",
            "section": "基础供电排查",
            "product_model": "L023",
            "sku": "L023",
            "source_url": "https://kb.example.test/l023-power",
            "text": "L023 不亮时，先确认充电线、插头、接口、按键和指示灯状态，再根据照片或视频判断是否需要人工复核。",
        }
    ]
    _write_jsonl(formal_dir / "formal_chunks.jsonl", formal_chunks)
    np.save(formal_dir / "embeddings.npy", np.zeros((len(formal_chunks), 768), dtype=np.float32))

    media_dir = data_dir / "media_index"
    media_dir.mkdir()
    media_chunks = [
        {
            "chunk_id": "media_l023_1",
            "topic_id": "thread:l023-media",
            "topic_link": "https://feishu.test/l023-media",
            "message_link": "https://feishu.test/l023-media#img",
            "sku": "L023",
            "media_type": "image",
            "media_id": "img_l023_damage",
            "text": "L023 图片中客户展示疑似外观损坏，需要人工打开原图确认。",
        }
    ]
    _write_jsonl(media_dir / "media_chunks.jsonl", media_chunks)
    (media_dir / "manifest.json").write_text(json.dumps({"source": "smoke"}, ensure_ascii=False), encoding="utf-8")
    np.save(media_dir / "media_embeddings.npy", np.zeros((len(media_chunks), 1024), dtype=np.float32))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")


def _smoke_requests(temp_dir: Path) -> list[SupportCaseRequest]:
    asset_dir = temp_dir / "assets"
    damage_path = asset_dir / "damage.jpg"
    return [
        SupportCaseRequest(
            request_id="smoke_text_only",
            source="terminal",
            channel="terminal",
            source_platform="terminal",
            user_text="L023 不亮，客户说刚收到就不能用",
        ),
        SupportCaseRequest(
            request_id="smoke_openclaw_burst_equivalent",
            source="feishu",
            channel="openclaw_feishu",
            source_platform="feishu",
            user_text="客户反馈 L023 不亮，补充了一张疑似损坏图片。",
            chat_id="oc_smoke_chat",
            thread_id="omt_smoke_thread",
            message_id="om_smoke_image",
            assets=[
                SupportAsset(
                    asset_id="openclaw_asset:smoke:image:damage",
                    media_type="image",
                    source="openclaw_feishu",
                    filename="damage.jpg",
                    mime_type="image/jpeg",
                    file_key="img_smoke_damage",
                    metadata={
                        "asset_role": "damage_photo",
                        "description": "产品损坏照片",
                        "download_status": "error",
                    },
                )
            ],
        ),
        SupportCaseRequest(
            request_id="smoke_damage_image",
            source="feishu",
            channel="openclaw_feishu",
            source_platform="feishu",
            assets=[
                SupportAsset(
                    asset_id="openclaw_asset:smoke:image:damage_local",
                    media_type="image",
                    source="openclaw_feishu",
                    filename="damage.jpg",
                    mime_type="image/jpeg",
                    local_path=str(damage_path),
                    metadata={"asset_role": "damage_photo", "description": "产品损坏图片"},
                )
            ],
        ),
        SupportCaseRequest(
            request_id="smoke_mixed_text_image",
            source="feishu",
            channel="openclaw_feishu",
            source_platform="feishu",
            user_text="L023 外壳有裂痕，客户问怎么处理。",
            assets=[
                SupportAsset(
                    asset_id="openclaw_asset:smoke:image:mixed_damage",
                    media_type="image",
                    source="openclaw_feishu",
                    filename="damage.jpg",
                    mime_type="image/jpeg",
                    local_path=str(damage_path),
                    metadata={"asset_role": "damage_photo", "description": "产品裂痕照片"},
                )
            ],
        ),
        SupportCaseRequest(
            request_id="smoke_video_placeholder",
            source="feishu",
            channel="openclaw_feishu",
            source_platform="feishu",
            user_text="客户发了一个 L023 不亮的视频。",
            assets=[
                SupportAsset(
                    asset_id="openclaw_asset:smoke:video:remote",
                    media_type="video",
                    source="openclaw_feishu",
                    filename="fault.mp4",
                    mime_type="video/mp4",
                    url="https://assets.example.test/fault.mp4",
                    metadata={"asset_role": "video", "description": "故障视频"},
                )
            ],
        ),
        SupportCaseRequest(
            request_id="smoke_rejected_attachment",
            source="feishu",
            channel="openclaw_feishu",
            source_platform="feishu",
            user_text="客户补了一张图片。",
            assets=[
                SupportAsset(
                    asset_id="openclaw_asset:smoke:image:rejected",
                    media_type="image",
                    source="openclaw_feishu",
                    filename="secret.jpg",
                    mime_type="image/jpeg",
                    local_path="/etc/passwd",
                    metadata={"asset_role": "damage_photo", "description": "非法路径测试"},
                )
            ],
        ),
    ]


def _check_imports_and_settings(settings: Settings) -> dict[str, Any]:
    import agent_runtime.channels.openclaw_feishu.adapter as openclaw_adapter
    import agent_runtime.copilot.runtime as core_runtime
    import agent_runtime.feishu.bridge as legacy_bridge
    import agent_runtime.feishu.webhook as feishu_webhook

    assert settings.llm_api_key == ""
    assert openclaw_adapter.build_support_case_request_from_openclaw
    assert core_runtime.run_support_case_request
    assert legacy_bridge.event_from_payload
    assert feishu_webhook.app
    return {"llm_api_key_required": False, "fastapi_title": feishu_webhook.app.title}


def _check_core_import_boundary() -> dict[str, Any]:
    runtime_path = ROOT / "src" / "agent_runtime" / "copilot" / "runtime.py"
    tree = ast.parse(runtime_path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    forbidden_prefixes = ("agent_runtime.feishu", "agent_runtime.channels", "openclaw", "lark_oapi")
    violations = [name for name in imports if name.startswith(forbidden_prefixes)]
    assert not violations, f"Core runtime imports channel modules: {violations}"
    return {"checked_file": str(runtime_path), "forbidden_imports": []}


def _check_openclaw_http_contract() -> dict[str, Any]:
    import agent_runtime.channels.openclaw_feishu.webhook as openclaw_webhook

    original_get_settings = openclaw_webhook.get_settings
    openclaw_webhook.get_settings = lambda: Settings(
        openclaw_feishu_bridge_secret="",
        openclaw_feishu_require_secret=False,
        llm_api_key="",
    )
    try:
        with TestClient(app) as client:
            healthz = client.get("/healthz")
            channel_health = client.get("/channels/openclaw-feishu/health")
            reply = client.post(
                "/channels/openclaw-feishu/support-case",
                json={
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
                        },
                        {
                            "chatId": "oc_smoke_chat",
                            "chatType": "group",
                            "messageId": "om_smoke_image",
                            "threadId": "omt_smoke_thread",
                            "senderId": "ou_smoke_sender",
                            "contentType": "image",
                            "resources": [
                                {
                                    "type": "image",
                                    "imageKey": "img_smoke_damage",
                                    "fileName": "damage.jpg",
                                    "mimeType": "image/jpeg",
                                    "status": "error",
                                    "downloadError": "smoke: no real download",
                                }
                            ],
                        },
                    ],
                },
            )
    finally:
        openclaw_webhook.get_settings = original_get_settings
    assert healthz.status_code == 200
    assert healthz.json() == {"ok": "true"}
    assert channel_health.status_code == 200
    assert channel_health.json()["runtime"] == "support_copilot"
    payload = reply.json()
    assert reply.status_code == 200
    assert payload["mode"] == "thread_reply"
    assert payload["replyInThread"] is True
    assert payload["chatId"] == "oc_smoke_chat"
    assert payload["threadId"] == "omt_smoke_thread"
    assert payload["replyToMessageId"] == "om_smoke_image"
    assert payload["fallbackText"].strip()
    return {
        "health": channel_health.json(),
        "reply_mode": payload["mode"],
        "reply_to_message_id": payload["replyToMessageId"],
    }


def _check_legacy_feishu_parse() -> dict[str, Any]:
    event = event_from_payload(
        {
            "event_id": "evt_smoke",
            "chat_id": "oc_legacy_smoke",
            "chat_type": "group",
            "message_id": "om_legacy_smoke",
            "message_type": "text",
            "sender_id": "ou_smoke",
            "content": json.dumps({"text": "@飞书 CLI L023 不亮，客户刚收到"}),
            "mentions": [{"name": "飞书 CLI", "id": {"open_id": "ou_bot"}}],
            "thread_id": "omt_legacy_thread",
        }
    )
    assert event is not None
    assert event.chat_id == "oc_legacy_smoke"
    assert event.message_id == "om_legacy_smoke"
    assert "L023 不亮" in event.content
    assert event.mention_names == ("飞书 CLI",)
    return {
        "chat_type": event.chat_type,
        "message_type": event.message_type,
        "thread_id": event.thread_id,
    }


def _check_live_smoke_enabled() -> dict[str, Any]:
    import os

    assert os.getenv("RUN_LIVE_SMOKE") == "1", "Set RUN_LIVE_SMOKE=1 before live smoke."
    return {"enabled": True}


def _check_report_no_sensitive_artifacts(report: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(report, ensure_ascii=False)
    forbidden = (
        "file_key",
        "local_path",
        "frame_paths",
        "/etc/passwd",
        "assets.example.test",
        "img_smoke_damage",
        "img_key",
        "video_key",
        "[0.0",
    )
    leaked = [token for token in forbidden if token in raw]
    assert not leaked, f"smoke report leaked internal attachment details: {leaked}"
    return {"forbidden_tokens_checked": len(forbidden)}


def _assert_runtime_result(request: SupportCaseRequest, result: Any, visible_text: str) -> None:
    assert result.request.request_id == request.request_id
    assert result.case_result.context.normalized_query.strip()
    assert result.answer.mention_enabled == result.coverage.mention_enabled
    assert result.answer.recommended_action == result.coverage.recommended_action or _action_rank(
        result.answer.recommended_action
    ) >= _action_rank(result.coverage.recommended_action)
    assert not result.contract_issues
    forbidden_visible_tokens = (
        "SupportAnswer",
        "Agent SDK",
        "trace",
        "tool",
        "raw vector",
        "file_key",
        "local_path",
        "/etc/passwd",
        "assets.example.test",
    )
    assert visible_text.strip()
    assert not any(token in visible_text for token in forbidden_visible_tokens)
    if _has_any_source_hit(result):
        assert visible_text != FEISHU_VISIBLE_REPLY_FALLBACK
        assert "客服可以先这样回应客户" in visible_text
        if result.coverage.recommended_action == "human_review":
            assert result.coverage.mention_enabled is True
            assert "建议人工复核" in visible_text
    else:
        assert result.coverage.recommended_action == "human_review"
        assert result.coverage.mention_enabled is True
        assert visible_text == FEISHU_VISIBLE_REPLY_FALLBACK
    raw = result.model_dump(mode="json") if hasattr(result, "model_dump") else {}
    raw_text = json.dumps(raw, ensure_ascii=False)
    assert "[0.0" not in raw_text
    if request.request_id == "smoke_rejected_attachment":
        assert any(
            artifact.status == "unsupported" and artifact.artifact_type == "image_embedding"
            for artifact in result.case_result.artifacts
        )
    if request.request_id == "smoke_video_placeholder":
        assert any(
            artifact.status == "unsupported" and artifact.artifact_type == "video_sampling"
            for artifact in result.case_result.artifacts
        )


def _action_rank(action: str) -> int:
    return {"answer": 0, "ask_clarification": 1, "human_review": 2}.get(action, 0)


def _has_any_source_hit(result: Any) -> bool:
    return any(
        getattr(item, "status", "") == "hit" and int(getattr(item, "hit_count", 0) or 0) > 0
        for item in result.coverage.items
    )


def _fixed_support_answer(*args: Any, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(final_output=_support_answer())


def _support_answer() -> SupportAnswer:
    return SupportAnswer(
        issue_type="troubleshooting",
        run_mode="Agent SDK",
        confidence="中",
        confidence_reason="离线冒烟测试固定答案，真实模型未调用。",
        user_issue_summary="客户反馈 L023 不亮或疑似损坏，需要先补充基础排查信息。",
        sku_match="SKU 目录可用于识别 L023，但不能单独判断故障原因。",
        suggested_reply="您好，关于您反馈的产品异常，建议先确认充电线、插头、接口和按键状态，并补充订单号、产品铭牌和当前状态照片，客服再结合人工复核给出处理口径。",
        troubleshooting_steps=["确认充电线、插头和接口是否正常", "确认是否长按开机键并观察指示灯", "补充产品铭牌或订单信息"],
        follow_up_questions=["请提供订单号或产品型号", "请补充清晰的产品状态照片"],
        official_evidence="未查询到可信正式依据，不可编造。",
        history_reference="未查询到可信历史参考，不可编造。",
        data_sources_used=[],
        missing_data_sources=[],
        recommended_action="answer",
        mention_enabled=False,
        ticket_draft="离线冒烟测试不生成真实工单。",
    )


@contextmanager
def _patched_runner_run(factory: Callable[..., SimpleNamespace]):
    import agent_runtime.copilot.runtime as runtime_module

    original = runtime_module.Runner.run

    async def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return factory(*args, **kwargs)

    runtime_module.Runner.run = fake_run
    try:
        yield
    finally:
        runtime_module.Runner.run = original


if __name__ == "__main__":
    raise SystemExit(main())
