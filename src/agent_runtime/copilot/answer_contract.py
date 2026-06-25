from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from agents import GuardrailFunctionOutput, output_guardrail
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_runtime.copilot.case_context import DataSourceCoverage
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

FEISHU_VISIBLE_REPLY_FALLBACK = "这轮没有生成到足够稳妥的建议，请人工接手确认后再回复客户。"

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

FORBIDDEN_COMMITMENT_REGEX_PATTERNS = [
    re.compile(r"(?:建议|安排|同意|可走|给客户|为客户|直接)[^。；，,\n]{0,8}(?:退款|赔偿|赔付|换新|补发)"),
    re.compile(r"(?:退款|赔偿|赔付|换新|补发)[^。；，,\n]{0,8}(?:处理|方案|给客户|为客户)"),
    re.compile(r"(?:承诺|确认|保证)[^。；，,\n]{0,8}(?:维修时效|维修时间|处理时效)"),
    re.compile(r"(?:承诺|确认|保证|确保)[^。；，,\n]{0,24}(?:\d+\s*(?:小时|天|个工作日)|当天|今日|明天|本周)[^。；，,\n]{0,16}(?:跟进|回复|处理|解决|完成|寄出|发出|补发|维修)"),
    re.compile(r"(?:\d+\s*(?:小时|天|个工作日)|当天|今日|明天|本周)[^。；，,\n]{0,16}(?:跟进|回复|处理|解决|完成|寄出|发出|补发|维修)"),
    re.compile(r"(?:尽快|尽早|马上|立即|立刻|第一时间)[^。；，,\n]{0,16}(?:回复|答复|处理|跟进|解决|安排|通知)"),
    re.compile(r"(?:已|已经)?[^。；，,\n]{0,4}(?:转交|提交|升级|安排|反馈)[^。；，,\n]{0,20}(?:跟进|处理|回复|答复|解决|核实)"),
]

NEGATED_COMMITMENT_PREFIXES = [
    "不要",
    "不能",
    "不可",
    "不得",
    "不建议",
    "无法",
    "不能直接",
    "不可直接",
    "暂不",
    "未获得正式政策依据前，不要",
]

FEISHU_VISIBLE_INTERNAL_PATTERNS = [
    re.compile(rf"{re.escape(field)}[：:]", re.IGNORECASE) for field in ANSWER_FIELDS
] + [
    re.compile(r"\bSupportAnswer\b", re.IGNORECASE),
    re.compile(r"\bAgent SDK\b", re.IGNORECASE),
    re.compile(r"\bSDK\b", re.IGNORECASE),
    re.compile(r"\bloop\b", re.IGNORECASE),
    re.compile(r"\boutput guardrail\b", re.IGNORECASE),
    re.compile(r"\bguardrail\b", re.IGNORECASE),
    re.compile(r"\bcontract\b", re.IGNORECASE),
    re.compile(r"\btrace\b", re.IGNORECASE),
    re.compile(r"\btool\b", re.IGNORECASE),
    re.compile(r"\bRunner\.run\b", re.IGNORECASE),
    re.compile(r"\bPydantic\b", re.IGNORECASE),
    re.compile(r"OpenAI Agents", re.IGNORECASE),
    re.compile(r"证据包"),
    re.compile(r"工具调用"),
    re.compile(r"未查询到可信正式依据"),
    re.compile(r"未查询到可信历史参考"),
    re.compile(r"未审核历史参考"),
    re.compile(r"未审核媒体观察证据"),
]

FEISHU_VISIBLE_REFERENCE_LEAK_PATTERNS = [
    re.compile(r"\bhttps?://[^\s，。；；)）]+", re.IGNORECASE),
    re.compile(r"\bfile://[^\s，。；；)）]+", re.IGNORECASE),
    re.compile(r"(?:^|[\s：:])/(?:tmp|var|opt|home|Users|private|mnt|data)/[^\s，。；；)）]+"),
    re.compile(r"\b[A-Za-z]:\\[^\s，。；；)）]+"),
    re.compile(r"\b(?:file[_-]?key|fileKey|imageKey|mediaKey|file_token)\b", re.IGNORECASE),
    re.compile(r"\b(?:img|file|media)_[A-Za-z0-9][A-Za-z0-9_-]{6,}\b", re.IGNORECASE),
    re.compile(r"\b(?:vector[_-]?id|vector ref|vector_ref)\b", re.IGNORECASE),
    re.compile(r"\b(?:vec|vector)[:_][A-Za-z0-9][A-Za-z0-9_:-]{6,}\b", re.IGNORECASE),
    re.compile(r"\[[+-]?(?:0|1)?\.\d{2,}\s*,\s*[+-]?(?:0|1)?\.\d{2,}"),
]

