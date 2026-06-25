import os
import re
import shutil
import sys
import termios
import tty
import unicodedata
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
APP_NAME = "VIJIM-after-sale-copilot"
ANSI = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def color(value: str, code: str) -> str:
    if not ANSI:
        return value
    return f"\033[{code}m{value}\033[0m"


def dim(value: str) -> str:
    return color(value, "2")


def cyan(value: str) -> str:
    return color(value, "36")


def green(value: str) -> str:
    return color(value, "32")


def yellow(value: str) -> str:
    return color(value, "33")


def display_width(value: str) -> int:
    width = 0
    for char in value:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def truncate_display(value: str, max_width: int) -> str:
    if display_width(value) <= max_width:
        return value
    output = ""
    used = 0
    for char in value:
        char_width = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if used + char_width > max_width - 1:
            break
        output += char
        used += char_width
    return output + "…"


def box_line(text: str, width: int) -> str:
    value = truncate_display(text, width)
    padding = " " * max(0, width - display_width(value))
    return cyan("│ ") + value + padding + cyan(" │")


def read_project_version() -> str:
    pyproject = ROOT / "pyproject.toml"
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else "unknown"


@dataclass(frozen=True)
class ModelPreset:
    key: str
    label: str
    model: str
    billing: str


def model_presets(settings) -> list[ModelPreset]:
    return [
        ModelPreset("flash", "Flash", settings.support_agent_model_flash, settings.support_agent_billing_mode),
        ModelPreset("pro", "Pro", settings.support_agent_model_pro, settings.support_agent_billing_mode),
    ]


def active_model_label(settings) -> str:
    for preset in model_presets(settings):
        if preset.model == settings.support_agent_model:
            return preset.label
    return "Custom"


def tool_names(agent) -> list[str]:
    return [tool.name for tool in agent.tools]


def print_panel(settings, agent) -> None:
    version = read_project_version()
    terminal_width = shutil.get_terminal_size((96, 24)).columns
    width = max(56, min(terminal_width - 6, 88))
    print()
    print(cyan("╭" + "─" * (width + 2) + "╮"))
    print(box_line(f"{APP_NAME} v{version}", width))
    print(box_line(f"{active_model_label(settings)} · {settings.support_agent_model} · {settings.support_agent_billing_mode}", width))
    print(box_line(str(Path.cwd()), width))
    print(cyan("╰" + "─" * (width + 2) + "╯"))
    print()


def print_status(settings, agent) -> None:
    print("\n" + cyan("当前配置"))
    print("运行模式: Agent SDK")
    print(f"项目版本: {read_project_version()}")
    print(f"当前模型: {active_model_label(settings)} · {settings.support_agent_model}")
    print(f"计费方式: {settings.support_agent_billing_mode}")
    print(f"Base URL: {settings.llm_base_url}")
    print(f"Tracing: {'enabled' if not settings.support_agent_tracing_disabled else 'disabled'}")
    print(f"Trace workflow: {settings.support_agent_trace_workflow_name}")
    print(f"Session limit: latest {settings.support_agent_session_limit} items")
    print("Agent 数量: 1")
    print(f"工具数量: {len(agent.tools)}")
    print(f"SKU catalog: {settings.sku_catalog_path}\n")


def print_help() -> None:
    print(
        "\n"
        + cyan("命令")
        + "\n"
        + "- /model   选择 Flash 或 Pro 模型\n"
        + "- /clear   清除当前终端会话上下文\n"
        + "- /compact 压缩当前终端会话上下文\n"
        + "- /info    打开 Agent / 工具信息窗口\n"
        + "- /status  查看项目、模型、Tracing、Agent 和工具\n"
        + "- /agents  查看当前 Agent\n"
        + "- /tools   查看当前工具\n"
        + "- /help    查看命令\n"
        + "- /bye     退出\n"
    )


def print_agents(agent) -> None:
    print(f"\nAgent（1）\n- {agent.name}\n")


def print_tools(agent) -> None:
    print(f"\n工具（{len(agent.tools)}）")
    for name in tool_names(agent):
        print(f"- {name}")
    print()


def prompt_text(settings, agent, context_count: int) -> str:
    return (
        f"{cyan('╭─')} {APP_NAME} {dim(active_model_label(settings))}\n"
        f"{cyan('│')} 上下文: {context_count} · {settings.support_agent_model}\n"
        f"{cyan('╰─')} 客服问题 > "
    )


def render_inline_box(
    title: str,
    lines: list[str],
    selected: int | None = None,
    footer: str = "↑/↓ 选择 · Enter 打开 · q 退出",
) -> str:
    terminal_width = shutil.get_terminal_size((96, 24)).columns
    content_width = max(
        [display_width(title), display_width(footer), *(display_width(line) + 2 for line in lines)],
        default=30,
    )
    width = min(max(content_width + 4, 34), max(34, terminal_width - 8))
    output = [cyan("╭" + "─" * (width + 2) + "╮")]
    output.append(box_line(title, width))
    output.append(cyan("├" + "─" * (width + 2) + "┤"))
    for index, line in enumerate(lines):
        prefix = "› " if selected == index else "  "
        value = prefix + line
        output.append(box_line(value, width))
    output.append(cyan("├" + "─" * (width + 2) + "┤"))
    output.append(box_line(footer, width))
    output.append(cyan("╰" + "─" * (width + 2) + "╯"))
    return "\n".join(output)


def read_single_key() -> str:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        first = sys.stdin.read(1)
        if first == "\x1b":
            second = sys.stdin.read(1)
            if second == "[":
                third = sys.stdin.read(1)
                return first + second + third
            return first + second
        return first
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def clear_rendered_block(line_count: int) -> None:
    if not ANSI:
        return
    sys.stdout.write(f"\033[{line_count}A\033[J")
    sys.stdout.flush()


def show_detail_box(title: str, lines: list[str]) -> None:
    rendered = render_inline_box(title, lines, footer="Enter/q 返回")
    print(rendered)
    if sys.stdin.isatty() and sys.stdout.isatty():
        while True:
            key = read_single_key()
            if key in {"\r", "\n", "q", "Q", "\x1b"}:
                break
    else:
        input("按回车返回 > ")
    clear_rendered_block(rendered.count("\n") + 1)


def info_menu(agent) -> None:
    options = ["Agent", "工具"]
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print_agents(agent)
        print_tools(agent)
        return

    selected = 0
    rendered = ""
    while True:
        if rendered:
            clear_rendered_block(rendered.count("\n") + 1)
        rendered = render_inline_box("运行信息", options, selected)
        print(rendered)
        key = read_single_key()
        if key in {"q", "Q", "\x1b"}:
            clear_rendered_block(rendered.count("\n") + 1)
            return
        if key == "\x1b[A":
            selected = (selected - 1) % len(options)
            continue
        if key == "\x1b[B":
            selected = (selected + 1) % len(options)
            continue
        if key in {"\r", "\n"}:
            clear_rendered_block(rendered.count("\n") + 1)
            rendered = ""
            if selected == 0:
                show_detail_box("Agent", [agent.name])
            else:
                show_detail_box("工具", tool_names(agent))
