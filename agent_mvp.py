import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_runtime.copilot.support_copilot import build_support_copilot
from agent_runtime.llm import configure_agents_runtime
from agent_runtime.settings import get_settings
from agent_runtime.terminal.runtime import build_compactor, compact_context, run_turn
from agent_runtime.terminal.session import (
    build_terminal_session,
    clear_context as clear_terminal_context,
    session_item_count,
    session_items_to_text,
)
from agent_runtime.terminal.ui import (
    active_model_label,
    green,
    info_menu,
    model_presets,
    print_agents,
    print_help,
    print_panel,
    print_status,
    print_tools,
    prompt_text,
    render_inline_box,
    yellow,
)


COMMANDS = {"/bye", "/exit", "/quit"}


def select_model(settings):
    presets = model_presets(settings)
    print("\n选择模型")
    for index, preset in enumerate(presets, start=1):
        current = " " + green("当前") if preset.model == settings.support_agent_model else ""
        print(f"{index}. {preset.label:<5} {preset.model} · {preset.billing}{current}")
    choice = input("输入 1/2，或直接回车取消 > ").strip()
    if not choice:
        print("已取消模型切换。\n")
        return None
    if choice not in {"1", "2"}:
        print(yellow("无效选择，模型未切换。\n"))
        return None

    preset = presets[int(choice) - 1]
    settings.support_agent_model = preset.model
    agent = build_support_copilot(settings.support_agent_model)
    print(green(f"已切换到 {preset.label}: {preset.model}\n"))
    return agent


async def clear_context(session) -> None:
    await clear_terminal_context(session)
    print(green("已清除当前终端会话上下文。\n"))


async def main() -> None:
    settings = configure_agents_runtime(get_settings())
    agent = build_support_copilot(settings.support_agent_model)
    session = build_terminal_session(settings)
    print_panel(settings, agent)

    while True:
        try:
            context_count = await session_item_count(session)
            user_input = input(prompt_text(settings, agent, context_count)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return

        if not user_input:
            continue

        command = user_input.lower()
        if command in COMMANDS:
            print("已退出。")
            return
        if command == "/help":
            print_help()
            continue
        if command == "/status":
            print_status(settings, agent)
            continue
        if command == "/agents":
            print_agents(agent)
            continue
        if command == "/tools":
            print_tools(agent)
            continue
        if command == "/info":
            info_menu(agent)
            continue
        if command == "/clear":
            await clear_context(session)
            continue
        if command == "/compact":
            await compact_context(settings, session)
            continue
        if command == "/model":
            selected_agent = select_model(settings)
            if selected_agent is not None:
                agent = selected_agent
            continue

        await run_turn(agent, settings, session, user_input)


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
