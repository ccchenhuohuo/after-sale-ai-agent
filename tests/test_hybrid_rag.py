import importlib.util
import json
import sys
from pathlib import Path

from agent_runtime.settings import Settings
from agent_runtime.tools import rag


ROOT = Path(__file__).resolve().parents[1]
HISTORY_SCRIPT_PATH = ROOT / "scripts" / "build_history_rag_index.py"
MEDIA_SCRIPT_PATH = ROOT / "scripts" / "build_media_evidence_index.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_sample_staging(staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True)
    topics = [
        {
            "topicId": "thread:tb15",
            "threadId": "tb15",
            "rootMessageId": "m1",
            "topicLink": "https://feishu.test/tb15",
            "topicCreateTime": "2026-05-30 06:33:00.000",
            "topicLastMessageTime": "2026-05-30 06:35:00.000",
            "messageCount": 2,
            "mediaCount": 1,
            "messages": [
                {"topic_sequence": 1, "sender_name": "客服", "msg_type": "text", "content_text": "TB15 里面全部出来放不进去了"},
                {"topic_sequence": 2, "sender_name": "鲁工", "msg_type": "text", "content_text": "胶水失效，补发3090-PJ01"},
            ],
        }
    ]
    events = [
        {
            "事件ID": "AF-TB15",
            "话题ID": "thread:tb15",
            "话题链接": "https://feishu.test/tb15",
            "事件标题": "TB15 胶水失效",
            "产品型号/SKU": "TB15, 3090-PJ01",
            "问题大类": "产品质量问题",
            "异常分类": "功能问题",
            "解决方案类型": "补发配件",
            "客户症状摘要": "TB15 里面出来放不回",
            "处理建议/结论": "胶水失效，补发3090-PJ01",
            "媒体数量": 1,
        }
    ]
    media_assets = [
        {
            "话题ID": "thread:tb15",
            "媒体ID": "media-tb15-video",
            "媒体类型": "video",
            "原消息链接": "https://feishu.test/tb15#message-video",
            "message_id": "m-tb15-video",
            "归档策略": "topic_media",
            "分析状态": "pending",
        }
    ]
    (staging_dir / "topics.ndjson").write_text("\n".join(json.dumps(topic, ensure_ascii=False) for topic in topics), encoding="utf-8")
    (staging_dir / "event_candidates.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    (staging_dir / "media_assets.json").write_text(json.dumps(media_assets, ensure_ascii=False), encoding="utf-8")


def test_search_issue_history_text_returns_hybrid_evidence_package(tmp_path, monkeypatch):
    history_builder = load_module("build_history_rag_index_hybrid_test", HISTORY_SCRIPT_PATH)
    media_builder = load_module("build_media_evidence_index_hybrid_test", MEDIA_SCRIPT_PATH)
    staging_dir = tmp_path / "staging"
    history_index = tmp_path / "history_index"
    media_index = tmp_path / "media_index"
    write_sample_staging(staging_dir)
    history_builder.build_index(staging_dir, history_index)
    media_builder.build_index(staging_dir, media_index)

    settings = Settings(
        history_rag_index_path=str(history_index),
        history_rag_provider="local_hash",
        history_rag_require_remote_models=False,
        history_rag_top_k=4,
        history_rag_top_n=2,
        media_rag_index_path=str(media_index),
        media_rag_provider="local_hash",
        media_rag_top_k=4,
        media_rag_top_n=2,
    )
    original_search_history_rag = rag.search_history_rag
    original_search_media_rag = rag.search_media_rag
    monkeypatch.setattr(
        rag,
        "search_sku_catalog_text",
        lambda query, limit=3: "- SKU：TB15\n  SPU：TB15\n  命中原因：sku精确匹配",
    )
    monkeypatch.setattr(
        rag,
        "search_history_rag",
        lambda query, product_model=None, issue_type=None: original_search_history_rag(
            query,
            product_model=product_model,
            issue_type=issue_type,
            settings=settings,
        ),
    )
    monkeypatch.setattr(
        rag,
        "search_media_rag",
        lambda query, product_model=None: original_search_media_rag(
            query,
            product_model=product_model,
            settings=settings,
        ),
    )

    result = rag.search_issue_history_text("TB15 胶水失效 PJ01 补发", product_model="TB15")

    assert "混合 RAG 证据打包结果" in result
    assert "SKU 精准匹配：" in result
    assert "SKU：TB15" in result
    assert "文本历史参考：" in result
    assert "媒体观察证据：" in result
    assert "已审核群聊历史 FAQ" in result
    assert "未审核媒体观察证据" in result
    assert "thread:tb15" in result
