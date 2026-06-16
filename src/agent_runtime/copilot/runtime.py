from __future__ import annotations

import json
import logging
import time

from agents import Runner, SQLiteSession, custom_span
from agents.memory import SessionSettings
from openai import AsyncOpenAI, BadRequestError
from opentelemetry import trace as otel_trace
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.copilot.answer_contract import (
    ContractIssue,
    SupportAnswer,
    apply_data_source_coverage,
    contract_issues_for_output,
    render_support_answer,
)
from agent_runtime.copilot.case_context import DataSourceCoverage, SupportCaseContextResult, SupportCaseRequest
from agent_runtime.copilot.context_assembly import build_data_source_coverage
from agent_runtime.copilot.evidence import SupportEvidencePack, evidence_pack_trace_attributes, short_hash
from agent_runtime.copilot.evidence_collection import collect_support_evidence
from agent_runtime.copilot.pipeline import build_support_case_context
from agent_runtime.copilot.prompts import build_agent_input
from agent_runtime.copilot.support_copilot import build_support_copilot
from agent_runtime.llm import build_run_config
from agent_runtime.observability.tracing import elapsed_ms, hash_trace_id, status_attrs
from agent_runtime.settings import Settings
from agent_runtime.tools.history_rag import history_rag_index_available
from agent_runtime.tools.formal_kb import formal_kb_index_available
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
    contract_issues: list[ContractIssue] = Field(default_factory=list)
    trace_include_full_io: bool = False

    @property
    def blocked(self) -> bool:
        return bool(self.contract_issues)


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
) -> SupportRuntimeResult:
    agent = build_support_copilot(settings.support_agent_model)
    raw_issue = request.user_text
    raw_group_id = request.trace_group_id or request.session_id or request.request_id
    group_id = run_config_group_id or f"{entrypoint}:{hash_trace_id(raw_group_id)}"
    trace_session_id = request.session_id or request.trace_group_id or request.request_id
    runtime_full_io_attrs = _full_io_trace_attrs(
        settings,
        **{
            "core.input.value": _request_input_for_trace(request),
            "core.user.input": raw_issue,
            "request.assets": _asset_summary_for_trace(request),
            "chat_id": request.chat_id,
            "thread_id": request.thread_id,
            "message_id": request.message_id,
            "sender_id": request.sender_id,
        },
    )
    with custom_span(
        "support_core_runtime",
        {
            "entrypoint": entrypoint,
            "trace_kind": "runtime",
            "loop_version": "v2",
            "request_id_hash": hash_trace_id(request.request_id),
            "session_id_hash": hash_trace_id(trace_session_id),
            "session.id": hash_trace_id(trace_session_id),
            "raw_issue_hash": short_hash(raw_issue),
            "asset_count": len(request.assets),
            **runtime_full_io_attrs,
        },
    ):
        _set_current_otel_attrs(runtime_full_io_attrs)
        intake_started_at = time.perf_counter()
        with custom_span(
            "intake_pipeline",
            {
                "entrypoint": entrypoint,
                "request_id_hash": hash_trace_id(request.request_id),
                "asset_count": len(request.assets),
            },
        ):
            case_result = await build_support_case_context(request, settings)
        with custom_span(
            "intake_pipeline_result",
            {
                "status": "ok",
                "latency_ms": elapsed_ms(intake_started_at),
                "input_modality": case_result.route.input_modality,
                "artifact_count": len(case_result.artifacts),
                "asset_ref_count": len(case_result.context.asset_refs),
                "vector_ref_count": len(case_result.context.vector_refs),
                "missing_information_count": len(case_result.context.missing_information),
                **_full_io_trace_attrs(
                    settings,
                    **{
                        "context.input.value": case_result.context.normalized_query,
                        "context.normalized_query": case_result.context.normalized_query,
                        "context.original_user_text": case_result.context.original_user_text,
                        "context.detected_product": case_result.context.detected_product,
                        "context.detected_fault": case_result.context.detected_fault,
                        "context.customer_intent": case_result.context.customer_intent,
                        "context.missing_information": case_result.context.missing_information,
                        "context.extracted_texts": case_result.context.extracted_texts,
                        "context.visual_summaries": case_result.context.visual_summaries,
                    },
                ),
            },
        ):
            pass

        retrieval_started_at = time.perf_counter()
        with custom_span(
            "retrieval_pipeline",
            {
                "entrypoint": entrypoint,
                "request_id_hash": hash_trace_id(request.request_id),
                "query_hash": short_hash(case_result.context.normalized_query),
                "query_chars": len(case_result.context.normalized_query),
                "vector_ref_count": len(case_result.context.vector_refs),
                **_full_io_trace_attrs(settings, **{"retrieval.input.value": case_result.context.normalized_query}),
            },
        ):
            evidence_pack = await collect_support_evidence(case_result.context, settings)
        with custom_span(
            "retrieval_pipeline_result",
            {
                **evidence_pack_trace_attributes(evidence_pack),
                "status": "ok",
                "latency_ms": elapsed_ms(retrieval_started_at),
            },
        ):
            pass
        coverage = build_data_source_coverage(case_result.context, evidence_pack)
        agent_input = build_agent_input(
            raw_issue,
            source=source_label,
            evidence_pack=evidence_pack,
            case_context=case_result.context,
            coverage=coverage,
        )
        fallback_used = False
        fallback_reason = ""
        agent_started_at = time.perf_counter()
        try:
            with custom_span(
                "agent_answer",
                {
                    "entrypoint": entrypoint,
                    "model": settings.support_agent_model,
                    "provider": settings.llm_base_url,
                    "structured_output_requested": True,
                    "fallback_used": False,
                    "recommended_action_ceiling": coverage.recommended_action,
                },
            ):
                with custom_span(
                    "runner_run",
                    {
                        "entrypoint": entrypoint,
                        "model": settings.support_agent_model,
                        "structured_output_requested": True,
                        **_full_io_trace_attrs(
                            settings,
                            agent_input=agent_input,
                            **{"runner.input.value": agent_input},
                        ),
                    },
                ):
                    _set_current_otel_attrs(
                        _full_io_trace_attrs(
                            settings,
                            agent_input=agent_input,
                            **{"runner.input.value": agent_input},
                        )
                    )
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
            fallback_used = True
            fallback_reason = "response_format_unavailable"
            logger.warning(
                "Agent structured output response_format unavailable; using JSON fallback. entrypoint=%s request_id=%s",
                entrypoint,
                request.request_id,
            )
            with custom_span(
                "agent_answer",
                {
                    "entrypoint": entrypoint,
                    "model": settings.support_agent_model,
                    "provider": settings.llm_base_url,
                    "structured_output_requested": True,
                    "structured_output_supported": False,
                    "fallback_used": True,
                    "fallback_reason": fallback_reason,
                    "recommended_action_ceiling": coverage.recommended_action,
                },
            ):
                final_answer = apply_data_source_coverage(
                    await _run_support_answer_json_fallback(settings, agent_input, evidence_pack),
                    coverage,
                )
        finally:
            with custom_span(
                "agent_answer_result",
                {
                    "entrypoint": entrypoint,
                    "latency_ms": elapsed_ms(agent_started_at),
                    "fallback_used": fallback_used,
                    "fallback_reason": fallback_reason,
                },
            ):
                pass

        internal_text = render_support_answer(final_answer)
        with custom_span(
            "answer_contract_check",
            {
                **evidence_pack_trace_attributes(evidence_pack),
                "entrypoint": entrypoint,
                "recommended_action": coverage.recommended_action,
                "mention_enabled": coverage.mention_enabled,
            },
        ):
            contract_issues = contract_issues_for_output(final_answer, evidence_pack=evidence_pack)
        with custom_span(
            "answer_contract_result",
            status_attrs(
                "blocked" if contract_issues else "ok",
                entrypoint=entrypoint,
                contract_blocked=bool(contract_issues),
                output_contract_valid=not contract_issues,
                issue_codes=[issue.code for issue in contract_issues],
                recommended_action=coverage.recommended_action,
                mention_enabled=coverage.mention_enabled,
                **_full_io_trace_attrs(
                    settings,
                    internal_answer=internal_text,
                    **{"internal_answer.value": internal_text},
                ),
            ),
        ):
            _set_current_otel_attrs(
                _full_io_trace_attrs(
                    settings,
                    internal_answer=internal_text,
                    **{"internal_answer.value": internal_text},
                )
            )
            pass

    return SupportRuntimeResult(
        request=request,
        case_result=case_result,
        evidence_pack=evidence_pack,
        coverage=coverage,
        answer=final_answer,
        internal_text=internal_text,
        contract_issues=contract_issues,
        trace_include_full_io=settings.support_agent_trace_include_sensitive_data,
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
        "formal_kb_index_available": formal_kb_index_available(settings),
        "history_index_available": history_rag_index_available(settings),
        "media_index_available": media_rag_index_available(settings),
    }
    if metadata:
        output.update(metadata)
    return output


