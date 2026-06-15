from __future__ import annotations

import json
import logging

from agents import Runner, SQLiteSession, custom_span, flush_traces
from agents.memory import SessionSettings
from openai import AsyncOpenAI, BadRequestError
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.copilot.answer_contract import (
    ContractIssue,
    SupportAnswer,
    apply_data_source_coverage,
    render_feishu_reply,
    render_support_answer,
    validate_answer_contract,
    validate_feishu_visible_reply,
)
from agent_runtime.copilot.case_context import DataSourceCoverage, SupportCaseContextResult, SupportCaseRequest
from agent_runtime.copilot.context_assembly import build_data_source_coverage
from agent_runtime.copilot.evidence import SupportEvidencePack, evidence_pack_trace_attributes, short_hash
from agent_runtime.copilot.evidence_collection import collect_support_evidence
from agent_runtime.copilot.pipeline import build_support_case_context
from agent_runtime.copilot.prompts import build_agent_input
from agent_runtime.copilot.support_copilot import build_support_copilot
from agent_runtime.llm import build_run_config
from agent_runtime.settings import Settings
from agent_runtime.tools.history_rag import history_rag_index_available
from agent_runtime.tools.media_rag import media_rag_index_available


logger = logging.getLogger(__name__)


class SupportRuntimeResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    request: SupportCaseRequest
    case_result: SupportCaseContextResult
    evidence_pack: SupportEvidencePack
    coverage: DataSourceCoverage
    answer: SupportAnswer
    internal_text: str
    visible_text: str
    contract_issues: list[ContractIssue] = Field(default_factory=list)
    visible_issues: list[ContractIssue] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.contract_issues or self.visible_issues)


def build_support_runtime_session(settings: Settings, session_id: str) -> SQLiteSession:
    return SQLiteSession(
        session_id,
        db_path=settings.support_agent_session_db_path,
        session_settings=SessionSettings(limit=settings.support_agent_session_limit),
    )


async def run_support_case_request(
    request: SupportCaseRequest,
    settings: Settings,
    *,
    entrypoint: str,
    source_label: str,
    session: SQLiteSession | None = None,
    run_config_group_id: str = "",
    run_config_metadata: dict[str, object] | None = None,
    render_visible_reply: bool = False,
) -> SupportRuntimeResult:
    agent = build_support_copilot(settings.support_agent_model)
    raw_issue = request.user_text
    group_id = run_config_group_id or request.trace_group_id or request.session_id or request.request_id
    with custom_span(
        "support_turn",
        {
            "entrypoint": entrypoint,
            "loop_version": "v2",
            "raw_issue_hash": short_hash(raw_issue),
        },
    ):
        case_result = await build_support_case_context(request, settings)
        evidence_pack = await collect_support_evidence(case_result.context.normalized_query, settings)
        coverage = build_data_source_coverage(case_result.context, evidence_pack)
        agent_input = build_agent_input(
            raw_issue,
            source=source_label,
            evidence_pack=evidence_pack,
            case_context=case_result.context,
            coverage=coverage,
        )
        try:
            result = await Runner.run(
                agent,
                agent_input,
                context=evidence_pack,
                session=session,
                run_config=build_run_config(
                    settings,
                    group_id=group_id,
                    metadata=_run_config_metadata(settings, entrypoint, run_config_metadata),
                ),
            )
            final_answer = apply_data_source_coverage(result.final_output, coverage)
        except BadRequestError as exc:
            if not _is_response_format_unavailable(exc):
                raise
            logger.warning(
                "Agent structured output response_format unavailable; using JSON fallback. entrypoint=%s request_id=%s",
                entrypoint,
                request.request_id,
            )
            final_answer = apply_data_source_coverage(
                await _run_support_answer_json_fallback(settings, agent_input, evidence_pack),
                coverage,
            )
        finally:
            flush_traces()

        internal_text = render_support_answer(final_answer)
        with custom_span(
            "answer_contract_check",
            {
                **evidence_pack_trace_attributes(evidence_pack),
                "entrypoint": entrypoint,
            },
        ):
            contract_issues = validate_answer_contract(
                internal_text,
                history_connected=history_rag_index_available(settings),
            )

        if render_visible_reply:
            visible_text = render_feishu_reply(final_answer)
            visible_issues = validate_feishu_visible_reply(visible_text)
            with custom_span(
                "visible_reply_check",
                {
                    "entrypoint": entrypoint,
                    "internal_issue_codes": [issue.code for issue in contract_issues],
                    "visible_issue_codes": [issue.code for issue in visible_issues],
                },
            ):
                pass
        else:
            visible_text = internal_text
            visible_issues = []

        flush_traces()

    return SupportRuntimeResult(
        request=request,
        case_result=case_result,
        evidence_pack=evidence_pack,
        coverage=coverage,
        answer=final_answer,
        internal_text=internal_text,
        visible_text=visible_text,
        contract_issues=contract_issues,
        visible_issues=visible_issues,
    )


def _run_config_metadata(
    settings: Settings,
    entrypoint: str,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    output = {
        "source": f"{entrypoint}-support-runtime",
        "entrypoint": entrypoint,
        "loop_version": "v2",
        "history_index_available": history_rag_index_available(settings),
        "media_index_available": media_rag_index_available(settings),
    }
    if metadata:
        output.update(metadata)
    return output


def _is_response_format_unavailable(exc: BadRequestError) -> bool:
    return "response_format" in str(exc) and "unavailable" in str(exc).lower()


async def _run_support_answer_json_fallback(
    settings: Settings,
    agent_input: str,
    evidence_pack: SupportEvidencePack,
) -> SupportAnswer:
    with custom_span(
        "agent_json_object_fallback",
        {
            **evidence_pack_trace_attributes(evidence_pack),
            "provider": "openai_compatible_chat_completions",
            "response_format": "json_object",
        },
    ):
        client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
        response = await client.chat.completions.create(
            model=settings.support_agent_model,
            messages=[
                {"role": "system", "content": _support_answer_json_fallback_instructions()},
                {"role": "user", "content": agent_input},
            ],
            response_format={"type": "json_object"},
        )
    content = response.choices[0].message.content or "{}"
    try:
        return SupportAnswer.model_validate_json(content)
    except Exception:
        return SupportAnswer.model_validate(json.loads(content))


def _support_answer_json_fallback_instructions() -> str:
    return """
你是飞书客服群里的 AI 客服参考助手。请只输出一个合法 JSON object，不要输出 Markdown 或解释文字。

JSON 字段必须完整：
issue_type: product_usage / troubleshooting / quality_issue / ticket_followup / unknown
run_mode: 固定为 Agent SDK
confidence: 高 / 中 / 低
confidence_reason: 一句话说明原因
user_issue_summary: 客户问题摘要
sku_match: SKU 命中说明
suggested_reply: 供客服参考、可复制调整的回复
troubleshooting_steps: 字符串数组
follow_up_questions: 字符串数组
official_evidence: 正式依据；无正式命中时写“未查询到可信正式依据，不可编造。”
history_reference: 历史参考；无命中时写“未查询到可信历史参考，不可编造。”
ticket_draft: 工单草稿或不建议生成工单原因

安全边界：
- 只能基于用户问题和证据包作答，不要声称查询了其他系统。
- 没有正式依据时，不得编造文档、链接、批次、负责人、政策或技术结论。
- 未审核历史或媒体线索只能作为内部参考，必须标注需人工确认，不能作为正式依据。
- 不得承诺退款、赔偿、换新、补发或处理时效。
""".strip()
