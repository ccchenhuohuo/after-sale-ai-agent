from __future__ import annotations

import re


REFERENCE_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bhttps?://[^\s，。；;,)）]+", re.IGNORECASE), "[redacted-url]"),
    (re.compile(r"\bfile://[^\s，。；;,)）]+", re.IGNORECASE), "[redacted-path]"),
    (re.compile(r"(?:^|[\s：:])/(?:tmp|var|opt|home|Users|private|mnt|data)/[^\s，。；;,)）]+"), " [redacted-path]"),
    (re.compile(r"\b[A-Za-z]:\\[^\s，。；;,)）]+"), "[redacted-path]"),
    (
        re.compile(r"\b(?:file[_-]?key|fileKey|imageKey|mediaKey|file_token)\s*[:=]\s*[^\s，。；;,)）]+", re.IGNORECASE),
        "[redacted-file-key]",
    ),
    (re.compile(r"\b(?:file[_-]?key|fileKey|imageKey|mediaKey|file_token)\b", re.IGNORECASE), "[redacted-file-key]"),
    (re.compile(r"\b(?:img|file|media)_[A-Za-z0-9][A-Za-z0-9_-]{6,}\b", re.IGNORECASE), "[redacted-file-ref]"),
    (re.compile(r"\b(?:vector[_-]?id|vector ref|vector_ref)\s*[:=]?\s*[A-Za-z0-9_:-]*", re.IGNORECASE), "[redacted-vector-ref]"),
    (re.compile(r"\b(?:vec|vector)[:_][A-Za-z0-9][A-Za-z0-9_:-]{6,}\b", re.IGNORECASE), "[redacted-vector-ref]"),
    (re.compile(r"\[[+-]?(?:0|1)?\.\d{2,}\s*,\s*[+-]?(?:0|1)?\.\d{2,}[^\]]*\]"), "[redacted-vector]"),
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
