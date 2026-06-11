from agent_runtime.llm import build_run_config
from agent_runtime.settings import Settings


def test_run_config_uses_current_project_trace_app_name():
    settings = Settings(support_agent_model="deepseek-v4-flash")

    config = build_run_config(settings, metadata={"source": "test"})

    assert config.trace_metadata["app"] == "ulanzi-after-sell-copilot"
    assert config.trace_metadata["llm_model"] == "deepseek-v4-flash"
    assert config.trace_metadata["source"] == "test"
