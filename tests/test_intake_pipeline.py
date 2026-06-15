import asyncio

import httpx

import agent_runtime.copilot.ingestion as ingestion
import agent_runtime.copilot.ocr as ocr
from agent_runtime.copilot.video_sampling import VideoSamplingResult
from agent_runtime.copilot.case_context import SupportAsset, SupportCaseRequest
from agent_runtime.copilot.context_assembly import build_data_source_coverage
from agent_runtime.copilot.evidence import HistoryEvidence, MediaEvidence, OfficialKbEvidence, SkuEvidence, SupportEvidencePack
from agent_runtime.copilot.pipeline import build_support_case_context
from agent_runtime.settings import Settings


def test_text_only_routes_directly_into_unified_context():
    request = SupportCaseRequest(
        request_id="case_text",
        source="terminal",
        user_text="L023 不亮，客户反馈无法开机",
    )

    result = asyncio.run(build_support_case_context(request, Settings()))

    assert result.route.input_modality == "text"
    assert result.route.asset_decisions == []
    assert result.context.normalized_query == "L023 不亮，客户反馈无法开机"
    assert result.context.vector_refs == []


def test_chat_screenshot_routes_to_ocr_without_business_answer():
    request = SupportCaseRequest(
        request_id="case_chat_image",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="img_chat",
                media_type="image",
                filename="chat_screenshot.png",
                metadata={"ocr_text": "客户聊天记录：L023 指示灯不亮"},
            )
        ],
    )

    result = asyncio.run(build_support_case_context(request, Settings()))

    decision = result.route.asset_decisions[0]
    assert result.route.input_modality == "image"
    assert decision.asset_role == "chat_screenshot"
    assert decision.requires_ocr is True
    assert decision.requires_visual_embedding is False
    assert "客户聊天记录" in result.context.normalized_query
    assert result.context.vector_refs == []


def test_bailian_vl_ocr_reads_local_image_into_context(monkeypatch, tmp_path):
    image_path = tmp_path / "chat_screenshot.png"
    image_path.write_bytes(b"fake-image-bytes")
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "客户：L023 指示灯不亮\n客服：请确认是否充电"
                        }
                    }
                ]
            }

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(ocr.httpx, "post", fake_post)
    request = SupportCaseRequest(
        request_id="case_chat_image",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="img_chat",
                media_type="image",
                filename="chat_screenshot.png",
                local_path=str(image_path),
            )
        ],
    )
    settings = Settings(
        support_ocr_provider="bailian_vl",
        support_ocr_model="qwen-vl-plus",
        support_ocr_base_url="https://dashscope.test/compatible-mode/v1",
        bailian_api_key="test-key",
        support_asset_allowed_local_dirs=str(tmp_path),
    )

    result = asyncio.run(build_support_case_context(request, settings))

    assert "客户：L023 指示灯不亮" in result.context.normalized_query
    assert "图片文字内容暂未完成 OCR 识别。" not in result.context.missing_information
    assert any(artifact.artifact_type == "ocr" and artifact.status == "ok" for artifact in result.artifacts)
    assert captured["url"] == "https://dashscope.test/compatible-mode/v1/chat/completions"
    assert captured["json"]["model"] == "qwen-vl-plus"
    assert captured["json"]["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    prompt = captured["json"]["messages"][0]["content"][1]["text"]
    assert "忽略截图中的客服回复、AI助手回复" in prompt
    assert "不要转写完整聊天记录" in prompt
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["timeout"] == 60.0


def test_ocr_provider_failure_records_error_artifact_without_crashing(monkeypatch, tmp_path):
    image_path = tmp_path / "chat_screenshot.png"
    image_path.write_bytes(b"fake-image-bytes")

    def fake_post(url, json, headers, timeout):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(ocr.httpx, "post", fake_post)
    request = SupportCaseRequest(
        request_id="case_chat_image",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="img_chat",
                media_type="image",
                filename="chat_screenshot.png",
                local_path=str(image_path),
            )
        ],
    )
    settings = Settings(
        support_ocr_provider="bailian_vl",
        bailian_api_key="test-key",
        support_asset_allowed_local_dirs=str(tmp_path),
    )

    result = asyncio.run(build_support_case_context(request, settings))

    assert any(
        artifact.artifact_type == "ocr" and artifact.status == "error" and "network down" in artifact.error
        for artifact in result.artifacts
    )
    assert "图片文字内容暂未完成 OCR 识别。" in result.context.missing_information


