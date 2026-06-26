import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any


SOURCE_SYSTEM = "yunting"
SOURCE_TYPE = "yunting_service_history_faq"
REFERENCE_CLASS = "support_history_faq"
AUTHORITY_LEVEL = "low"
AUTHORITY_SCORE = 0.45
TEXT_COLLECTION = "yunting_service_text_v1_dev"
MEDIA_COLLECTION = "yunting_service_media_v1_dev"


def now_ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def compact_json(value: Any) -> str:
    if value is None:
        value = {}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_array(value: Any) -> str:
    if value is None:
        value = []
    if not isinstance(value, list):
        value = [value]
    return compact_json(value)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(*parts: Any, length: int = 32) -> str:
    text = "||".join(str(part or "") for part in parts)
    return sha256_text(text)[:length]


def stable_uuid(*parts: Any) -> str:
    raw = stable_id(*parts, length=32)
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def clean_text(value: Any) -> str:
    text = str(value or "").replace("[Invalid text JSON]", "").strip()
    return re.sub(r"\s+", " ", text)


def boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def first_present(data: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = data.get(name)
        if value is not None:
            return value
    return default


def normalize_time(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if text.isdigit():
        raw = int(text)
        if raw > 10_000_000_000:
            raw = raw / 1000
        return datetime.fromtimestamp(raw).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return text


def stat_date_from(value: Any) -> str:
    text = normalize_time(value)
    if len(text) >= 10:
        return text[:10].replace("-", "")
    return datetime.now().strftime("%Y%m%d")


def stat_week_from(value: Any) -> str:
    text = normalize_time(value)
    if len(text) >= 10:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
            year, week, _ = dt.isocalendar()
            return f"{year}-{week:02d}"
        except ValueError:
            pass
    dt = datetime.now()
    year, week, _ = dt.isocalendar()
    return f"{year}-{week:02d}"


def source_url_from_content(content: str) -> str:
    match = re.search(r"https?://[^\s\]\)\"']+", content or "")
    return match.group(0) if match else ""