def _full_io_trace_attrs(settings: Settings, **attrs: object) -> dict[str, object]:
    if not settings.support_agent_trace_include_sensitive_data:
        return {}
    return {key: value for key, value in attrs.items() if value is not None and value != ""}


def _sensitive_trace_attrs(settings: Settings, **attrs: object) -> dict[str, object]:
    return _full_io_trace_attrs(settings, **attrs)


def _request_input_for_trace(request: SupportCaseRequest) -> str:
    pieces: list[str] = []
    if request.user_text:
        pieces.append(request.user_text)
    asset_summary = _asset_summary_for_trace(request)
    if asset_summary:
        pieces.append(f"附件：{asset_summary}")
    return "\n".join(pieces)


def _asset_summary_for_trace(request: SupportCaseRequest) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for asset in request.assets:
        item: dict[str, object] = {
            "asset_id": asset.asset_id,
            "media_type": asset.media_type,
        }
        if asset.filename:
            item["filename"] = asset.filename
        if asset.mime_type:
            item["mime_type"] = asset.mime_type
        if asset.metadata:
            item["metadata"] = asset.metadata
        output.append(item)
    return output


def _set_current_otel_attrs(attrs: dict[str, object]) -> None:
    if not attrs:
        return
    try:
        current_span = otel_trace.get_current_span()
        for key, value in attrs.items():
            current_span.set_attribute(key, _otel_attr_value(value))
    except Exception:
        logger.debug("Failed to set current OpenTelemetry span attributes", exc_info=True)


