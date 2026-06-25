import asyncio
import json

import agent_runtime.production_healthcheck as healthcheck
from agent_runtime.settings import Settings


def test_env_presence_reports_key_status_without_secret_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "LLM_API_KEY=sk-secret-value-should-not-leak",
                "LLM_BASE_URL=https://model.example.test/v1",
                "SUPPORT_AGENT_MODEL=claude-code-cli",
                "FEISHU_APP_ID=cli_test",
                "FEISHU_APP_SECRET=app-secret-value-should-not-leak",
                "FEISHU_SUPPORT_GROUP_CHAT_ID=oc_secret_group",
                "FEISHU_MESSAGE_ADMISSION_MODE=listen_new_topics",
                "FEISHU_THREAD_CONTEXT_ENABLED=true",
                "SKU_CATALOG_PATH=data/sku.csv",
                "HISTORY_RAG_INDEX_PATH=data/history",
                "FORMAL_KB_INDEX_PATH=data/formal",
                "MEDIA_RAG_INDEX_PATH=data/media",
            ]
        ),
        encoding="utf-8",
    )

    report = healthcheck._check_env_presence(healthcheck._read_env_keys(env_path))
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["ok"] is True
    assert report["present"]["FEISHU_APP_SECRET"] is True
    assert report["secret_keys_configured"]["FEISHU_APP_SECRET"] is True
    assert "app-secret-value-should-not-leak" not in serialized
    assert "sk-secret-value-should-not-leak" not in serialized
    assert "oc_secret_group" not in serialized


def test_feishu_permission_probe_redacts_token_and_raw_chat_id():
    class FakeResponse:
        status_code = 400

        def json(self):
            return {"code": 230027, "msg": "Lack permission: im:message.group_msg"}

    class FakeClient:
        async def get(self, url, *, params=None, headers=None):
            assert headers["Authorization"] == "Bearer tenant-token-should-not-leak"
            assert params["container_id"] == "oc_raw_chat_should_not_leak"
            return FakeResponse()

    result = asyncio.run(
        healthcheck._probe_feishu_chat(
            FakeClient(),
            "tenant-token-should-not-leak",
            "oc_raw_chat_should_not_leak",
        )
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["ok"] is False
    assert result["code"] == 230027
    assert result["missing_scope"] == "im:message.group_msg"
    assert "tenant-token-should-not-leak" not in serialized
    assert "oc_raw_chat_should_not_leak" not in serialized


def test_phoenix_endpoint_check_uses_ulanzicloud_default_host():
    settings = Settings(phoenix_tracing_enabled=True)

    report = healthcheck._check_phoenix_endpoint(settings)

    assert report["ok"] is True
    assert report["expected_host"] == "100.111.223.41"
    assert report["configured_expected_host"] is True


def test_deploy_revision_commit_parses_key_value_file():
    text = "branch=codex/test\ncommit=abc123\nserver_tests=passed\n"

    assert healthcheck._deploy_revision_commit(text) == "abc123"
