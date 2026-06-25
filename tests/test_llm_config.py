from pathlib import Path

import pytest

import agent_runtime.llm as llm_module
from agent_runtime.llm import build_run_config, configure_agents_runtime
from agent_runtime.settings import Settings


def test_env_example_covers_runtime_settings():
    env_path = ".env.example"
    env_keys = {
        line.split("=", 1)[0]
        for line in Path(env_path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }

    expected = {
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "SUPPORT_AGENT_MODEL",
        "SUPPORT_AGENT_SESSION_LIMIT",
        "SUPPORT_AGENT_SESSION_DB_PATH",
        "SUPPORT_AGENT_OPENAI_HOSTED_TRACING_ENABLED",
        "SUPPORT_AGENT_TRACING_DISABLED",
        "SUPPORT_AGENT_TRACE_INCLUDE_SENSITIVE_DATA",
        "OPENAI_TRACING_API_KEY",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_SUPPORT_GROUP_CHAT_ID",
        "FEISHU_BOT_OPEN_ID",
        "FEISHU_RUNTIME_DB_PATH",
        "HISTORY_RAG_INDEX_PATH",
        "MEDIA_RAG_INDEX_PATH",
        "BAILIAN_API_KEY",
        "DASHSCOPE_API_KEY",
    }

    assert expected <= env_keys


def test_project_python_and_openai_sdk_dependency_bounds_are_pinned():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    constraints = Path("constraints.txt").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.10"' in pyproject
    assert '"openai>=2.38,<3"' in pyproject
    assert '"openai-agents>=0.17,<0.18"' in pyproject
    assert "eval-type-backport" not in pyproject
    assert "openai==2.38.0" in constraints
    assert "openai-agents==0.17.3" in constraints


def test_run_config_uses_current_project_trace_app_name():
    settings = Settings(support_agent_model="deepseek-v4-flash")

    config = build_run_config(settings, metadata={"source": "test"})

    assert config.trace_metadata["app"] == "VIJIM-after-sale-copilot"
    assert config.trace_metadata["llm_model"] == "deepseek-v4-flash"
    assert config.trace_metadata["source"] == "test"
    assert config.trace_include_sensitive_data is True


def test_run_config_can_explicitly_minimize_sensitive_trace_data():
    settings = Settings(support_agent_trace_include_sensitive_data=False)

    config = build_run_config(settings)

    assert config.trace_include_sensitive_data is False


def test_configure_agents_runtime_disables_openai_hosted_trace_processors_by_default(monkeypatch):
    calls = []

    monkeypatch.setattr(llm_module, "_CONFIGURED", False)
    monkeypatch.setattr(llm_module, "_configure_phoenix_tracing", lambda settings: calls.append(("phoenix", settings.phoenix_tracing_enabled)))
    monkeypatch.setattr(llm_module, "set_default_openai_client", lambda client, use_for_tracing=False: calls.append(("client", use_for_tracing)))
    monkeypatch.setattr(llm_module, "set_default_openai_api", lambda api: calls.append(("api", api)))
    monkeypatch.setattr(llm_module, "set_trace_processors", lambda processors: calls.append(("processors", processors)))
    monkeypatch.setattr(llm_module, "set_tracing_export_api_key", lambda key: calls.append(("trace_key", key)))
    monkeypatch.setattr(llm_module, "set_tracing_disabled", lambda disabled: calls.append(("disabled", disabled)))

    settings = Settings(
        llm_api_key="provider-key",
        llm_base_url="https://api.deepseek.com",
        support_agent_tracing_disabled=False,
        support_agent_openai_hosted_tracing_enabled=False,
        phoenix_tracing_enabled=False,
    )

    configure_agents_runtime(settings)

    assert ("phoenix", False) in calls
    assert ("client", False) in calls
    assert ("processors", []) in calls
    assert ("disabled", False) in calls
    assert not any(call[0] == "trace_key" for call in calls)


def test_configure_agents_runtime_uses_only_phoenix_when_phoenix_is_enabled(monkeypatch):
    calls = []

    monkeypatch.setattr(llm_module, "_CONFIGURED", False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-tracing-key-that-should-be-ignored")
    monkeypatch.setattr(llm_module, "_configure_phoenix_tracing", lambda settings: calls.append(("phoenix", settings.phoenix_tracing_enabled)))
    monkeypatch.setattr(llm_module, "set_default_openai_client", lambda client, use_for_tracing=False: calls.append(("client", use_for_tracing)))
    monkeypatch.setattr(llm_module, "set_default_openai_api", lambda api: calls.append(("api", api)))
    monkeypatch.setattr(llm_module, "set_trace_processors", lambda processors: calls.append(("processors", processors)))
    monkeypatch.setattr(llm_module, "set_tracing_export_api_key", lambda key: calls.append(("trace_key", key)))
    monkeypatch.setattr(llm_module, "set_tracing_disabled", lambda disabled: calls.append(("disabled", disabled)))

    settings = Settings(
        llm_api_key="provider-key",
        llm_base_url="https://api.deepseek.com",
        support_agent_tracing_disabled=False,
        support_agent_openai_hosted_tracing_enabled=True,
        openai_tracing_api_key="explicit-openai-tracing-key-that-should-be-ignored",
        phoenix_tracing_enabled=True,
    )

    configure_agents_runtime(settings)

    assert ("phoenix", True) in calls
    assert ("client", False) in calls
    assert ("disabled", False) in calls
    assert not any(call == ("processors", []) for call in calls)
    assert not any(call[0] == "trace_key" for call in calls)


def test_configure_agents_runtime_requires_tracing_key_only_when_hosted_tracing_enabled(monkeypatch):
    monkeypatch.setattr(llm_module, "_CONFIGURED", False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings = Settings(
        llm_api_key="provider-key",
        llm_base_url="https://api.deepseek.com",
        support_agent_tracing_disabled=False,
        support_agent_openai_hosted_tracing_enabled=True,
        phoenix_tracing_enabled=False,
        openai_tracing_api_key="",
    )

    with pytest.raises(RuntimeError, match="OPENAI_TRACING_API_KEY"):
        configure_agents_runtime(settings)


def test_configure_agents_runtime_configures_hosted_tracing_when_enabled(monkeypatch):
    calls = []

    monkeypatch.setattr(llm_module, "_CONFIGURED", False)
    monkeypatch.setattr(llm_module, "_configure_phoenix_tracing", lambda settings: None)
    monkeypatch.setattr(llm_module, "set_default_openai_client", lambda client, use_for_tracing=False: calls.append(("client", use_for_tracing)))
    monkeypatch.setattr(llm_module, "set_default_openai_api", lambda api: calls.append(("api", api)))
    monkeypatch.setattr(llm_module, "set_trace_processors", lambda processors: calls.append(("processors", processors)))
    monkeypatch.setattr(llm_module, "set_tracing_export_api_key", lambda key: calls.append(("trace_key", key)))
    monkeypatch.setattr(llm_module, "set_tracing_disabled", lambda disabled: calls.append(("disabled", disabled)))

    settings = Settings(
        llm_api_key="provider-key",
        llm_base_url="https://api.deepseek.com",
        support_agent_tracing_disabled=False,
        support_agent_openai_hosted_tracing_enabled=True,
        phoenix_tracing_enabled=False,
        openai_tracing_api_key="trace-key",
    )

    configure_agents_runtime(settings)

    assert ("trace_key", "trace-key") in calls
    assert ("client", False) in calls
    assert ("disabled", False) in calls
    assert not any(call == ("processors", []) for call in calls)
