import os

from openai import AsyncOpenAI
from agents import (
    RunConfig,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
    set_tracing_export_api_key,
)

from agent_runtime.settings import Settings, get_settings


_CONFIGURED = False
_PHOENIX_CONFIGURED = False


def _is_openai_endpoint(base_url: str) -> bool:
    normalized = base_url.lower()
    return "api.openai.com" in normalized or "api.openai.azure.com" in normalized


def _configure_phoenix_tracing(settings: Settings) -> None:
    global _PHOENIX_CONFIGURED

    if _PHOENIX_CONFIGURED or not settings.phoenix_tracing_enabled:
        return

    try:
        from phoenix.otel import register
    except ImportError as exc:
        raise RuntimeError(
            "Phoenix tracing is enabled but dependencies are missing. "
            "Install: pip install arize-phoenix-otel openinference-instrumentation-openai-agents"
        ) from exc

    register(
        endpoint=settings.phoenix_collector_endpoint,
        project_name=settings.phoenix_project_name,
        auto_instrument=True,
    )
    _PHOENIX_CONFIGURED = True


def configure_agents_runtime(settings: Settings = None) -> Settings:
    global _CONFIGURED

    settings = settings or get_settings()
    if _CONFIGURED:
        return settings

    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is required. Copy .env.example to .env and fill it in.")

    tracing_key = settings.openai_tracing_api_key or os.getenv("OPENAI_API_KEY", "")
    use_llm_client_for_tracing = (
        not settings.support_agent_tracing_disabled
        and not tracing_key
        and _is_openai_endpoint(settings.llm_base_url)
    )

    if not settings.support_agent_tracing_disabled and not tracing_key and not use_llm_client_for_tracing:
        raise RuntimeError(
            "OPENAI_TRACING_API_KEY is required when tracing is enabled with a non-OpenAI "
            "LLM provider. Use your platform.openai.com API key for tracing and keep "
            "LLM_API_KEY as the DeepSeek key."
        )

    if settings.openai_org_id:
        os.environ["OPENAI_ORG_ID"] = settings.openai_org_id
    if settings.openai_project_id:
        os.environ["OPENAI_PROJECT_ID"] = settings.openai_project_id

    _configure_phoenix_tracing(settings)

    client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    set_default_openai_client(client, use_for_tracing=use_llm_client_for_tracing)
    if settings.support_agent_use_chat_completions:
        set_default_openai_api("chat_completions")

    if tracing_key:
        set_tracing_export_api_key(tracing_key)

    set_tracing_disabled(settings.support_agent_tracing_disabled)

    _CONFIGURED = True
    return settings


def build_run_config(
    settings: Settings = None,
    group_id: str = None,
    metadata: dict = None,
) -> RunConfig:
    settings = settings or get_settings()
    trace_metadata = {
        "app": "ulanzi-after-sell-copilot",
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.support_agent_model,
    }
    if metadata:
        trace_metadata.update(metadata)

    return RunConfig(
        tracing_disabled=settings.support_agent_tracing_disabled,
        trace_include_sensitive_data=settings.support_agent_trace_include_sensitive_data,
        workflow_name=settings.support_agent_trace_workflow_name,
        group_id=group_id,
        trace_metadata=trace_metadata,
    )
