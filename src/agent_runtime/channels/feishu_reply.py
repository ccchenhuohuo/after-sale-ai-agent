from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.copilot.answer_contract import (
    ContractIssue,
    FEISHU_VISIBLE_REPLY_FALLBACK,
    render_feishu_reply,
    validate_feishu_visible_reply,
)
from agent_runtime.copilot.runtime import SupportRuntimeResult
from agent_runtime.observability.tracing import set_current_otel_attrs, span_if_tracing


class FeishuVisibleReply(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    issues: list[ContractIssue] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.issues)

    @property
    def safe_text(self) -> str:
        return FEISHU_VISIBLE_REPLY_FALLBACK if self.blocked else self.text


def render_feishu_visible_runtime_reply(result: SupportRuntimeResult) -> FeishuVisibleReply:
    coverage = getattr(result, "coverage", None)
    with span_if_tracing(
        "visible_reply_render",
        {
            "recommended_action": getattr(coverage, "recommended_action", ""),
            "mention_enabled": getattr(coverage, "mention_enabled", False),
        },
    ) as trace_span:
        no_source_hit_fallback = _should_use_no_source_hit_fallback(result)
        if no_source_hit_fallback:
            reply = FeishuVisibleReply(text=FEISHU_VISIBLE_REPLY_FALLBACK, issues=[])
            fallback_reason = "no_source_hit_fallback"
        elif result.contract_issues:
            reply = FeishuVisibleReply(text=FEISHU_VISIBLE_REPLY_FALLBACK, issues=result.contract_issues)
            fallback_reason = "answer_contract_blocked"
        else:
            text = render_feishu_reply(result.answer)
            reply = FeishuVisibleReply(text=text, issues=validate_feishu_visible_reply(text))
            fallback_reason = "visible_reply_validation_blocked" if reply.blocked else ""
        if trace_span is not None:
            full_io_attrs = {}
            if getattr(result, "trace_include_full_io", False):
                full_io_attrs = {
                    "visible_reply": reply.safe_text,
                    "visible_reply.value": reply.safe_text,
                }
            trace_attrs = {
                "recommended_action": getattr(coverage, "recommended_action", ""),
                "mention_enabled": getattr(coverage, "mention_enabled", False),
                "contract_blocked": bool(result.contract_issues),
                "visible_reply_fallback_used": bool(fallback_reason),
                "fallback_reason": fallback_reason,
                "issue_codes": [issue.code for issue in reply.issues],
                "reply_chars": len(reply.safe_text),
                "output_chars": len(reply.safe_text),
                **full_io_attrs,
            }
            trace_span.span_data.data.update(trace_attrs)
            set_current_otel_attrs(trace_attrs)
        return reply


def _should_use_no_source_hit_fallback(result: SupportRuntimeResult) -> bool:
    coverage = getattr(result, "coverage", None)
    items = getattr(coverage, "items", None)
    if not isinstance(items, list) or not items:
        return False
    return not any(
        getattr(item, "status", "") == "hit" and int(getattr(item, "hit_count", 0) or 0) > 0 for item in items
    )
