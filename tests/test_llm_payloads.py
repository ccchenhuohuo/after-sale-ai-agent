import asyncio
import json
from types import SimpleNamespace

import agent_runtime.copilot.context_assembly as context_assembly_module
import agent_runtime.copilot.intake as intake_module
from agent_runtime.copilot.case_context import (
    AssetRouteDecision,
    IngestionArtifact,
    RouteDecision,
    SupportAsset,
    SupportCaseRequest,
)
from agent_runtime.copilot.context_assembly import deterministic_assemble_unified_case_context
from agent_runtime.copilot.llm_payloads import (
    safe_artifact_payload_for_llm,
    safe_asset_payload_for_llm,
    safe_request_payload_for_llm,
)
from agent_runtime.settings import Settings


FORBIDDEN_LLM_PAYLOAD_KEYS = {
    "embedding",
    "ocr_text",
    "text_hint",
    "visual_summary",
    "video_summary",
    "local_path",
    "url",
    "file_key",
    "chat_id",
    "thread_id",
    "message_id",
    "sender_id",
    "custom_secret",
}


def test_safe_asset_payload_whitelists_metadata_and_transport_fields():
    asset = SupportAsset(
        asset_id="img_1",
        media_type="image",
        filename="damage.jpg",
        mime_type="image/jpeg",
        file_key="file_secret_key",
        message_id="om_secret",
        url="https://internal.example/file",
        local_path="/tmp/private/damage.jpg",
        metadata={
            "asset_role": "damage_photo",
            "description": "客户上传的损坏图片",
            "source_type": "feishu",
            "low_quality": False,
            "rich_tag": "after_sales",
            "feishu_message_type": "image",
            "embedding": [0.123456, 0.654321],
            "ocr_text": "SHOULD_NOT_PASS_AS_ASSET_METADATA",
            "text_hint": "private text hint",
            "visual_summary": "private visual summary",
            "video_summary": "private video summary",
            "local_path": "/metadata/private/path",
            "url": "https://metadata.internal/file",
            "file_key": "metadata_file_secret",
            "chat_id": "oc_secret",
            "thread_id": "omt_secret",
            "message_id": "om_metadata_secret",
            "sender_id": "ou_secret",
            "custom_secret": "hidden",
        },
    )

    payload = safe_asset_payload_for_llm(asset)

    assert set(payload) == {
        "asset_id",
        "media_type",
        "filename",
        "mime_type",
        "safe_metadata",
    }
    assert payload["safe_metadata"] == {
        "asset_role": "damage_photo",
        "description": "客户上传的损坏图片",
        "source_type": "feishu",
        "low_quality": False,
        "rich_tag": "after_sales",
        "feishu_message_type": "image",
    }
    _assert_forbidden_keys_absent(payload)
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "file_secret_key" not in dumped
    assert "https://internal.example/file" not in dumped
    assert "/tmp/private/damage.jpg" not in dumped
    assert "SHOULD_NOT_PASS_AS_ASSET_METADATA" not in dumped
    assert "0.123456" not in dumped


def test_safe_request_payload_excludes_chat_thread_message_and_sender_ids():
    request = SupportCaseRequest(
        request_id="case_1",
        source="feishu",
        user_text="客户反馈 L023 不亮",
        chat_id="oc_secret",
        thread_id="omt_secret",
        message_id="om_secret",
        sender_id="ou_secret",
        metadata={"file_key": "request_file_secret", "custom_secret": "hidden"},
        assets=[
            SupportAsset(
                asset_id="img_1",
                media_type="image",
                filename="chat_screenshot.png",
                file_key="asset_file_secret",
                metadata={"asset_role": "chat_screenshot", "ocr_text": "asset metadata OCR"},
            )
        ],
    )

    payload = safe_request_payload_for_llm(request)

    assert set(payload) == {"request_id", "source", "user_text", "assets"}
    _assert_forbidden_keys_absent(payload)
    dumped = json.dumps(payload, ensure_ascii=False)
    for forbidden_value in (
        "oc_secret",
        "omt_secret",
        "om_secret",
        "ou_secret",
        "request_file_secret",
        "asset_file_secret",
        "asset metadata OCR",
    ):
        assert forbidden_value not in dumped


def test_safe_artifact_payload_keeps_vector_ref_and_drops_metadata():
    artifact = IngestionArtifact(
        artifact_id="visual:img_1:vec_1",
        artifact_type="image_embedding",
        status="ok",
        asset_id="img_1",
        summary="图片显示外壳断裂。",
        vector_id="vec_public_ref",
        model_name="vl-embedding",
        index_namespace="after_sales_v1",
        metadata={"embedding": [0.123456], "file_key": "metadata_file_secret"},
    )

    payload = safe_artifact_payload_for_llm(artifact)

    assert payload["vector_id"] == "vec_public_ref"
    assert "metadata" not in payload
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "vec_public_ref" in dumped
    assert "metadata_file_secret" not in dumped
    assert "0.123456" not in dumped


