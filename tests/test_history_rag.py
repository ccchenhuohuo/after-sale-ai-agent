import importlib.util
import json
import sys
from pathlib import Path

from agent_runtime.settings import Settings
from agent_runtime.tools import history_rag
from agent_runtime.tools.history_rag import search_history_rag


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "build_history_rag_index.py"


def load_builder_module():
    spec = importlib.util.spec_from_file_location("build_history_rag_index", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_sample_staging(staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True)
    topics = [
        {
            "topicId": "thread:t081",
            "threadId": "t081",
            "rootMessageId": "m1",
            "topicLink": "https://feishu.test/t081",
            "topicCreateTime": "2026-05-31 04:06:00.000",
            "topicLastMessageTime": "2026-05-31 04:35:00.000",
            "messageCount": 2,
            "mediaCount": 1,
            "messages": [
                {"topic_sequence": 1, "create_time": "2026-05-31 04:06:00.000", "sender_name": "客服", "msg_type": "post", "content_text": "T081 刚收到就一推就掉了 [图片]"},
                {"topic_sequence": 2, "create_time": "2026-05-31 04:35:00.000", "sender_name": "鲁工", "msg_type": "text", "content_text": "给客户更换吧，这是制成工艺问题"},
            ],
        },
        {
            "topicId": "thread:tb15",
            "threadId": "tb15",
            "rootMessageId": "m2",
            "topicLink": "https://feishu.test/tb15",
            "topicCreateTime": "2026-05-30 06:33:00.000",
            "topicLastMessageTime": "2026-05-30 06:35:00.000",
            "messageCount": 2,
            "mediaCount": 1,
            "messages": [
                {"topic_sequence": 1, "create_time": "2026-05-30 06:34:00.000", "sender_name": "客服", "msg_type": "text", "content_text": "TB15 里面全部出来放不进去了"},
                {"topic_sequence": 2, "create_time": "2026-05-30 06:35:00.000", "sender_name": "鲁工", "msg_type": "text", "content_text": "这是胶水失效了，是供应商制成问题，或者补发3090-PJ01进行更换"},
            ],
        },
        {
            "topicId": "thread:a053",
            "threadId": "a053",
            "rootMessageId": "m3",
            "topicLink": "https://feishu.test/a053",
            "topicCreateTime": "2026-05-31 04:12:00.000",
            "topicLastMessageTime": "2026-05-31 04:35:00.000",
            "messageCount": 1,
            "mediaCount": 0,
            "messages": [
                {"topic_sequence": 1, "create_time": "2026-05-31 04:12:00.000", "sender_name": "客服", "msg_type": "text", "content_text": "A053 接收器单独购买一个可以自动配对吗"},
            ],
        },
    ]
    events = [
        {"事件ID": "AF-T081", "话题ID": "thread:t081", "事件标题": "T081 一推就掉了", "产品型号/SKU": "T081", "问题大类": "产品质量问题", "异常分类": "功能问题", "解决方案类型": "换新", "客户症状摘要": "T081 一推就掉", "处理建议/结论": "制成工艺问题，给客户更换", "媒体数量": 1},
        {"事件ID": "AF-TB15", "话题ID": "thread:tb15", "事件标题": "TB15 胶水失效", "产品型号/SKU": "TB15, PJ01", "问题大类": "产品质量问题", "异常分类": "功能问题", "解决方案类型": "补发配件", "客户症状摘要": "TB15 里面出来放不回", "处理建议/结论": "胶水失效，补发3090-PJ01", "媒体数量": 1},
        {"事件ID": "AF-A053", "话题ID": "thread:a053", "事件标题": "A053 自动配对咨询", "产品型号/SKU": "A053", "问题大类": "其他", "异常分类": "信息咨询", "解决方案类型": "待确认", "客户症状摘要": "接收器单独购买能否自动配对", "处理建议/结论": "", "媒体数量": 0},
    ]
    (staging_dir / "topics.ndjson").write_text("\n".join(json.dumps(topic, ensure_ascii=False) for topic in topics), encoding="utf-8")
    (staging_dir / "event_candidates.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")


def test_build_history_rag_index_creates_cases_chunks_and_manifest(tmp_path):
    module = load_builder_module()
    staging_dir = tmp_path / "staging"
    output_dir = tmp_path / "index"
    write_sample_staging(staging_dir)

    manifest = module.build_index(staging_dir, output_dir)

    chunks = (output_dir / "history_chunks.jsonl").read_text(encoding="utf-8").splitlines()
    first_chunk = json.loads(chunks[0])
    assert manifest["case_count"] == 3
    assert manifest["chunk_count"] > 3
    assert first_chunk["topic_id"]
    assert first_chunk["topic_link"]
    assert first_chunk["text"]
    assert first_chunk["evidence_level"] == "reviewed_case"
    assert first_chunk["can_be_formal_evidence"] is False
    assert first_chunk["is_reviewed"] is True
    assert (output_dir / "embeddings.npy").exists()


def test_search_history_rag_returns_reviewed_reference(tmp_path):
    module = load_builder_module()
    staging_dir = tmp_path / "staging"
    output_dir = tmp_path / "index"
    write_sample_staging(staging_dir)
    module.build_index(staging_dir, output_dir)
    settings = Settings(
        history_rag_index_path=str(output_dir),
        history_rag_provider="local_hash",
        history_rag_require_remote_models=False,
        history_rag_top_k=8,
        history_rag_top_n=2,
    )

    result = search_history_rag("TB15 胶水失效 PJ01 补发", product_model="TB15", settings=settings)

    assert "已审核群聊历史 FAQ" in result
    assert "thread:tb15" in result
    assert "https://feishu.test/tb15" in result
    assert "相似度" in result
    assert "重排分" in result


def test_search_history_rag_does_not_fallback_to_unrelated_product(tmp_path):
    module = load_builder_module()
    staging_dir = tmp_path / "staging"
    output_dir = tmp_path / "index"
    write_sample_staging(staging_dir)
    module.build_index(staging_dir, output_dir)
    settings = Settings(
        history_rag_index_path=str(output_dir),
        history_rag_provider="local_hash",
        history_rag_require_remote_models=False,
        history_rag_top_k=8,
        history_rag_top_n=2,
    )

    result = search_history_rag("不存在的型号 一推就掉了", product_model="ZZ999", settings=settings)

    assert "未查询到可信历史参考" in result
    assert "thread:t081" not in result
    assert "thread:tb15" not in result


def test_bailian_embedding_payload_is_normalized(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [3.0, 4.0]}]}

    def fake_post(url, json, headers, timeout):
        assert url == "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
        assert json["model"] == "text-embedding-v4"
        assert headers["Authorization"] == "Bearer test-key"
        return Response()

    monkeypatch.setattr(history_rag.httpx, "post", fake_post)
    settings = Settings(bailian_api_key="test-key")

    vector = history_rag._bailian_embeddings(settings, ["测试"])

    assert vector is not None
    assert vector.shape == (1, 2)
    assert round(float(vector[0][0]), 4) == 0.6
    assert round(float(vector[0][1]), 4) == 0.8


def test_bailian_rerank_maps_result_index_to_chunk_id(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.2}]}

    def fake_post(url, json, headers, timeout):
        assert url == "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
        assert json["model"] == "qwen3-rerank"
        assert json["top_n"] == 2
        assert headers["Authorization"] == "Bearer test-key"
        return Response()

    monkeypatch.setattr(history_rag.httpx, "post", fake_post)
    settings = Settings(bailian_api_key="test-key")
    chunks = [{"chunk_id": "a", "text": "无关"}, {"chunk_id": "b", "text": "相关"}]

    scores = history_rag._bailian_rerank(settings, "查询", chunks)

    assert scores == {"b": 0.91, "a": 0.2}
