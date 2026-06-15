from __future__ import annotations

import re
from typing import Any

from agent_runtime.channels.feishu_reply import render_feishu_visible_runtime_reply
from agent_runtime.copilot.runtime import SupportRuntimeResult
from agent_runtime.observability.tracing import span_if_tracing


MARKDOWN_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
MARKDOWN_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
MARKDOWN_CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\n?|\n?```")


def build_openclaw_thread_reply(result: SupportRuntimeResult) -> dict[str, Any]:
    visible_reply = render_feishu_visible_runtime_reply(result)
    request = result.request
    with span_if_tracing(
        "channel_reply",
        {
            "entrypoint": "openclaw_feishu",
            "reply_target": "thread",
            "reply_status": "payload_built",
            "reply_in_thread": True,
            "blocked": visible_reply.blocked,
            "recommended_action": result.coverage.recommended_action,
        },
    ):
        pass
    reply = {
        "channel": "feishu",
        "mode": "thread_reply",
        "chatId": request.chat_id,
        "threadId": request.thread_id,
        "replyToMessageId": request.message_id,
        "replyInThread": True,
        "preferredFormat": "post",
        "text": visible_reply.safe_text,
        "fallbackText": readable_plain_text(visible_reply.safe_text),
        "metadata": {
            "source": "support_copilot",
            "requestId": request.request_id,
            "recommendedAction": result.coverage.recommended_action,
            "mentionEnabled": False,
            "blocked": visible_reply.blocked,
            "issueCodes": [issue.code for issue in visible_reply.issues],
        },
    }
    with span_if_tracing(
        "channel_reply_result",
        {
            "entrypoint": "openclaw_feishu",
            "reply_target": "thread",
            "reply_status": "payload_built",
            "reply_transport": "payload_only",
            "reply_in_thread": True,
            "blocked": visible_reply.blocked,
            "recommended_action": result.coverage.recommended_action,
            "payload_text_chars": len(reply["text"]),
            "fallback_text_chars": len(reply["fallbackText"]),
        },
    ):
        pass
    return reply


def readable_plain_text(text: str) -> str:
    output = MARKDOWN_CODE_FENCE_RE.sub("", text)
    output = MARKDOWN_HEADING_RE.sub("", output)
    output = MARKDOWN_BOLD_RE.sub(r"\1", output)
    output = _flatten_markdown_tables(output)
    return "\n".join(line.rstrip() for line in output.splitlines()).strip()


def _flatten_markdown_tables(text: str) -> str:
    lines = text.splitlines()
    output = []
    for line in lines:
        if not MARKDOWN_TABLE_RE.match(line):
            output.append(line)
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        output.append(" / ".join(cell for cell in cells if cell))
    return "\n".join(output)
