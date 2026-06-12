from agent_runtime.copilot.answer_contract import (
    SupportAnswer,
    contract_issues_for_output,
    render_support_answer,
    validate_answer_contract,
)
from agent_runtime.copilot.evidence import HistoryEvidence, SupportEvidencePack


VALID_EMPTY_EVIDENCE_ANSWER = """AI 客服参考

问题类型：
troubleshooting

运行模式：
Agent SDK

置信度：
低，正式依据和历史参考都未查询到。

用户问题摘要：
客户反馈设备异常。

SKU 命中：
未在 SKU 目录中命中；需要补充订单 SKU、包装 SKU、产品铭牌或图片。

建议回复（供客服参考，可复制调整）：
建议先收集信息并人工确认。

建议排查步骤：
1. 确认型号。
2. 收集截图。
3. 记录复现步骤。

需要追问：
- 请补充 SKU。

正式依据：
未查询到可信正式依据，不可编造。

历史参考：
未查询到可信历史参考，不可编造。

工单草稿：
不建议生成工单，并说明原因。
"""


def test_valid_empty_evidence_answer_has_no_contract_issues():
    assert validate_answer_contract(VALID_EMPTY_EVIDENCE_ANSWER) == []


def test_missing_field_is_reported():
    issues = validate_answer_contract(VALID_EMPTY_EVIDENCE_ANSWER.replace("工单草稿：", ""))

    assert any(issue.code == "missing_field" and "工单草稿" in issue.message for issue in issues)


def test_field_order_issue_is_reported():
    answer = VALID_EMPTY_EVIDENCE_ANSWER.replace("问题类型：", "置信度：", 1)

    issues = validate_answer_contract(answer)

    assert any(issue.code in {"missing_field", "field_order"} for issue in issues)


def test_forbidden_commitment_is_reported():
    answer = VALID_EMPTY_EVIDENCE_ANSWER.replace("建议先收集信息并人工确认。", "可以退款，直接换新。")

    issues = validate_answer_contract(answer)

    assert any(issue.code == "forbidden_commitment" for issue in issues)


def test_disconnected_rag_requires_empty_evidence_wording():
    answer = VALID_EMPTY_EVIDENCE_ANSWER.replace("未查询到可信正式依据，不可编造。", "文档 A / 第一章 / https://example.test")

    issues = validate_answer_contract(answer)

    assert any(issue.code == "official_evidence" for issue in issues)


def test_connected_history_requires_review_status_marker():
    answer = VALID_EMPTY_EVIDENCE_ANSWER.replace(
        "未查询到可信历史参考，不可编造。",
        "命中历史话题 thread:abc，之前建议补发。",
    )

    issues = validate_answer_contract(answer, history_connected=True)

    assert any(issue.code == "history_evidence" for issue in issues)


def test_connected_history_allows_unreviewed_marker():
    answer = VALID_EMPTY_EVIDENCE_ANSWER.replace(
        "未查询到可信历史参考，不可编造。",
        "命中未审核历史参考，需人工确认，不能作为正式依据：thread:abc。",
    )

    assert validate_answer_contract(answer, history_connected=True) == []


def test_connected_history_allows_unreviewed_media_marker():
    answer = VALID_EMPTY_EVIDENCE_ANSWER.replace(
        "未查询到可信历史参考，不可编造。",
        "命中未审核媒体观察证据，需人工确认，不能作为正式依据：thread:abc / image。",
    )

    assert validate_answer_contract(answer, history_connected=True) == []


def test_negated_commitment_is_not_reported():
    answer = VALID_EMPTY_EVIDENCE_ANSWER.replace(
        "建议先收集信息并人工确认。",
        "未获得正式政策依据前，不要承诺退款、换新或补发。",
    )

    issues = validate_answer_contract(answer)

    assert not any(issue.code == "forbidden_commitment" for issue in issues)


def test_support_answer_renders_existing_customer_service_format():
    answer = SupportAnswer(
        issue_type="troubleshooting",
        run_mode="Agent SDK",
        confidence="低",
        confidence_reason="未查询到可信正式依据。",
        user_issue_summary="客户反馈设备异常。",
        sku_match="未在 SKU 目录中命中；需要补充订单 SKU、包装 SKU、产品铭牌或图片。",
        suggested_reply="建议先收集信息并人工确认。",
        troubleshooting_steps=["确认型号", "收集截图"],
        follow_up_questions=["请补充 SKU"],
        official_evidence="未查询到可信正式依据，不可编造。",
        history_reference="未查询到可信历史参考，不可编造。",
        ticket_draft="不建议生成工单，并说明原因。",
    )

    rendered = render_support_answer(answer)

    assert "AI 客服参考" in rendered
    assert "问题类型：" in rendered
    assert "troubleshooting" in rendered
    assert "建议排查步骤：" in rendered
    assert "1. 确认型号" in rendered
    assert validate_answer_contract(rendered) == []


def test_support_answer_contract_issues_for_forbidden_commitment():
    answer = SupportAnswer(
        issue_type="quality_issue",
        run_mode="Agent SDK",
        confidence="低",
        confidence_reason="没有正式依据。",
        user_issue_summary="客户反馈产品脱落。",
        sku_match="SKU 命中置信度高，处理建议置信度低。",
        suggested_reply="可以退款，直接换新。",
        troubleshooting_steps=["收集图片"],
        follow_up_questions=["请补充订单信息"],
        official_evidence="未查询到可信正式依据，不可编造。",
        history_reference="未查询到可信历史参考，不可编造。",
        ticket_draft="建议生成工单草稿，缺失订单信息。",
    )

    issues = contract_issues_for_output(answer)

    assert any(issue.code == "forbidden_commitment" for issue in issues)


def test_support_answer_contract_uses_evidence_pack_for_history_markers():
    answer = SupportAnswer(
        issue_type="quality_issue",
        run_mode="Agent SDK",
        confidence="低",
        confidence_reason="仅命中未审核历史参考。",
        user_issue_summary="客户反馈产品脱落。",
        sku_match="SKU 命中置信度高，处理建议置信度低。",
        suggested_reply="建议先收集信息并人工确认。",
        troubleshooting_steps=["收集图片"],
        follow_up_questions=["请补充订单信息"],
        official_evidence="未查询到可信正式依据，不可编造。",
        history_reference="命中未审核历史参考，需人工确认，不能作为正式依据：thread:abc。",
        ticket_draft="建议生成工单草稿，缺失订单信息。",
    )
    pack = SupportEvidencePack(
        raw_issue_hash="hash",
        query_chars=8,
        issue_type="quality_issue",
        product_model="",
        sku=[],
        official=[],
        history=[
            HistoryEvidence(
                status="hit",
                evidence_level="unreviewed_history",
                verified=False,
                query_hash="hash",
                topic_id="thread:abc",
            )
        ],
        media=[],
    )

    assert any(issue.code == "history_evidence" for issue in contract_issues_for_output(answer))
    assert contract_issues_for_output(answer, evidence_pack=pack) == []
