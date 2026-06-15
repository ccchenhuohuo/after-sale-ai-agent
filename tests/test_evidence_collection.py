import asyncio

from agent_runtime.copilot import evidence_collection
from agent_runtime.copilot.case_context import RouteDecision, UnifiedCaseContext
from agent_runtime.copilot.evidence import (
    HistoryEvidence,
    MediaEvidence,
    OfficialKbEvidence,
    SkuEvidence,
    SupportEvidencePack,
    render_evidence_pack,
)
from agent_runtime.copilot.prompts import build_agent_input
from agent_runtime.settings import Settings


def test_collect_support_evidence_returns_structured_pack(monkeypatch):
    query_hashes = []

    def fake_sku(query, limit=5, settings=None):
        query_hashes.append(query)
        return [
            SkuEvidence(
                status="hit",
                evidence_level="identity_only",
                verified=True,
                query_hash="sku-hash",
                sku="TB15",
                spu="TB15",
                sku_name_cn="测试 SKU",
                product_name_cn="测试产品",
                product_owner_name="负责人",
                score=100,
                matched_reasons=["sku精确匹配"],
            )
        ]

    def fake_official(query, product_model=None, module=None, issue_type=None, settings=None):
        return [
            OfficialKbEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="official-hash",
                message="未查询到可信正式依据：正式知识库/RAG 索引尚未接入当前终端测试运行。",
            )
        ]

    def fake_history(query, product_model=None, issue_type=None, settings=None):
        assert product_model == "TB15"
        return [
            HistoryEvidence(
                status="hit",
                evidence_level="reviewed_case",
                verified=True,
                query_hash="history-hash",
                topic_id="thread:tb15",
                message="命中已审核群聊历史 FAQ：thread:tb15。",
            )
        ]

    def fake_media(query, product_model=None, settings=None, vector_refs=None):
        assert product_model == "TB15"
        assert vector_refs == []
        return [
            MediaEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="media-hash",
                message="未查询到可信媒体观察证据：没有命中相似媒体记录。",
            )
        ]

    monkeypatch.setattr(evidence_collection, "resolve_sku_evidence", fake_sku)
    monkeypatch.setattr(evidence_collection, "search_official_kb_evidence", fake_official)
    monkeypatch.setattr(evidence_collection, "search_history_evidence", fake_history)
    monkeypatch.setattr(evidence_collection, "search_media_evidence", fake_media)

    pack = asyncio.run(collect_for_test("TB15 胶水失效", Settings()))

    assert query_hashes == ["TB15 胶水失效"]
    assert pack.product_model == "TB15"
    assert pack.issue_type == "quality_issue"
    assert pack.sku_hit_count == 1
    assert pack.history_hit_count == 1
    rendered = render_evidence_pack(pack)
    assert "结构化证据包" in rendered
    assert "已审核群聊历史 FAQ" in rendered


def test_collect_support_evidence_isolates_branch_errors(monkeypatch):
    monkeypatch.setattr(
        evidence_collection,
        "resolve_sku_evidence",
        lambda query, limit=5, settings=None: [
            SkuEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="sku-hash",
                message="未在SKU目录中命中。",
            )
        ],
    )
    monkeypatch.setattr(
        evidence_collection,
        "search_official_kb_evidence",
        lambda query, product_model=None, module=None, issue_type=None, settings=None: [
            OfficialKbEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="official-hash",
                message="未查询到可信正式依据。",
            )
        ],
    )
    monkeypatch.setattr(
        evidence_collection,
        "search_history_evidence",
        lambda query, product_model=None, issue_type=None, settings=None: (_ for _ in ()).throw(RuntimeError("history down")),
    )
    monkeypatch.setattr(
        evidence_collection,
        "search_media_evidence",
        lambda query, product_model=None, settings=None, vector_refs=None: [
            MediaEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="media-hash",
                message="未查询到可信媒体观察证据。",
            )
        ],
    )

    pack = asyncio.run(collect_for_test("未知型号无法连接", Settings()))

    assert pack.product_model == ""
    assert pack.history[0].status == "error"
    assert "历史话题检索异常" in pack.history[0].message
    assert pack.media[0].status == "empty"


async def collect_for_test(raw_issue: str, settings: Settings):
    return await evidence_collection.collect_support_evidence(raw_issue, settings)


