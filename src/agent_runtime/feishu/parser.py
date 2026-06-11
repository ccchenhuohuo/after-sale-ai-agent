import json
from typing import Iterable, Optional


def extract_text_content(content: object) -> str:
    """Extract text from Feishu message.content."""
    if content is None:
        return ""

    if isinstance(content, str):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return content
    elif isinstance(content, dict):
        data = content
    else:
        return str(content)

    text = data.get("text")
    if isinstance(text, str):
        return text

    title = data.get("title")
    parts = [title] if isinstance(title, str) else []
    for block in data.get("content", []) if isinstance(data.get("content"), list) else []:
        for item in block if isinstance(block, list) else []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])

    return "\n".join(part for part in parts if part).strip()


def should_trigger_ai(
    text: str,
    trigger_prefix: str = "AI分析：",
    mention_names: Optional[Iterable[str]] = None,
) -> bool:
    normalized = text.strip()
    if not normalized:
        return False

    if trigger_prefix and normalized.startswith(trigger_prefix):
        return True

    if mention_names:
        return any(name and name in normalized for name in mention_names)

    return False


def strip_trigger_prefix(text: str, trigger_prefix: str = "AI分析：") -> str:
    normalized = text.strip()
    if trigger_prefix and normalized.startswith(trigger_prefix):
        return normalized[len(trigger_prefix) :].strip()
    return normalized
