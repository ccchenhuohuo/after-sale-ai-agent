import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "ingest_feishu_support_data.py"


def load_ingest_module():
    spec = importlib.util.spec_from_file_location("ingest_feishu_support_data", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ingest_config_reads_environment(monkeypatch):
    module = load_ingest_module()
    monkeypatch.setenv("FEISHU_SUPPORT_BASE_TOKEN", "base_token")
    monkeypatch.setenv("FEISHU_SUPPORT_GROUP_CHAT_ID", "group_chat")
    monkeypatch.setenv("FEISHU_SUPPORT_LUZ_CHAT_ID", "luz_chat")
    monkeypatch.setenv("FEISHU_SUPPORT_TABLE_EVENTS", "events")
    monkeypatch.setenv("FEISHU_SUPPORT_TABLE_RAW_MESSAGES", "raw")
    monkeypatch.setenv("FEISHU_SUPPORT_TABLE_MEDIA", "media")
    monkeypatch.setenv("FEISHU_SUPPORT_TABLE_ACTIONS", "actions")
    args = argparse.Namespace(
        base_token="",
        group_chat_id="",
        luz_chat_id="",
        table_events="",
        table_raw_messages="",
        table_media="",
        table_actions="",
    )

    config = module.IngestConfig.from_args(args)

    assert config.base_token == "base_token"
    assert config.group_chat_id == "group_chat"
    assert config.table_actions == "actions"


def test_apply_requires_write_config():
    module = load_ingest_module()
    config = module.IngestConfig(
        base_token="",
        group_chat_id="group",
        luz_chat_id="luz",
        table_events="",
        table_raw_messages="",
        table_media="",
        table_actions="",
    )

    with pytest.raises(SystemExit):
        module.ensure_config(config, apply=True, sync_drive_images=False)


def test_drive_sync_without_apply_only_summarizes_local_queue(tmp_path):
    module = load_ingest_module()
    run_dir = tmp_path / "run"
    image_dir = run_dir / "images"
    image_dir.mkdir(parents=True)
    (image_dir / "img_1.jpg").write_bytes(b"fake")
    payload = {
        "fields": ["资源唯一键", "file_key"],
        "rows": [["message:img_1", "img_1"], ["message:img_2", "img_2"]],
    }
    (run_dir / "media-1.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    summary = module.summarize_drive_image_sync_plan(run_dir)

    assert summary["apply"] is False
    assert summary["total_run_media"] == 2
    assert summary["local_images_available"] == 1
    assert summary["missing_local"] == 1