def test_collect_support_evidence_passes_context_vector_refs_to_media(monkeypatch):
    seen_vector_refs = []

    monkeypatch.setattr(
        evidence_collection,
        "resolve_sku_evidence",
        lambda query, limit=5, settings=None: [
            SkuEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="sku-hash",
                message="未在SKU目录中命中。",
            )
        ],
    )
    monkeypatch.setattr(
        evidence_collection,
        "search_official_kb_evidence",
        lambda query, product_model=None, module=None, issue_type=None, settings=None: [
            OfficialKbEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="official-hash",
                message="未查询到可信正式依据。",
            )
        ],
    )
    monkeypatch.setattr(
        evidence_collection,
        "search_history_evidence",
        lambda query, product_model=None, issue_type=None, settings=None: [
            HistoryEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="history-hash",
                message="未查询到可信历史参考。",
            )
        ],
    )

    def fake_media(query, product_model=None, settings=None, vector_refs=None):
        seen_vector_refs.extend(vector_refs or [])
        return [
            MediaEvidence(
                status="empty",
                evidence_level="empty",
                verified=False,
                query_hash="media-hash",
                message="未查询到可信媒体观察证据。",
            )
        ]

    monkeypatch.setattr(evidence_collection, "search_media_evidence", fake_media)
    context = UnifiedCaseContext(
        request_id="req-vector",
        source="feishu",
        original_user_text="客户发了产品损坏图片",
        normalized_query="客户发了产品损坏图片",
        vector_refs=["vec_damage_ref"],
        route=RouteDecision(input_modality="image", confidence=0.8),
    )

    asyncio.run(evidence_collection.collect_support_evidence(context, Settings()))

    assert seen_vector_refs == ["vec_damage_ref"]


def test_agent_prompt_redacts_internal_references_from_evidence_pack():
    pack = SupportEvidencePack(
        raw_issue_hash="hash",
        query_chars=20,
        issue_type="quality_issue",
        product_model="L023",
        sku=[
            SkuEvidence(
                status="hit",
                evidence_level="identity_only",
                verified=True,
                query_hash="sku-hash",
                sku="L023",
                spu="L023",
                sku_name_cn="测试 SKU",
                product_name_cn="测试产品",
                product_owner_name="负责人",
                score=100,
                matched_reasons=["sku精确匹配"],
            )
        ],
        official=[
            OfficialKbEvidence(
                status="hit",
                evidence_level="formal",
                verified=True,
                query_hash="official-hash",
                title="正式售后政策",
                section="/opt/agent-runtime/private/section",
                reference_url="https://internal.example/kb/l023",
            )
        ],
        history=[
            HistoryEvidence(
                status="hit",
                evidence_level="reviewed_case",
                verified=True,
                query_hash="history-hash",
                topic_id="thread-1",
                message=(
                    "- 话题ID：thread-1\n"
                    "  问题摘要：客户反馈不亮\n"
                    "  话题链接：https://feishu.example/history/thread-1\n"
                    "  附件路径：/opt/agent-runtime/data/feishu_runtime/assets/a.jpg\n"
                    "  file_key=img_secret_123456"
                ),
            )
        ],
        media=[
            MediaEvidence(
                status="hit",
                evidence_level="unreviewed_media",
                verified=False,
                query_hash="media-hash",
                media_id="media_secret_123456",
                message=(
                    "- 话题ID：thread-2\n"
                    "  媒体观察摘要：图片疑似划伤\n"
                    "  消息链接：https://feishu.example/history/message-2\n"
                    "  向量：vector_id=vec_secret_abcdef\n"
                    "  embedding=[0.123456, 0.654321]"
                ),
            )
        ],
    )

    prompt = build_agent_input(
        "客户发来产品损坏图",
        source="飞书客服群",
        evidence_pack=pack,
    )

    assert "https://internal.example" not in prompt
    assert "https://feishu.example" not in prompt
    assert "/opt/agent-runtime" not in prompt
    assert "img_secret_123456" not in prompt
    assert "media_secret_123456" not in prompt
    assert "vec_secret_abcdef" not in prompt
    assert "0.123456" not in prompt
    assert "[redacted-url]" in prompt
    assert "[redacted-path]" in prompt
    assert "[redacted-file-key]" in prompt
    assert "[redacted-vector-ref]" in prompt
    assert "[redacted-vector]" in prompt


