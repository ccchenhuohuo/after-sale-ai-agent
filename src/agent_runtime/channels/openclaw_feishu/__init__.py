"""Compatibility layer between OpenClaw Feishu messages and support copilot schemas."""

from agent_runtime.channels.openclaw_feishu.adapter import build_support_case_request_from_openclaw
from agent_runtime.channels.openclaw_feishu.assets import support_asset_from_openclaw_resource
from agent_runtime.channels.openclaw_feishu.responder import build_openclaw_thread_reply

__all__ = [
    "build_openclaw_thread_reply",
    "build_support_case_request_from_openclaw",
    "support_asset_from_openclaw_resource",
]
