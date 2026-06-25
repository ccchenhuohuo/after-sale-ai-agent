from __future__ import annotations

import re


REFERENCE_REF_PREFIX = r"\s：:，。；、;,(（"
REFERENCE_VALUE_BOUNDARY = r"\s，。；、;,)）"

REFERENCE_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|"
            r"(?:api[_-]?key|app[_-]?secret|access[_-]?token|tenant[_-]?token|authorization|password)"
            r"\s*[:=]\s*[^"
            + REFERENCE_VALUE_BOUNDARY
            + r"]+)",
            re.IGNORECASE,
        ),
        "[redacted-secret]",
    ),
    (re.compile(r"\[[+-]?(?:0|1)?\.\d{2,}\s*,\s*[+-]?(?:0|1)?\.\d{2,}[^\]]*\]"), "[redacted-vector]"),
    (re.compile(rf"\bhttps?://[^{REFERENCE_VALUE_BOUNDARY}]+", re.IGNORECASE), "[redacted-url]"),
    (re.compile(rf"\bfile://[^{REFERENCE_VALUE_BOUNDARY}]+", re.IGNORECASE), "[redacted-path]"),
    (
        re.compile(rf"(?:^|[{REFERENCE_REF_PREFIX}])/(?:tmp|var|opt|home|Users|private|mnt|data)/[^{REFERENCE_VALUE_BOUNDARY}]+"),
        " [redacted-path]",
    ),
    (re.compile(rf"\b[A-Za-z]:\\[^{REFERENCE_VALUE_BOUNDARY}]+"), "[redacted-path]"),
    (
        re.compile(
            rf"\b(?:file[_-]?key|fileKey|imageKey|mediaKey|file_token)\s*[:=]\s*[^{REFERENCE_VALUE_BOUNDARY}]+",
            re.IGNORECASE,
        ),
        "[redacted-file-key]",
    ),
    (re.compile(r"\b(?:file[_-]?key|fileKey|imageKey|mediaKey|file_token)\b", re.IGNORECASE), "[redacted-file-key]"),
    (re.compile(r"\b(?:img|file|media)_[A-Za-z0-9][A-Za-z0-9_-]{6,}\b", re.IGNORECASE), "[redacted-file-ref]"),
    (re.compile(r"\b(?:vector[_-]?id|vector ref|vector_ref)\s*[:=]?\s*[A-Za-z0-9_:-]*", re.IGNORECASE), "[redacted-vector-ref]"),
    (re.compile(r"\b(?:vec|vector)[:_][A-Za-z0-9][A-Za-z0-9_:-]{6,}\b", re.IGNORECASE), "[redacted-vector-ref]"),
)


def redact_internal_references(value: object, *, max_chars: int | None = None) -> str:
    text = str(value or "")
    if not text:
        return ""
    for pattern, replacement in REFERENCE_REDACTIONS:
        text = pattern.sub(replacement, text)
    if max_chars is not None and max_chars >= 0:
        text = text[:max_chars]
    return text
