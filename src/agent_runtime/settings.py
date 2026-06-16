from functools import lru_cache
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    support_agent_model: str = "deepseek-v4-flash"
    support_agent_model_flash: str = "deepseek-v4-flash"
    support_agent_model_pro: str = "deepseek-v4-pro"
    support_agent_billing_mode: str = "API Usage Billing"
    support_agent_session_limit: int = 40
    support_agent_session_db_path: str = "data/feishu_runtime/agent_sessions.sqlite3"
    support_agent_use_chat_completions: bool = True
    support_intake_router_enabled: bool = False
    support_intake_router_model: str = ""
    support_context_assembler_enabled: bool = False
    support_context_assembler_model: str = ""
    support_ocr_provider: str = "disabled"
    support_ocr_model: str = "qwen-vl-plus"
    support_ocr_base_url: str = ""
    support_ocr_timeout_seconds: float = 60.0
    support_ocr_image_max_bytes: int = 7_000_000
    support_visual_understanding_provider: str = "disabled"
    support_visual_understanding_model: str = "qwen-vl-plus"
    support_visual_understanding_base_url: str = ""
    support_visual_understanding_timeout_seconds: float = 60.0
    support_visual_understanding_image_max_bytes: int = 7_000_000
    support_visual_understanding_max_images: int = 4
    support_video_sampling_enabled: bool = True
    support_video_sample_dir: str = "data/feishu_runtime/video_samples"
    support_video_sample_count: int = 3
    support_video_sample_interval_seconds: float = 3.0
    support_video_sample_timeout_seconds: float = 30.0
    support_video_probe_timeout_seconds: float = 10.0
    support_video_max_duration_seconds: float = 300.0
    support_video_max_width: int = 3840
    support_video_max_height: int = 2160
    support_video_ffmpeg_path: str = ""
    support_vector_index_namespace: str = "after_sales_v1"
    support_vector_artifact_dir: str = "data/feishu_runtime/vector_artifacts"
    support_asset_allowed_local_dirs: str = ""
    support_asset_allowed_url_hosts: str = ""
    support_asset_input_max_bytes: int = 25_000_000
    openclaw_sidecar_version: str = "2026.6.6"
    openclaw_lark_plugin_version: str = "2026.6.10"
    openclaw_feishu_bridge_secret: str = ""
    openclaw_feishu_require_secret: bool = True

    openai_tracing_api_key: str = ""
    openai_org_id: str = ""
    openai_project_id: str = ""
    support_agent_openai_hosted_tracing_enabled: bool = False
    support_agent_tracing_disabled: bool = True
    support_agent_trace_include_sensitive_data: bool = True
    support_agent_trace_workflow_name: str = "ulanzi after-sell copilot MVP"
    support_trace_admission_mode: str = "sample"
    support_trace_duplicate_events: bool = False
    support_trace_ignored_events: bool = False
    support_trace_admission_sample_rate: float = 0.05
    support_trace_runtime_pipeline: bool = True
    phoenix_tracing_enabled: bool = False
    phoenix_collector_endpoint: str = "http://opencloud.taild79054.ts.net:6006/v1/traces"
    phoenix_project_name: str = "agent-runtime-test"

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_support_group_chat_id: str = ""
    feishu_bot_mention_name: str = ""
    feishu_reply_max_chars: int = 3500
    feishu_runtime_db_path: str = "data/feishu_runtime/runtime.sqlite3"
    feishu_bot_open_id: str = ""
    feishu_allowed_user_open_ids: str = ""
    feishu_event_concurrency: int = 5
    feishu_event_max_age_seconds: int = 1800
    feishu_backfill_enabled: bool = True
    feishu_backfill_interval_seconds: float = 10.0
    feishu_backfill_lookback_seconds: int = 180
    feishu_backfill_page_size: int = 50
    feishu_bot_loop_max_turns: int = 3
    feishu_dedup_ttl_seconds: int = 43200
    feishu_dedup_max_items: int = 5000
    feishu_media_auto_accept_enabled: bool = False
    feishu_asset_download_enabled: bool = True
    feishu_asset_cache_dir: str = "data/feishu_runtime/assets"
    feishu_asset_download_max_bytes: int = 25_000_000

    support_agent_trigger_prefix: str = "AI分析："
    support_agent_sync_mode: bool = False
    sku_catalog_path: str = "data/sku_catalog/processed/2026-05-26/sku_support_catalog-2026-05-26.csv"
    history_rag_index_path: str = "data/history_rag/index/latest"
    history_rag_provider: str = "bailian"
    bailian_api_key: str = ""
    bailian_embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    bailian_rerank_base_url: str = "https://dashscope.aliyuncs.com/compatible-api/v1"
    history_rag_embedding_model: str = "text-embedding-v4"
    history_rag_rerank_model: str = "qwen3-rerank"
    history_rag_require_remote_models: bool = True
    history_rag_top_k: int = 10
    history_rag_top_n: int = 5
    formal_kb_source_dir: str = "data/formal_kb/source"
    formal_kb_index_path: str = "data/formal_kb/index/latest"
    formal_kb_provider: str = "local_hash"
    formal_kb_embedding_model: str = "text-embedding-v4"
    formal_kb_rerank_model: str = "qwen3-rerank"
    formal_kb_embedding_dimension: int = 768
    formal_kb_require_remote_models: bool = False
    formal_kb_top_k: int = 8
    formal_kb_top_n: int = 4
    media_rag_index_path: str = "data/media_rag/index/latest"
    media_rag_provider: str = "bailian_vl"
    media_rag_embedding_model: str = "qwen3-vl-embedding"
    media_rag_rerank_model: str = "qwen3-vl-rerank"
    bailian_multimodal_embedding_base_url: str = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
    bailian_vl_rerank_base_url: str = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    media_rag_embedding_dimension: int = 1024
    media_rag_require_vl_models: bool = False
    media_rag_top_k: int = 10
    media_rag_top_n: int = 5

    @property
    def resolved_bailian_api_key(self) -> str:
        return self.bailian_api_key or os.getenv("DASHSCOPE_API_KEY", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
