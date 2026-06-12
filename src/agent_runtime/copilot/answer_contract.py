from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agents import GuardrailFunctionOutput, output_guardrail
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.copilot.evidence import SupportEvidencePack


ANSWER_FIELDS = [
    "问题类型",
    "运行模式",
    "置信度",
    "用户问题摘要",
    "SKU 命中",
    "建议回复（供客服参考，可复制调整）",
    "建议排查步骤",
    "需要追问",
    "正式依据",
    "历史参考",
    "工单草稿",
]

FORBIDDEN_COMMITMENT_PATTERNS = [
    "可以退款",
    "直接退款",
    "承诺退款",
    "可以赔偿",
    "直接赔偿",
    "承诺赔偿",
    "可以换新",
    "直接换新",
    "承诺换新",
    "可以补发",
    "直接补发",
    "承诺补发",
    "维修时效",
]

NEGATED_COMMITMENT_PREFIXES = ["不要", "不能", "不可", "不得", "未获得正式政策依据前，不要"]


@dataclass(frozen=True)
class ContractIssue:
    code: str
    message: str


class SupportAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_type: Literal["product_usage", "troubleshooting", "quality_issue", "ticket_followup", "unknown"] = Field(
        description="售后问题类型。"
    )
    run_mode: Literal["Agent SDK"] = Field(description="固定为 Agent SDK。")
    confidence: Literal["高", "中", "低"] = Field(description="整体答案置信度。")
    confidence_reason: str = Field(description="一句话说明置信度原因。")
    user_issue_summary: str = Field(description="客户问题摘要。")
    sku_match: str = Field(description="SKU 命中说明。")
    suggested_reply: str = Field(description="供客服参考、可复制调整的回复。")
    troubleshooting_steps: list[str] = Field(description="建议排查步骤。")
    follow_up_questions: list[str] = Field(description="需要继续追问的信息。")
    official_evidence: str = Field(description="正式依据说明；无命中时必须明确未查询到可信正式依据。")
    history_reference: str = Field(description="历史参考说明；未审核证据必须标注需人工确认。")
    ticket_draft: str = Field(description="工单草稿或不建议生成工单的理由。")


def _field_position(text: str, field: str) -> int:
    candidates = [text.find(f"{field}："), text.find(f"{field}:")]
    positions = [position for position in candidates if position >= 0]
    return min(positions) if positions else -1


def _section_text(text: str, field: str) -> str:
    start = _field_position(text, field)
    if start < 0:
        return ""
    next_starts = [
        _field_position(text, next_field)
        for next_field in ANSWER_FIELDS[ANSWER_FIELDS.index(field) + 1 :]
    ]
    next_positions = [position for position in next_starts if position > start]
    end = min(next_positions) if next_positions else len(text)
    return text[start:end]


def _contains_forbidden_commitment(text: str, pattern: str) -> bool:
    start = 0
    while True:
        index = text.find(pattern, start)
        if index < 0:
            return False
        prefix = text[max(0, index - 16) : index]
        if not any(negation in prefix for negation in NEGATED_COMMITMENT_PREFIXES):
            return True
        start = index + len(pattern)


