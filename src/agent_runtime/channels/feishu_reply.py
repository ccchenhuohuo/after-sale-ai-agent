from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.copilot.answer_contract import (
    ContractIssue,
    FEISHU_VISIBLE_REPLY_FALLBACK,
    render_feishu_reply,
    validate_feishu_visible_reply,
)
from agent_runtime.copilot.runtime import SupportRuntimeResult


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
    if result.contract_issues:
        return FeishuVisibleReply(text=FEISHU_VISIBLE_REPLY_FALLBACK, issues=result.contract_issues)
    text = render_feishu_reply(result.answer)
    return FeishuVisibleReply(text=text, issues=validate_feishu_visible_reply(text))
