import asyncio

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