FEISHU_VISIBLE_MARKDOWN_PATTERNS = [
    re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE),
    re.compile(r"^\s{0,3}(?:[-*+]\s+|\d+[.)]\s+)", re.MULTILINE),
    re.compile(r"(?:^|[：:；;。]\s*)\d+[.)]\s+", re.MULTILINE),
    re.compile(r"^\s{0,3}\|.*\|\s*$", re.MULTILINE),
    re.compile(r"```"),
    re.compile(r"`[^`]+`"),
    re.compile(r"\*\*[^*]+\*\*"),
]

REVIEWED_HISTORY_MARKER_RE = re.compile(r"(?:已审核群聊历史\s*FAQ|reviewed_case)", re.IGNORECASE)


@dataclass(frozen=True)
class ContractIssue:
    code: str
    message: str


def _stringify_structured_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "；".join(_stringify_structured_text(item) for item in value if item is not None)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text = _stringify_structured_text(item)
            if text:
                parts.append(f"{key}：{text}")
        return "；".join(parts)
    return str(value)


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
    history_reference: str = Field(description="历史参考说明；已审核群聊历史 FAQ 可作为可靠售后参考，未审核媒体证据必须标注需人工确认。")
    data_sources_used: list[str] = Field(default_factory=list, description="本轮已参考或命中的数据源名称。")
    missing_data_sources: list[str] = Field(default_factory=list, description="本轮缺失、未接入或未命中的关键数据源。")
    recommended_action: Literal["answer", "ask_clarification", "human_review"] = Field(
        default="ask_clarification",
        description="建议动作；资料不足时使用 human_review 进入人工复核。",
    )
    owner_candidate: str = Field(default="", description="建议负责人候选；没有时留空。")
    mention_enabled: bool = Field(default=False, description="是否需要在飞书回复中 @ 人工复核负责人。")
    ticket_draft: str = Field(description="工单草稿或不建议生成工单的理由。")

    @field_validator(
        "confidence_reason",
        "user_issue_summary",
        "sku_match",
        "suggested_reply",
        "official_evidence",
        "history_reference",
        "owner_candidate",
        "ticket_draft",
        mode="before",
    )
    @classmethod
    def _coerce_text_fields(cls, value: Any) -> str:
        return _stringify_structured_text(value)


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
        if not _has_negated_commitment_context(text, index):
            return True
        start = index + len(pattern)


def _contains_forbidden_commitment_regex(text: str, pattern: re.Pattern[str]) -> bool:
    start = 0
    while True:
        match = pattern.search(text, start)
        if match is None:
            return False
        if not _has_negated_commitment_context(text, match.start()):
            return True
        start = match.end()


def _has_negated_commitment_context(text: str, start: int) -> bool:
    context = text[max(0, start - 16) : start + 8]
    return any(negation in context for negation in NEGATED_COMMITMENT_PREFIXES)


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

    issues.extend(_forbidden_commitment_issues(_customer_visible_contract_text(text)))

    official_section = _section_text(text, "正式依据")
    if not official_kb_connected and official_section and "未查询到可信正式依据" not in official_section:
        issues.append(ContractIssue("official_evidence", "正式知识库未接入时，正式依据必须说明未查询到可信正式依据。"))

    history_section = _section_text(text, "历史参考")
    if not history_connected and history_section and "未查询到可信历史参考" not in history_section:
        issues.append(ContractIssue("history_evidence", "历史案例库未接入时，历史参考必须说明未查询到可信历史参考。"))
    if history_connected and history_section:
        has_empty_history = "未查询到可信历史参考" in history_section
        has_reviewed_history_marker = bool(REVIEWED_HISTORY_MARKER_RE.search(history_section))
        has_unreviewed_marker = "未审核历史参考" in history_section and "需人工确认" in history_section
        has_unreviewed_media_marker = "未审核媒体观察证据" in history_section and "需人工确认" in history_section
        if not (has_empty_history or has_reviewed_history_marker or has_unreviewed_marker or has_unreviewed_media_marker):
            issues.append(
                ContractIssue(
                    "history_evidence",
                    "历史参考必须标注未查询到可信历史参考、已审核群聊历史 FAQ，或标注未审核媒体观察证据且需人工确认。",
                )
            )

    return issues


