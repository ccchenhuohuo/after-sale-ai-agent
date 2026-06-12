from pathlib import Path

from agent_runtime.llm import build_run_config
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

    assert config.trace_metadata["app"] == "ulanzi-after-sell-copilot"
    assert config.trace_metadata["llm_model"] == "deepseek-v4-flash"
    assert config.trace_metadata["source"] == "test"
