import asyncio

import pytest

from agent_runtime.feishu.message_sender import TopLevelMessageBlocked, send_feishu_text_message
from agent_runtime.settings import Settings


def test_top_level_message_create_is_blocked_by_default():
    settings = Settings(feishu_app_id="cli_x", feishu_app_secret="secret")

    with pytest.raises(TopLevelMessageBlocked):
        asyncio.run(send_feishu_text_message("oc_chat", "hello", settings))


def test_top_level_message_create_requires_operator_reason():
    settings = Settings(feishu_app_id="cli_x", feishu_app_secret="secret")

    with pytest.raises(TopLevelMessageBlocked):
        asyncio.run(
            send_feishu_text_message(
                "oc_chat",
                "hello",
                settings,
                allow_top_level=True,
                reason="",
            )
        )