def render_feishu_reply(answer: SupportAnswer | dict) -> str:
    answer = _coerce_support_answer(answer)
    paragraphs = [
        _intro_paragraph(answer),
        f"客服可以先这样回应客户：{_visible_text(answer.suggested_reply)}",
    ]
    source_paragraph = _source_paragraph(answer)
    if source_paragraph:
        paragraphs.append(source_paragraph)
    action_paragraph = _action_paragraph(answer)
    if action_paragraph:
        paragraphs.append(action_paragraph)
    paragraphs.append(
        "涉及退款、赔付、换新、补发这类售后动作，或者涉及处理时间的内容，先不要直接承诺；需要等正式政策或人工复核后再给客户明确口径。"
    )
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def validate_feishu_visible_reply(text: str) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for pattern in FEISHU_VISIBLE_INTERNAL_PATTERNS:
        if pattern.search(text):
            issues.append(ContractIssue("visible_internal_leak", f"飞书可见回复包含内部信息：{pattern.pattern}"))
            break
    for pattern in FEISHU_VISIBLE_MARKDOWN_PATTERNS:
        if pattern.search(text):
            issues.append(ContractIssue("visible_markdown", f"飞书可见回复包含 Markdown 痕迹：{pattern.pattern}"))
            break
    for pattern in FEISHU_VISIBLE_REFERENCE_LEAK_PATTERNS:
        if pattern.search(text):
            issues.append(ContractIssue("visible_reference_leak", "飞书可见回复包含内部附件、路径、URL 或向量引用。"))
            break
    issues.extend(_forbidden_commitment_issues(text))
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
            "已参考数据源：",
            _bullet_lines(answer.data_sources_used),
            "",
            "缺失数据源：",
            _bullet_lines(answer.missing_data_sources),
            "",
            "建议动作：",
            _recommended_action_line(answer),
            "",
            "工单草稿：",
            answer.ticket_draft,
        ]
    )