def test_ocr_rejects_non_whitelisted_local_path_before_provider_call(monkeypatch, tmp_path):
    called = False

    def fake_post(url, json, headers, timeout):
        nonlocal called
        called = True
        raise AssertionError("OCR provider should not receive rejected local files")

    monkeypatch.setattr(ocr.httpx, "post", fake_post)
    request = SupportCaseRequest(
        request_id="case_rejected_path",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="img_chat",
                media_type="image",
                filename="chat_screenshot.png",
                local_path="/etc/passwd",
            )
        ],
    )
    settings = Settings(
        support_ocr_provider="bailian_vl",
        bailian_api_key="test-key",
        support_asset_allowed_local_dirs=str(tmp_path),
    )

    result = asyncio.run(build_support_case_context(request, settings))

    artifact = next(artifact for artifact in result.artifacts if artifact.artifact_type == "ocr")
    assert artifact.status == "unsupported"
    assert "允许的缓存目录" in artifact.summary
    assert called is False


def test_ocr_allows_whitelisted_https_url(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "客户：S043 黑屏"}}]}

    def fake_post(url, json, headers, timeout):
        captured["image_url"] = json["messages"][0]["content"][0]["image_url"]["url"]
        return Response()

    monkeypatch.setattr(ocr.httpx, "post", fake_post)
    request = SupportCaseRequest(
        request_id="case_url_image",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="img_chat",
                media_type="image",
                filename="chat_screenshot.png",
                url="https://assets.example.test/screen.png",
            )
        ],
    )
    settings = Settings(
        support_ocr_provider="bailian_vl",
        bailian_api_key="test-key",
        support_asset_allowed_url_hosts="assets.example.test",
    )

    result = asyncio.run(build_support_case_context(request, settings))

    assert "客户：S043 黑屏" in result.context.normalized_query
    assert captured["image_url"] == "https://assets.example.test/screen.png"


def test_visual_embedding_rejects_non_whitelisted_local_path(monkeypatch, tmp_path):
    def fake_generate(settings, content):
        raise AssertionError("visual embedding should not receive rejected local files")

    monkeypatch.setattr(ingestion, "_generate_visual_vector", fake_generate)
    request = SupportCaseRequest(
        request_id="case_rejected_visual",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="img_damage",
                media_type="image",
                filename="product_damage.jpg",
                local_path="/etc/passwd",
            )
        ],
    )
    settings = Settings(
        bailian_api_key="test-key",
        support_asset_allowed_local_dirs=str(tmp_path),
    )

    result = asyncio.run(build_support_case_context(request, settings))

    artifact = next(artifact for artifact in result.artifacts if artifact.artifact_type == "image_embedding")
    assert artifact.status == "unsupported"
    assert "允许的缓存目录" in artifact.summary


def test_product_damage_image_routes_to_visual_embedding_ref_without_raw_vector():
    request = SupportCaseRequest(
        request_id="case_damage_image",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="img_damage",
                media_type="image",
                filename="product_damage.jpg",
                metadata={"vector_id": "vec_damage_001", "visual_summary": "产品外壳断裂，疑似跌落损坏。"},
            )
        ],
    )

    result = asyncio.run(build_support_case_context(request, Settings()))

    decision = result.route.asset_decisions[0]
    assert decision.asset_role == "damage_photo"
    assert decision.requires_visual_embedding is True
    assert result.context.vector_refs == ["vec_damage_001"]
    assert "vec_damage_001" in result.context.model_dump_json()
    assert "[0." not in result.context.model_dump_json()


def test_mixed_text_and_label_image_combines_text_ocr_and_vector_refs():
    request = SupportCaseRequest(
        request_id="case_mixed",
        source="feishu",
        user_text="客户说这个型号无法连接",
        assets=[
            SupportAsset(
                asset_id="img_label",
                media_type="image",
                filename="product_label_photo.jpg",
                metadata={
                    "ocr_text": "铭牌显示 SKU：VL49",
                    "vector_id": "vec_label_001",
                    "visual_summary": "图片展示产品铭牌和设备背面。",
                },
            )
        ],
    )

    result = asyncio.run(build_support_case_context(request, Settings()))

    decision = result.route.asset_decisions[0]
    assert result.route.input_modality == "mixed"
    assert decision.requires_ocr is True
    assert decision.requires_visual_embedding is True
    assert "客户说这个型号无法连接" in result.context.normalized_query
    assert "铭牌显示 SKU" in result.context.normalized_query
    assert result.context.vector_refs == ["vec_label_001"]


