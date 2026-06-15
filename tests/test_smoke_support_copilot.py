import json

from scripts import smoke_support_copilot as smoke


def test_default_smoke_passes_without_external_credentials(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("OPENCLAW_FEISHU_BRIDGE_SECRET", raising=False)

    report = smoke.run_smoke()

    assert report["ok"] is True
    assert report["mode"] == "offline"
    assert {check["name"] for check in report["checks"]} >= {
        "imports_and_settings",
        "core_import_boundary",
        "openclaw_http_contract",
        "legacy_feishu_parse",
    }
    assert {scenario["name"] for scenario in report["scenarios"]} >= {
        "smoke_text_only",
        "smoke_openclaw_burst_equivalent",
        "smoke_damage_image",
        "smoke_mixed_text_image",
        "smoke_video_placeholder",
    }


def test_smoke_report_contains_replayable_pipeline_shape():
    report = smoke.run_smoke()

    for scenario in report["scenarios"]:
        assert scenario["ok"] is True
        assert scenario["route"]["input_modality"]
        assert scenario["ingestion_artifacts"]
        assert scenario["context"]["normalized_query"]
        assert "recommended_action" in scenario["coverage"]
        assert "recommended_action" in scenario["final_answer"]
        assert scenario["coverage"]["mention_enabled"] is False
        assert scenario["final_answer"]["mention_enabled"] is False
        assert scenario["final_answer"]["contract_issues"] == []

    video = _scenario(report, "smoke_video_placeholder")
    assert any(
        artifact["artifact_type"] == "video_sampling" and artifact["status"] == "unsupported"
        for artifact in video["ingestion_artifacts"]
    )

    rejected = _scenario(report, "smoke_rejected_attachment")
    assert any(
        artifact["artifact_type"] == "image_embedding" and artifact["status"] == "unsupported"
        for artifact in rejected["ingestion_artifacts"]
    )

    serialized = json.dumps(report, ensure_ascii=False)
    assert "/etc/passwd" not in serialized
    assert "[0.0" not in serialized


def test_smoke_returns_nonzero_style_report_for_failed_check(monkeypatch):
    monkeypatch.setattr(
        smoke,
        "_check_core_import_boundary",
        lambda: (_ for _ in ()).throw(AssertionError("forced smoke failure")),
    )

    report = smoke.run_smoke()

    assert report["ok"] is False
    failed = [check for check in report["checks"] if check["name"] == "core_import_boundary"][0]
    assert failed["ok"] is False
    assert "forced smoke failure" in failed["error"]


def test_live_smoke_requires_explicit_environment_flag(monkeypatch):
    monkeypatch.delenv("RUN_LIVE_SMOKE", raising=False)

    report = smoke.run_smoke(live=True)

    assert report["ok"] is False
    failed = [check for check in report["checks"] if check["name"] == "live_smoke_guard"][0]
    assert failed["ok"] is False
    assert "RUN_LIVE_SMOKE=1" in failed["error"]


def _scenario(report, name):
    return [scenario for scenario in report["scenarios"] if scenario["name"] == name][0]
