import json
from pathlib import Path


SIDECAR_DIR = Path(__file__).resolve().parents[1] / "deploy" / "openclaw_sidecar"


def test_openclaw_sidecar_manifest_pins_runtime_and_support_copilot_smoke():
    package = json.loads((SIDECAR_DIR / "package.json").read_text(encoding="utf-8"))
    lockfile = json.loads((SIDECAR_DIR / "package-lock.json").read_text(encoding="utf-8"))

    assert package["packageManager"] == "npm@11.17.0"
    assert package["scripts"]["smoke:support-copilot"] == "node smoke-openclaw-feishu.mjs"
    assert package["dependencies"]["openclaw"] == "2026.6.6"
    assert package["dependencies"]["@larksuite/openclaw-lark"] == "2026.6.10"
    assert lockfile["packages"][""]["dependencies"]["openclaw"] == "2026.6.6"
    assert lockfile["packages"][""]["dependencies"]["@larksuite/openclaw-lark"] == "2026.6.10"


def test_openclaw_sidecar_smoke_script_preserves_endpoint_contract():
    script = (SIDECAR_DIR / "smoke-openclaw-feishu.mjs").read_text(encoding="utf-8")

    assert "/channels/openclaw-feishu/support-case" in script
    assert "OPENCLAW_FEISHU_BRIDGE_SECRET" in script
    assert "x-openclaw-feishu-secret" in script
    assert "batchId" in script
    assert "messages" in script
    assert "thread_reply" in script
    assert "replyInThread" in script
    assert "replyToMessageId" in script