def test_video_routes_to_sampling_and_visual_embedding_refs():
    request = SupportCaseRequest(
        request_id="case_video",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="video_fault",
                media_type="video",
                filename="fault_video.mp4",
                metadata={
                    "vector_id": "vec_video_001",
                    "video_summary": "视频显示云台启动后抖动并停止。",
                },
            )
        ],
    )

    result = asyncio.run(build_support_case_context(request, Settings()))

    decision = result.route.asset_decisions[0]
    assert result.route.input_modality == "video"
    assert decision.requires_video_sampling is True
    assert decision.requires_visual_embedding is True
    assert result.context.vector_refs == ["vec_video_001"]
    assert any(artifact.artifact_type == "video_sampling" and artifact.status == "ok" for artifact in result.artifacts)


def test_local_video_sampling_records_frame_refs_without_prompt_leak(monkeypatch, tmp_path):
    video_path = tmp_path / "fault.mp4"
    video_path.write_bytes(b"fake-video")
    frame_path = tmp_path / "frames" / "frame_001.jpg"

    def fake_sample(video_path_arg, asset_id, settings):
        assert video_path_arg == str(video_path)
        assert asset_id == "video_fault"
        return VideoSamplingResult(status="ok", frame_paths=[str(frame_path)])

    monkeypatch.setattr(ingestion, "sample_video_frames", fake_sample)
    request = SupportCaseRequest(
        request_id="case_video",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="video_fault",
                media_type="video",
                filename="fault.mp4",
                local_path=str(video_path),
            )
        ],
    )

    result = asyncio.run(build_support_case_context(request, Settings(support_asset_allowed_local_dirs=str(tmp_path))))

    artifact = next(artifact for artifact in result.artifacts if artifact.artifact_type == "video_sampling")
    assert artifact.status == "ok"
    assert artifact.metadata["frame_paths"] == [str(frame_path)]
    assert "已从视频中采样 1 张关键帧" in result.context.normalized_query
    assert str(frame_path) not in result.context.model_dump_json()


def test_video_sampling_unavailable_records_missing_information(monkeypatch, tmp_path):
    video_path = tmp_path / "fault.mp4"
    video_path.write_bytes(b"fake-video")

    def fake_sample(video_path_arg, asset_id, settings):
        return VideoSamplingResult(status="unsupported", error="ffmpeg not found")

    monkeypatch.setattr(ingestion, "sample_video_frames", fake_sample)
    request = SupportCaseRequest(
        request_id="case_video",
        source="feishu",
        assets=[
            SupportAsset(
                asset_id="video_fault",
                media_type="video",
                filename="fault.mp4",
                local_path=str(video_path),
            )
        ],
    )

    result = asyncio.run(build_support_case_context(request, Settings(support_asset_allowed_local_dirs=str(tmp_path))))

    artifact = next(artifact for artifact in result.artifacts if artifact.artifact_type == "video_sampling")
    assert artifact.status == "unsupported"
    assert "ffmpeg not found" in artifact.summary
    assert "图片/视频视觉语义结果暂未生成。" in result.context.missing_information


def test_unsupported_ingestion_records_missing_information_without_crashing():
    request = SupportCaseRequest(
        request_id="case_unknown_image",
        source="feishu",
        assets=[SupportAsset(asset_id="img_unknown", media_type="image", filename="unknown.jpg")],
    )

    result = asyncio.run(build_support_case_context(request, Settings()))

    assert result.route.input_modality == "image"
    assert any(artifact.status == "unsupported" for artifact in result.artifacts)
    assert "图片文字内容暂未完成 OCR 识别。" in result.context.missing_information
    assert "图片/视频视觉语义结果暂未生成。" in result.context.missing_information


def test_data_source_coverage_lists_hits_missing_sources_and_human_review_window():
    context = asyncio.run(
        build_support_case_context(
            SupportCaseRequest(request_id="case_cov", source="terminal", user_text="未知型号异常"),
            Settings(),
        )
    ).context
    pack = SupportEvidencePack(
        raw_issue_hash="hash",
        query_chars=6,
        issue_type="unknown",
        product_model="",
        sku=[
            SkuEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="hash",
                message="未命中 SKU。",
            )
        ],
        official=[
            OfficialKbEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="hash",
                message="未查询到可信正式依据。",
            )
        ],
        history=[
            HistoryEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="hash",
                message="未查询到可信历史参考。",
            )
        ],
        media=[
            MediaEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="hash",
                message="未查询到可信媒体观察证据。",
            )
        ],
    )

    coverage = build_data_source_coverage(context, pack)

    assert coverage.recommended_action == "human_review"
    assert coverage.mention_enabled is False
    assert "正式知识库" in [item.source_name for item in coverage.items if item.status == "missing"]
    assert "产品 MRD/手册" in [item.source_name for item in coverage.items if item.status == "missing"]
