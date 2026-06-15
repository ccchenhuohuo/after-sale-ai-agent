import json
from pathlib import Path


SIDECAR_DIR = Path(__file__).resolve().parents[1] / "deploy" / "openclaw_sidecar"


def test_openclaw_sidecar_manifest_pins_runtime_and_support_copilot_smoke():
    package = json.loads((SIDECAR_DIR / "package.json").read_text(encoding="utf-8"))
    lockfile = json.loads((SIDECAR_DIR / "package-lock.json").read_text(encoding="utf-8"))

    assert package["packageManager"] == "npm@11.17.0"
    assert package["scripts"]["doctor:support-copilot"] == "node diagnose-openclaw-feishu.mjs"
    assert package["scripts"]["smoke:support-copilot"] == "node smoke-openclaw-feishu.mjs"
    assert package["dependencies"]["openclaw"] == "2026.6.6"
    assert package["dependencies"]["@larksuite/openclaw-lark"] == "2026.6.10"
    assert package["overrides"]["axios"] == "1.18.0"
    assert lockfile["packages"][""]["dependencies"]["openclaw"] == "2026.6.6"
    assert lockfile["packages"][""]["dependencies"]["@larksuite/openclaw-lark"] == "2026.6.10"
    assert lockfile["packages"]["node_modules/axios"]["version"] == "1.18.0"


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


def test_openclaw_sidecar_diagnose_script_preserves_preflight_contract():
    script = (SIDECAR_DIR / "diagnose-openclaw-feishu.mjs").read_text(encoding="utf-8")

    assert "SUPPORT_COPILOT_HEALTH_URL" in script
    assert "SUPPORT_COPILOT_URL" in script
    assert "/channels/openclaw-feishu/support-case" in script
    assert "openclaw-version" in script
    assert "openclaw-lark-cli-version" in script
    assert '"--tools-version", "2026.6.10"' in script
    assert "openclaw-config-validate" in script
    assert "openclaw-feishu-channel-status" in script
    assert "--allow-unconfigured" in script
    assert "support-copilot-health" in script


def test_openclaw_sidecar_deployment_docs_preserve_live_validation_gate():
    env_example = (SIDECAR_DIR / "support-copilot.env.example").read_text(encoding="utf-8")
    checklist = (SIDECAR_DIR / "live-e2e-checklist.md").read_text(encoding="utf-8")
    readme = (SIDECAR_DIR / "README.md").read_text(encoding="utf-8")

    assert "SUPPORT_COPILOT_URL" in env_example
    assert "OPENCLAW_FEISHU_BRIDGE_SECRET" in env_example
    assert "OPENCLAW_VERSION=2026.6.6" in env_example
    assert "OPENCLAW_LARK_PLUGIN_VERSION=2026.6.10" in env_example
    assert "/channels/openclaw-feishu/health" in checklist
    assert "doctor:support-copilot" in checklist
    assert "replyInThread=true" in checklist
    assert "corepack npm audit --omit=dev --audit-level=high" in checklist
    assert "Legacy `feishu-long-connection`" in checklist
    assert "axios@1.18.0" in readme
    assert "corepack npm audit --omit=dev --audit-level=high" in readme
    assert "support-copilot.env.example" in readme
    assert "live-e2e-checklist.md" in readme
