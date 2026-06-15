import asyncio

from agent_runtime.copilot import evidence_collection
from agent_runtime.copilot.case_context import RouteDecision, UnifiedCaseContext
from agent_runtime.copilot.evidence import (
    HistoryEvidence,
    MediaEvidence,
    OfficialKbEvidence,
    SkuEvidence,
    render_evidence_pack,
)
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
