from agent_runtime.copilot.answer_contract import validate_answer_contract


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
