import sys
from pathlib import Path

from agent_runtime.copilot.prompts import SUPPORT_COPILOT_INSTRUCTIONS
from agent_runtime.copilot.support_copilot import build_support_copilot
from agent_runtime.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mvp import (
    active_model_label,
    build_compactor,
    model_presets,
    prompt_text,
    render_inline_box,
    session_items_to_text,
)


def test_prompt_keeps_terminal_answer_contract():
    required_sections = [
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

    for section in required_sections:
        assert section in SUPPORT_COPILOT_INSTRUCTIONS

    assert "不能编造文档、案例、链接、批次、负责人、政策或技术结论" in SUPPORT_COPILOT_INSTRUCTIONS
    assert "不要增加无关字段" in SUPPORT_COPILOT_INSTRUCTIONS
    assert "SKU 精确命中只能说明产品识别可靠" in SUPPORT_COPILOT_INSTRUCTIONS
    assert "整体置信度必须为低" in SUPPORT_COPILOT_INSTRUCTIONS


def test_agent_exposes_only_agent_sdk_tools():
    agent = build_support_copilot("test-model")
    tool_names = [tool.name for tool in agent.tools]

    assert tool_names == [
        "search_sku_catalog",
        "hybrid_search_kb",
        "search_issue_history",
        "create_ticket_draft",
    ]


def test_terminal_model_presets_expose_flash_and_pro():
    settings = Settings(
        support_agent_model="deepseek-v4-flash",
        support_agent_model_flash="deepseek-v4-flash",
        support_agent_model_pro="deepseek-v4-pro",
    )

    presets = model_presets(settings)

    assert [preset.label for preset in presets] == ["Flash", "Pro"]
    assert [preset.model for preset in presets] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert active_model_label(settings) == "Flash"


def test_terminal_compactor_has_no_tools():
    compactor = build_compactor("test-model")

    assert compactor.name == "终端上下文压缩器"
    assert compactor.tools == []


def test_session_items_to_text_preserves_roles_and_content():
    text = session_items_to_text(
        [
            {"role": "user", "content": "第一轮问题"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "第一轮回答"}]},
        ]
    )

    assert "user" in text
    assert "第一轮问题" in text
    assert "assistant" in text
    assert "第一轮回答" in text


def test_terminal_prompt_keeps_only_context_and_model_status():
    settings = Settings(support_agent_model="deepseek-v4-flash")
    agent = build_support_copilot(settings.support_agent_model)

    prompt = prompt_text(settings, agent, context_count=3)

    assert "上下文: 3" in prompt
    assert "deepseek-v4-flash" in prompt
    assert "Agent 数量:" not in prompt
    assert "工具数量:" not in prompt


def test_info_box_renders_inline_without_fullscreen():
    rendered = render_inline_box("运行信息", ["Agent", "工具"], selected=0)

    assert "运行信息" in rendered
    assert "› Agent" in rendered
    assert "工具" in rendered