def test_agent_prompt_redacts_case_context_internal_refs():
    context = UnifiedCaseContext(
        request_id="case_context_refs",
        source="feishu",
        original_user_text="客户发来图片",
        normalized_query="客户图片已下载到 /opt/agent-runtime/private/a.jpg",
        asset_refs=["openclaw_asset:imageKey:img_secret_abcdef"],
        vector_refs=["vec_secret_abcdef"],
        missing_information=["缺少原始文件 https://internal.example/file"],
        route=RouteDecision(input_modality="image", confidence=0.8),
    )

    prompt = build_agent_input(
        "客户发来图片",
        source="飞书客服群",
        case_context=context,
    )

    assert "openclaw_asset:imageKey:img_secret_abcdef" not in prompt
    assert "vec_secret_abcdef" not in prompt
    assert "/opt/agent-runtime" not in prompt
    assert "https://internal.example" not in prompt
    assert "引用哈希" in prompt
    assert "[redacted-path]" in prompt
    assert "[redacted-url]" in prompt


def test_agent_prompt_redacts_raw_issue_internal_refs():
    prompt = build_agent_input(
        (
            "客户原文包含 https://internal.example/file "
            "/opt/agent-runtime/private/a.jpg "
            "file_key=img_secret_123456 "
            "vector_id=vec_secret_abcdef "
            "embedding=[0.123456, 0.654321]"
        ),
        source="飞书客服群",
    )

    assert "https://internal.example" not in prompt
    assert "/opt/agent-runtime" not in prompt
    assert "img_secret_123456" not in prompt
    assert "vec_secret_abcdef" not in prompt
    assert "0.123456" not in prompt
    assert "[redacted-url]" in prompt
    assert "[redacted-path]" in prompt
    assert "[redacted-file-key]" in prompt
    assert "[redacted-vector-ref]" in prompt
    assert "[redacted-vector]" in prompt


def test_agent_prompt_redacts_combined_reference_boundaries():
    pack = SupportEvidencePack(
        raw_issue_hash="hash",
        query_chars=20,
        issue_type="quality_issue",
        product_model="L023",
        sku=[
            SkuEvidence(
                status="hit",
                evidence_level="identity_only",
                verified=True,
                query_hash="sku-hash",
                sku="L023",
                score=100,
                matched_reasons=[
                    "路径；/opt/agent-runtime/private/sku.jpg",
                    "embedding=[0.123456, 0.654321]",
                    "file_key=img_secret_123456",
                ],
            )
        ],
        official=[
            OfficialKbEvidence(
                status="hit",
                evidence_level="formal",
                verified=True,
                query_hash="official-hash",
                title="正式依据",
                section="章节；/opt/agent-runtime/private/section",
                reference_url="https://internal.example/kb",
            )
        ],
        history=[
            HistoryEvidence(
                status="hit",
                evidence_level="reviewed_case",
                verified=True,
                query_hash="history-hash",
                message="话题链接：https://feishu.example/t、路径；/opt/agent-runtime/history/a.jpg、vector_id=vec_history_secret",
            )
        ],
        media=[
            MediaEvidence(
                status="hit",
                evidence_level="unreviewed_media",
                verified=False,
                query_hash="media-hash",
                message="消息链接：https://feishu.example/m、路径；/opt/agent-runtime/media/a.jpg、embedding=[0.777777, 0.888888]",
            )
        ],
    )
    context = UnifiedCaseContext(
        request_id="case_combo",
        source="feishu",
        original_user_text="客户发来图片",
        normalized_query="客户图片；/opt/agent-runtime/context/a.jpg",
        asset_refs=["openclaw_asset:imageKey:img_context_secret"],
        vector_refs=["vec_context_secret"],
        missing_information=["补充链接；https://internal.example/context"],
        route=RouteDecision(input_modality="mixed", confidence=0.8),
    )

    prompt = build_agent_input(
        "客户原文；/opt/agent-runtime/raw/a.jpg、https://raw.example/a、vector_id=vec_raw_secret、embedding=[0.111111, 0.222222]",
        source="飞书客服群",
        evidence_pack=pack,
        case_context=context,
    )

    for forbidden in (
        "/opt/agent-runtime",
        "https://internal.example",
        "https://feishu.example",
        "https://raw.example",
        "img_secret_123456",
        "img_context_secret",
        "vec_history_secret",
        "vec_context_secret",
        "vec_raw_secret",
        "0.123456",
        "0.654321",
        "0.777777",
        "0.888888",
        "0.111111",
        "0.222222",
    ):
        assert forbidden not in prompt
    assert "[redacted-path]" in prompt
    assert "[redacted-url]" in prompt
    assert "[redacted-file-key]" in prompt
    assert "[redacted-vector-ref]" in prompt
    assert "[redacted-vector]" in prompt