def _otel_attr_value(value: object) -> str | int | float | bool:
    if isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _is_response_format_unavailable(exc: BadRequestError) -> bool:
    return "response_format" in str(exc) and "unavailable" in str(exc).lower()


async def _run_support_answer_json_fallback(
    settings: Settings,
    agent_input: str,
    evidence_pack: SupportEvidencePack,
) -> SupportAnswer:
    system_prompt = _support_answer_json_fallback_instructions()
    sensitive_input_attrs = _sensitive_trace_attrs(
        settings,
        **{
            "llm.input_messages.system": system_prompt,
            "llm.input_messages.user": agent_input,
        },
    )
    span = custom_span(
        "agent_json_object_fallback",
        {
            **evidence_pack_trace_attributes(evidence_pack),
            "provider": "openai_compatible_chat_completions",
            "response_format": "json_object",
            **sensitive_input_attrs,
        },
    )
    with span:
        _set_current_otel_attrs(sensitive_input_attrs)
        client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
        response = await client.chat.completions.create(
            model=settings.support_agent_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": agent_input},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        sensitive_output_attrs = _sensitive_trace_attrs(
            settings,
            **{"llm.response.content": content},
        )
        span.span_data.data.update(sensitive_output_attrs)
        _set_current_otel_attrs(sensitive_output_attrs)
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
history_reference: 历史参考；已审核群聊历史 FAQ 可作为可靠售后参考；无命中时写“未查询到可信历史参考，不可编造。”
ticket_draft: 工单草稿或不建议生成工单原因

安全边界：
- 只能基于用户问题和证据包作答，不要声称查询了其他系统。
- 没有正式依据时，不得编造文档、链接、批次、负责人、政策或技术结论。
- 已审核群聊历史 FAQ 是可靠售后参考，但不是正式政策源，不能覆盖正式 KB/MRD/SOP。
- 未审核媒体线索只能作为内部参考，必须标注需人工确认，不能作为正式依据。
- 不得承诺退款、赔偿、换新、补发或处理时效。
- 不得写“我们将进一步核实/处理”“安排技术人员处理”“正在跟进”“已提交/已反馈/已升级给负责人”等暗示已经进入处理流程的话术；应改为“客服可先补充信息并提交人工复核”。
""".strip()