def validate_answer_contract(
    text: str,
    *,
    official_kb_connected: bool = False,
    history_connected: bool = False,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    positions: list[tuple[str, int]] = []

    for field in ANSWER_FIELDS:
        position = _field_position(text, field)
        if position < 0:
            issues.append(ContractIssue("missing_field", f"缺少字段：{field}"))
        else:
            positions.append((field, position))

    previous_position = -1
    for field, position in positions:
        if position < previous_position:
            issues.append(ContractIssue("field_order", f"字段顺序错误：{field}"))
            break
        previous_position = position

    for pattern in FORBIDDEN_COMMITMENT_PATTERNS:
        if _contains_forbidden_commitment(text, pattern):
            issues.append(ContractIssue("forbidden_commitment", f"可能包含售后承诺：{pattern}"))

    official_section = _section_text(text, "正式依据")
    if not official_kb_connected and official_section and "未查询到可信正式依据" not in official_section:
        issues.append(ContractIssue("official_evidence", "正式知识库未接入时，正式依据必须说明未查询到可信正式依据。"))

    history_section = _section_text(text, "历史参考")
    if not history_connected and history_section and "未查询到可信历史参考" not in history_section:
        issues.append(ContractIssue("history_evidence", "历史案例库未接入时，历史参考必须说明未查询到可信历史参考。"))
    if history_connected and history_section:
        has_empty_history = "未查询到可信历史参考" in history_section
        has_unreviewed_marker = "未审核历史参考" in history_section and "需人工确认" in history_section
        has_unreviewed_media_marker = "未审核媒体观察证据" in history_section and "需人工确认" in history_section
        if not (has_empty_history or has_unreviewed_marker or has_unreviewed_media_marker):
            issues.append(
                ContractIssue(
                    "history_evidence",
                    "历史参考必须标注未查询到可信历史参考，或标注未审核历史/媒体观察证据且需人工确认。",
                )
            )

    return issues


def render_support_answer(answer: SupportAnswer | object) -> str:
    if not isinstance(answer, SupportAnswer):
        if isinstance(answer, dict):
            answer = SupportAnswer.model_validate(answer)
        else:
            return str(answer).strip()

    return "\n".join(
        [
            "AI 客服参考",
            "",
            "问题类型：",
            answer.issue_type,
            "",
            "运行模式：",
            answer.run_mode,
            "",
            "置信度：",
            f"{answer.confidence}，{answer.confidence_reason}",
            "",
            "用户问题摘要：",
            answer.user_issue_summary,
            "",
            "SKU 命中：",
            answer.sku_match,
            "",
            "建议回复（供客服参考，可复制调整）：",
            answer.suggested_reply,
            "",
            "建议排查步骤：",
            _numbered_lines(answer.troubleshooting_steps),
            "",
            "需要追问：",
            _bullet_lines(answer.follow_up_questions),
            "",
            "正式依据：",
            answer.official_evidence,
            "",
            "历史参考：",
            answer.history_reference,
            "",
            "工单草稿：",
            answer.ticket_draft,
        ]
    )


def contract_issues_for_output(
    output: SupportAnswer | object,
    evidence_pack: SupportEvidencePack | None = None,
) -> list[ContractIssue]:
    try:
        rendered = render_support_answer(output)
    except Exception as exc:
        return [ContractIssue("invalid_output", f"结构化输出无法渲染：{type(exc).__name__}")]
    return validate_answer_contract(
        rendered,
        official_kb_connected=evidence_pack.has_formal_evidence if evidence_pack is not None else False,
        history_connected=_has_history_or_media_evidence(evidence_pack),
    )


@output_guardrail(name="support_answer_contract")
def support_answer_output_guardrail(ctx, agent, output) -> GuardrailFunctionOutput:
    evidence_pack = getattr(ctx, "context", None)
    issues = contract_issues_for_output(
        output,
        evidence_pack=evidence_pack if isinstance(evidence_pack, SupportEvidencePack) else None,
    )
    return GuardrailFunctionOutput(
        output_info=[{"code": issue.code, "message": issue.message} for issue in issues],
        tripwire_triggered=bool(issues),
    )


def _numbered_lines(items: list[str]) -> str:
    clean_items = [item.strip() for item in items if item.strip()]
    if not clean_items:
        return "1. 无。"
    return "\n".join(f"{index}. {item}" for index, item in enumerate(clean_items, start=1))


def _bullet_lines(items: list[str]) -> str:
    clean_items = [item.strip() for item in items if item.strip()]
    if not clean_items:
        return "- 无。"
    return "\n".join(f"- {item}" for item in clean_items)


def _has_history_or_media_evidence(evidence_pack: SupportEvidencePack | None) -> bool:
    if evidence_pack is None:
        return False
    return evidence_pack.history_hit_count > 0 or evidence_pack.media_hit_count > 0
