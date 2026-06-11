from agents import SQLiteSession
from agents.memory import SessionSettings


def build_terminal_session(settings) -> SQLiteSession:
    return SQLiteSession(
        "terminal-chat",
        session_settings=SessionSettings(limit=settings.support_agent_session_limit),
    )


async def session_item_count(session: SQLiteSession) -> int:
    return len(await session.get_items(limit=2_147_483_647))


async def clear_context(session: SQLiteSession) -> None:
    await session.clear_session()


def content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or part))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content or "")


def session_items_to_text(items: list[dict]) -> str:
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            lines.append(f"{index}. {item}")
            continue
        role = item.get("role") or item.get("type") or "item"
        content = content_to_text(item.get("content") or item.get("output") or item)
        if len(content) > 4000:
            content = content[:4000] + "\n...[truncated]"
        lines.append(f"## {index}. {role}\n{content}")
    return "\n\n".join(lines)
