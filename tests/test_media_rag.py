import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

from agent_runtime.settings import Settings
from agent_runtime.tools import media_rag
from agent_runtime.tools.media_rag import search_media_rag


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "build_media_evidence_index.py"


def load_builder_module():
    spec = importlib.util.spec_from_file_location("build_media_evidence_index", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_sample_media_staging(staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True)
    events = [
        {
            "话题ID": "thread:tb15",
            "话题链接": "https://feishu.test/tb15",
            "事件标题": "TB15 胶水失效",
            "产品型号/SKU": "TB15, 3090-PJ01",
            "问题大类": "产品质量问题",
            "解决方案类型": "补发配件",
            "客户症状摘要": "TB15 内部结构脱落，胶水失效，PJ01 需要补发",
            "处理建议/结论": "疑似供应商制成问题，补发3090-PJ01",
        },
        {
            "话题ID": "thread:t081",
            "话题链接": "https://feishu.test/t081",
            "事件标题": "T081 一推就掉",
            "产品型号/SKU": "T081",
            "问题大类": "产品质量问题",
            "解决方案类型": "换新",
            "客户症状摘要": "T081 一推就掉，有图片",
            "处理建议/结论": "制成工艺问题，给客户更换",
        },
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
        },
        {
            "话题ID": "thread:t081",
            "媒体ID": "media-t081-image",
            "媒体类型": "image",
            "原消息链接": "https://feishu.test/t081#message-image",
            "message_id": "m-t081-image",
            "归档策略": "topic_media",
            "分析状态": "pending",
        },
    ]
    (staging_dir / "event_candidates.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    (staging_dir / "media_assets.json").write_text(json.dumps(media_assets, ensure_ascii=False), encoding="utf-8")


def test_build_media_evidence_index_creates_cases_chunks_and_manifest(tmp_path):
    module = load_builder_module()
    staging_dir = tmp_path / "staging"
    output_dir = tmp_path / "index"
    write_sample_media_staging(staging_dir)

    manifest = module.build_index(staging_dir, output_dir)

    chunks = (output_dir / "media_chunks.jsonl").read_text(encoding="utf-8").splitlines()
    first_chunk = json.loads(chunks[0])
    assert manifest["case_count"] == 2
    assert manifest["chunk_count"] == 4
    assert manifest["planned_embedding_model"] == "qwen3-vl-embedding"
    assert manifest["planned_rerank_model"] == "qwen3-vl-rerank"
    assert first_chunk["topic_id"]
    assert first_chunk["topic_link"]
    assert first_chunk["text"]
    assert first_chunk["evidence_level"] == "media_observation_unreviewed"
    assert first_chunk["can_be_formal_evidence"] is False
    assert (output_dir / "media_embeddings.npy").exists()


def test_build_media_evidence_index_uses_bailian_vl_for_local_image(tmp_path, monkeypatch):
    module = load_builder_module()
    staging_dir = tmp_path / "staging"
    output_dir = tmp_path / "index"
    write_sample_media_staging(staging_dir)
    media_dir = staging_dir / "media"
    media_dir.mkdir()
    (media_dir / "media-t081-image.jpg").write_bytes(b"fake-image")
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output": {"embeddings": [{"embedding": [1.0, 0.0, 0.0, 0.0]}]}}

    def fake_post(url, json, headers, timeout):
        calls.append(json)
        assert url == "https://dashscope.test/multimodal"
        assert json["model"] == "qwen3-vl-embedding"
        assert json["parameters"]["enable_fusion"] is True
        assert json["parameters"]["dimension"] == 4
        assert json["input"]["contents"][1]["image"].startswith("data:image/jpeg;base64,")
        assert headers["Authorization"] == "Bearer test-key"
        return Response()

    monkeypatch.setattr(module.httpx, "post", fake_post)

    manifest = module.build_index(
        staging_dir,
        output_dir,
        dimension=4,
        provider="bailian_vl",
        bailian_api_key="test-key",
        bailian_multimodal_embedding_base_url="https://dashscope.test/multimodal",
    )

    chunks = [json.loads(line) for line in (output_dir / "media_chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    image_chunk = next(chunk for chunk in chunks if chunk.get("media_id") == "media-t081-image")
    assert len(calls) == 1
    assert manifest["embedding_backend"] == "bailian_vl_mixed"
    assert manifest["vl_embedding_count"] == 1
    assert manifest["local_hash_fallback_count"] == 3
    assert image_chunk["embedding_backend"] == "bailian_vl_fusion"
    assert image_chunk["media_file_available"] is True


def test_build_media_evidence_index_uses_bailian_vl_for_video_url(tmp_path, monkeypatch):
    module = load_builder_module()
    staging_dir = tmp_path / "staging"
    output_dir = tmp_path / "index"
    write_sample_media_staging(staging_dir)
    media_assets_path = staging_dir / "media_assets.json"
    media_assets = json.loads(media_assets_path.read_text(encoding="utf-8"))
    media_assets[0]["媒体URL"] = "https://cdn.test/tb15.mp4"
    media_assets_path.write_text(json.dumps(media_assets, ensure_ascii=False), encoding="utf-8")
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output": {"embeddings": [{"embedding": [1.0, 0.0, 0.0, 0.0]}]}}

    def fake_post(url, json, headers, timeout):
        calls.append(json)
        assert json["model"] == "qwen3-vl-embedding"
        assert json["input"]["contents"][1] == {"video": "https://cdn.test/tb15.mp4"}
        return Response()

    monkeypatch.setattr(module.httpx, "post", fake_post)

    manifest = module.build_index(
        staging_dir,
        output_dir,
        dimension=4,
        provider="bailian_vl",
        bailian_api_key="test-key",
    )

    chunks = [json.loads(line) for line in (output_dir / "media_chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    video_chunk = next(chunk for chunk in chunks if chunk.get("media_id") == "media-tb15-video")
    assert len(calls) == 1
    assert manifest["vl_embedding_count"] == 1
    assert manifest["media_url_count"] == 1
    assert video_chunk["media_url"] == "https://cdn.test/tb15.mp4"
    assert video_chunk["media_vl_content_type"] == "video"


def test_search_media_rag_returns_unreviewed_media_evidence(tmp_path):
    module = load_builder_module()
    staging_dir = tmp_path / "staging"
    output_dir = tmp_path / "index"
    write_sample_media_staging(staging_dir)
    module.build_index(staging_dir, output_dir)
    settings = Settings(media_rag_index_path=str(output_dir), media_rag_provider="local_hash", media_rag_top_k=4, media_rag_top_n=2)

    result = search_media_rag("TB15 胶水失效 PJ01 补发 视频", product_model="TB15", settings=settings)

    assert "未审核媒体观察证据" in result
    assert "thread:tb15" in result
    assert "https://feishu.test/tb15" in result
    assert "相似度" in result
    assert "重排分" in result


def test_search_media_rag_uses_query_vector_refs(tmp_path):
    output_dir = tmp_path / "index"
    vector_dir = tmp_path / "vectors"
    output_dir.mkdir()
    vector_dir.mkdir()
    chunks = [
        {
            "chunk_id": "media_a",
            "topic_id": "thread:a",
            "topic_link": "https://feishu.test/a",
            "sku": "A001",
            "media_type": "image",
            "media_id": "img_a",
            "text": "A001 普通图片，不相关。",
        },
        {
            "chunk_id": "media_b",
            "topic_id": "thread:b",
            "topic_link": "https://feishu.test/b",
            "sku": "B002",
            "media_type": "image",
            "media_id": "img_b",
            "text": "B002 产品损坏图片。",
        },
    ]
    (output_dir / "media_chunks.jsonl").write_text(
        "\n".join(json.dumps(chunk, ensure_ascii=False) for chunk in chunks),
        encoding="utf-8",
    )
    (output_dir / "manifest.json").write_text(json.dumps({"source": "test"}, ensure_ascii=False), encoding="utf-8")
    np.save(output_dir / "media_embeddings.npy", np.asarray([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype=np.float32))
    np.save(vector_dir / "vec_query_damage.npy", np.asarray([[0.0, 1.0, 0.0, 0.0]], dtype=np.float32))
    settings = Settings(
        media_rag_index_path=str(output_dir),
        media_rag_provider="local_hash",
        media_rag_embedding_dimension=4,
        media_rag_top_k=2,
        media_rag_top_n=1,
        support_vector_artifact_dir=str(vector_dir),
    )

    result = search_media_rag("完全不相关文本", settings=settings, vector_refs=["vec_query_damage"])

    assert "thread:b" in result
    assert "thread:a" not in result
    assert "视觉向量引用命中" in result
    assert "检索通道：视觉向量" in result


def test_search_media_rag_does_not_fallback_to_unrelated_product(tmp_path):
    module = load_builder_module()
    staging_dir = tmp_path / "staging"
    output_dir = tmp_path / "index"
    write_sample_media_staging(staging_dir)
    module.build_index(staging_dir, output_dir)
    settings = Settings(media_rag_index_path=str(output_dir), media_rag_provider="local_hash", media_rag_top_k=4, media_rag_top_n=2)

    result = search_media_rag("不存在的型号 一推就掉 图片", product_model="ZZ999", settings=settings)

    assert "未查询到可信媒体观察证据" in result
    assert "thread:tb15" not in result
    assert "thread:t081" not in result


def test_bailian_vl_embedding_payload_is_normalized(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output": {"embeddings": [{"embedding": [3.0, 4.0]}]}}

    def fake_post(url, json, headers, timeout):
        assert url == "https://dashscope.test/multimodal"
        assert json["model"] == "qwen3-vl-embedding"
        assert json["input"]["contents"] == [{"text": "测试"}]
        assert json["parameters"] == {"enable_fusion": True, "dimension": 2}
        assert headers["Authorization"] == "Bearer test-key"
        return Response()

    monkeypatch.setattr(media_rag.httpx, "post", fake_post)
    settings = Settings(
        bailian_api_key="test-key",
        bailian_multimodal_embedding_base_url="https://dashscope.test/multimodal",
        media_rag_embedding_dimension=2,
    )

    vector = media_rag._bailian_vl_embedding(settings, [{"text": "测试"}])

    assert vector is not None
    assert vector.shape == (1, 2)
    assert round(float(vector[0][0]), 4) == 0.6
    assert round(float(vector[0][1]), 4) == 0.8


def test_bailian_vl_rerank_maps_result_index_to_chunk_id(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output": {"results": [{"index": 1, "relevance_score": 0.88}, {"index": 0, "relevance_score": 0.21}]}}

    def fake_post(url, json, headers, timeout):
        assert url == "https://dashscope.test/vl-rerank"
        assert json["model"] == "qwen3-vl-rerank"
        assert json["input"]["query"] == {"text": "图片证据"}
        assert json["parameters"]["top_n"] == 2
        assert headers["Authorization"] == "Bearer test-key"
        return Response()

    monkeypatch.setattr(media_rag.httpx, "post", fake_post)
    settings = Settings(bailian_api_key="test-key", bailian_vl_rerank_base_url="https://dashscope.test/vl-rerank")
    chunks = [{"chunk_id": "a", "text": "无关"}, {"chunk_id": "b", "text": "图片证据"}]

    scores = media_rag._bailian_vl_rerank(settings, "图片证据", chunks)

    assert scores == {"b": 0.88, "a": 0.21}


def test_bailian_vl_rerank_uses_video_url_document(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output": {"results": [{"index": 0, "relevance_score": 0.77}]}}

    def fake_post(url, json, headers, timeout):
        assert json["input"]["documents"] == [{"video": "https://cdn.test/tb15.mp4"}]
        return Response()

    monkeypatch.setattr(media_rag.httpx, "post", fake_post)
    settings = Settings(bailian_api_key="test-key")
    chunks = [{"chunk_id": "video", "text": "TB15 视频", "media_type": "video", "media_url": "https://cdn.test/tb15.mp4"}]

    scores = media_rag._bailian_vl_rerank(settings, "TB15 视频", chunks)

    assert scores == {"video": 0.77}


def test_bailian_vl_rerank_resolves_relative_media_path_from_manifest(tmp_path, monkeypatch):
    staging_dir = tmp_path / "staging"
    media_dir = staging_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "proof.jpg").write_bytes(b"fake-image")
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output": {"results": [{"index": 0, "relevance_score": 0.91}]}}

    def fake_post(url, json, headers, timeout):
        calls.append(json)
        assert json["input"]["documents"][0]["image"].startswith("data:image/jpeg;base64,")
        return Response()

    monkeypatch.setattr(media_rag.httpx, "post", fake_post)
    settings = Settings(bailian_api_key="test-key")
    chunks = [
        {
            "chunk_id": "image",
            "text": "T081 图片证据",
            "media_type": "image",
            "media_file_path": "media/proof.jpg",
        }
    ]

    scores = media_rag._bailian_vl_rerank(
        settings,
        "T081 图片证据",
        chunks,
        index_dir=index_dir,
        manifest={"source_staging_dir": str(staging_dir)},
    )

    assert calls
    assert scores == {"image": 0.91}