def apply_data_source_coverage(answer: SupportAnswer | dict, coverage: DataSourceCoverage) -> SupportAnswer:
    answer = _coerce_support_answer(answer)
    used = [item.source_name for item in coverage.items if item.status == "hit"]
    missing = [item.source_name for item in coverage.items if item.status != "hit"]
    return answer.model_copy(
        update={
            "data_sources_used": used,
            "missing_data_sources": missing,
            "history_reference": _coverage_history_reference(answer.history_reference, coverage),
            "recommended_action": coverage.recommended_action,
            "owner_candidate": answer.owner_candidate or coverage.owner_candidate,
            "mention_enabled": coverage.mention_enabled,
        }
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


def _customer_visible_contract_text(text: str) -> str:
    return "\n".join(
        section
        for section in (
            _section_text(text, "建议回复（供客服参考，可复制调整）"),
            _section_text(text, "建议排查步骤"),
            _section_text(text, "需要追问"),
        )
        if section
    )


def _forbidden_commitment_issues(text: str) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for pattern in FORBIDDEN_COMMITMENT_PATTERNS:
        if _contains_forbidden_commitment(text, pattern):
            issues.append(ContractIssue("forbidden_commitment", f"可能包含售后承诺：{pattern}"))
    for pattern in FORBIDDEN_COMMITMENT_REGEX_PATTERNS:
        if _contains_forbidden_commitment_regex(text, pattern):
            issues.append(ContractIssue("forbidden_commitment", f"可能包含售后承诺：{pattern.pattern}"))
    return issues


def _coerce_support_answer(answer: SupportAnswer | dict) -> SupportAnswer:
    if isinstance(answer, SupportAnswer):
        return answer
    return SupportAnswer.model_validate(answer)


def _coverage_history_reference(history_reference: str, coverage: DataSourceCoverage) -> str:
    if not _coverage_has_reviewed_history(coverage):
        return history_reference
    clean_reference = str(history_reference or "").strip()
    if REVIEWED_HISTORY_MARKER_RE.search(clean_reference):
        return clean_reference
    marker = "已审核群聊历史 FAQ："
    suffix = "已命中，可作为可靠售后参考；不是正式政策源。"
    if not clean_reference or "未查询到可信历史参考" in clean_reference:
        return marker + suffix
    return marker + clean_reference


def _coverage_has_reviewed_history(coverage: DataSourceCoverage) -> bool:
    return any(
        item.source_id == "history_faq" and item.status == "hit" and item.authority == "reviewed"
        for item in coverage.items
    )


def _intro_paragraph(answer: SupportAnswer) -> str:
    summary = _visible_text(answer.user_issue_summary).rstrip("。！？") or "客户反馈的问题还需要进一步确认"
    direction = (
        "目前材料还不够直接下结论，建议先按信息收集和人工确认推进。"
        if _needs_cautious_visible_reply(answer)
        else "现有信息可以先支持客服做初步沟通，但最终处理口径仍建议人工确认。"
    )
    return f"我先看了一下，{summary}。{direction}"


def _action_paragraph(answer: SupportAnswer) -> str:
    parts = []
    steps = _visible_items(answer.troubleshooting_steps, limit=3)
    questions = _visible_follow_up_items(answer.follow_up_questions, limit=3)
    if steps:
        parts.append("处理前建议先" + "；".join(steps))
    if questions:
        parts.append("还需要向客户确认" + _join_follow_up_items(questions))
    if not parts:
        return ""
    return "；".join(parts) + "。"


def _source_paragraph(answer: SupportAnswer) -> str:
    parts = []
    used = [_visible_text(item).strip("。") for item in answer.data_sources_used[:3] if item.strip()]
    missing = [_visible_text(item).strip("。") for item in answer.missing_data_sources[:3] if item.strip()]
    if used:
        parts.append("这次主要参考了" + "、".join(used))
    if missing:
        parts.append("暂缺" + "、".join(missing))
    if answer.recommended_action == "human_review":
        parts.append("建议人工复核后再形成明确处理口径")
    elif answer.recommended_action == "ask_clarification":
        parts.append("建议先补齐关键信息")
    if not parts:
        return ""
    return "；".join(parts) + "。"


def _recommended_action_line(answer: SupportAnswer) -> str:
    labels = {
        "answer": "可给出保守客服参考",
        "ask_clarification": "先追问补充信息",
        "human_review": "建议人工复核",
    }
    owner = f"；负责人候选：{answer.owner_candidate}" if answer.owner_candidate else ""
    mention = "；mention_enabled=false" if not answer.mention_enabled else "；mention_enabled=true"
    return f"{answer.recommended_action}（{labels[answer.recommended_action]}{owner}{mention}）"


def _needs_cautious_visible_reply(answer: SupportAnswer) -> bool:
    combined = (
        f"{answer.confidence} {answer.confidence_reason} {answer.official_evidence} "
        f"{answer.history_reference} {answer.recommended_action}"
    )
    return (
        answer.confidence == "低"
        or answer.recommended_action in {"ask_clarification", "human_review"}
        or "未查询到可信正式依据" in combined
        or "未查询到可信历史参考" in combined
        or "未审核历史参考" in combined
        or "未审核媒体观察证据" in combined
        or "需人工确认" in combined
    )


def _visible_items(items: list[str], limit: int = 3) -> list[str]:
    return [item for item in (_visible_text(item).rstrip("。！？") for item in items[:limit]) if item]


def _visible_follow_up_items(items: list[str], limit: int = 3) -> list[str]:
    clean_items = []
    for item in _visible_items(items, limit=limit):
        clean = re.sub(r"^(?:请客户|请)?(?:补充|提供|确认|说明)", "", item).strip(" ：:，,")
        clean_items.append(clean or item)
    return clean_items


def _join_follow_up_items(items: list[str]) -> str:
    text = "；".join(items)
    return (" " + text) if text and re.match(r"[A-Za-z0-9]", text) else text


def _visible_text(value: str) -> str:
    text = str(value or "").replace("\r", "\n")
    lines = []
    for line in text.splitlines():
        clean = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s*)", "", line).strip()
        if clean:
            lines.append(clean)
    text = "；".join(lines)
    text = re.sub(r"[`*_#|]+", "", text)
    text = re.sub(r"(?:^|(?<=[：:；;。])\s*)\d+[.)]\s*", "", text)
    text = re.sub(r"\s+\d+[.)]\s*", "；", text)
    text = re.sub(r"([：:；;])\s*[；;]+", r"\1", text)
    text = re.sub(r"[；;]{2,}", "；", text)
    text = re.sub(r"\s+", " ", text).strip()
    replacements = {
        "未查询到可信正式依据": "目前还没有足够材料直接下结论",
        "未查询到可信历史参考": "目前没有可直接复用的历史处理口径",
        "未审核历史参考": "相似历史信息",
        "未审核媒体观察证据": "相似媒体线索",
        "Agent SDK": "",
        "SupportAnswer": "",
        "证据包": "参考信息",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    clean_text = text.strip(" ；。")
    if not clean_text:
        return ""
    return clean_text + ("。" if clean_text[-1] not in "。！？" else "")