def test_router_agent_payload_is_sanitized_when_enabled(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_run(agent, input_text, *, run_config=None):
        captured["payload"] = json.loads(input_text)
        return SimpleNamespace(
            final_output=RouteDecision(
                input_modality="image",
                confidence=0.9,
                reason="test route",
                asset_decisions=[
                    AssetRouteDecision(
                        asset_id="img_1",
                        media_type="image",
                        asset_role="damage_photo",
                        requires_visual_embedding=True,
                        confidence=0.9,
                        reason="test asset route",
                    )
                ],
            )
        )

    monkeypatch.setattr(intake_module.Runner, "run", fake_run)
    request = SupportCaseRequest(
        request_id="case_router",
        source="feishu",
        user_text="看下这张图",
        chat_id="oc_secret",
        thread_id="omt_secret",
        message_id="om_secret",
        sender_id="ou_secret",
        assets=[
            SupportAsset(
                asset_id="img_1",
                media_type="image",
                filename="product_damage.jpg",
                file_key="asset_file_secret",
                local_path="/tmp/private/product_damage.jpg",
                metadata={
                    "asset_role": "damage_photo",
                    "description": "客户上传的损坏图",
                    "source_type": "feishu",
                    "embedding": [0.123456],
                    "ocr_text": "asset metadata OCR",
                    "visual_summary": "asset metadata visual summary",
                    "file_key": "metadata_file_secret",
                },
            )
        ],
    )

    route = asyncio.run(
        intake_module.route_support_case(request, Settings(support_intake_router_enabled=True))
    )

    assert route.asset_decisions[0].requires_visual_embedding is True
    payload = captured["payload"]
    assert isinstance(payload, dict)
    asset_payload = payload["assets"][0]
    assert asset_payload["safe_metadata"] == {
        "asset_role": "damage_photo",
        "description": "客户上传的损坏图",
        "source_type": "feishu",
    }
    _assert_forbidden_keys_absent(payload)
    dumped = json.dumps(payload, ensure_ascii=False)
    for forbidden_value in (
        "asset_file_secret",
        "/tmp/private/product_damage.jpg",
        "asset metadata OCR",
        "asset metadata visual summary",
        "metadata_file_secret",
        "0.123456",
    ):
        assert forbidden_value not in dumped


def test_context_assembler_agent_payload_is_sanitized_when_enabled(monkeypatch):
    captured: dict[str, object] = {}
    request = SupportCaseRequest(
        request_id="case_assembler",
        source="feishu",
        user_text="客户反馈产品断裂",
        chat_id="oc_secret",
        thread_id="omt_secret",
        message_id="om_secret",
        sender_id="ou_secret",
        metadata={"file_key": "request_file_secret"},
        assets=[
            SupportAsset(
                asset_id="img_1",
                media_type="image",
                filename="product_damage.jpg",
                file_key="asset_file_secret",
                metadata={
                    "asset_role": "damage_photo",
                    "embedding": [0.98765],
                    "ocr_text": "asset metadata OCR",
                    "visual_summary": "asset metadata visual summary",
                },
            )
        ],
    )
    route = RouteDecision(
        input_modality="mixed",
        confidence=0.85,
        reason="test route",
        user_text=request.user_text,
        asset_decisions=[
            AssetRouteDecision(
                asset_id="img_1",
                media_type="image",
                asset_role="damage_photo",
                requires_ocr=True,
                requires_visual_embedding=True,
                confidence=0.85,
                reason="test asset route",
            )
        ],
    )
    artifacts = [
        IngestionArtifact(
            artifact_id="ocr:img_1:text",
            artifact_type="ocr",
            status="ok",
            asset_id="img_1",
            text="OCR 识别到型号 VL49",
            model_name="ocr-test",
            metadata={"ocr_raw_payload": "private raw OCR payload"},
        ),
        IngestionArtifact(
            artifact_id="visual:img_1:vec_public_ref",
            artifact_type="image_embedding",
            status="ok",
            asset_id="img_1",
            summary="图片显示外壳断裂。",
            vector_id="vec_public_ref",
            model_name="vl-embedding",
            index_namespace="after_sales_v1",
            metadata={"embedding": [0.123456], "file_key": "metadata_file_secret"},
        ),
    ]
    expected_context = deterministic_assemble_unified_case_context(request, route, artifacts)

    async def fake_run(agent, input_text, *, run_config=None):
        captured["payload"] = json.loads(input_text)
        return SimpleNamespace(final_output=expected_context)

    monkeypatch.setattr(context_assembly_module.Runner, "run", fake_run)

    context = asyncio.run(
        context_assembly_module.assemble_unified_case_context(
            request,
            route,
            artifacts,
            Settings(support_context_assembler_enabled=True),
        )
    )

    assert context.vector_refs == ["vec_public_ref"]
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["request"]["assets"][0]["safe_metadata"] == {
        "asset_role": "damage_photo",
    }
    assert "vec_public_ref" in json.dumps(payload, ensure_ascii=False)
    for artifact_payload in payload["artifacts"]:
        assert "metadata" not in artifact_payload
    _assert_forbidden_keys_absent(payload["request"])
    dumped = json.dumps(payload, ensure_ascii=False)
    for forbidden_value in (
        "oc_secret",
        "omt_secret",
        "om_secret",
        "ou_secret",
        "request_file_secret",
        "asset_file_secret",
        "asset metadata OCR",
        "asset metadata visual summary",
        "private raw OCR payload",
        "metadata_file_secret",
        "0.98765",
        "0.123456",
    ):
        assert forbidden_value not in dumped


def _assert_forbidden_keys_absent(value: object) -> None:
    if isinstance(value, dict):
        assert not FORBIDDEN_LLM_PAYLOAD_KEYS.intersection(value.keys())
        for child in value.values():
            _assert_forbidden_keys_absent(child)
    elif isinstance(value, list):
        for child in value:
            _assert_forbidden_keys_absent(child)
