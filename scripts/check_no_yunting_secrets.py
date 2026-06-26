from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


SECRET_PATTERNS = (
    re.compile(r"\b(sk-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,})"),
    re.compile(r"(?im)^\s*(api[_-]?key|access[_-]?token|app[_-]?secret|password)\s*=\s*[^\s#]+"),
    re.compile(r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
)

RAW_DATA_MARKERS = (
    "data/yunting/service/raw/",
    "data/yunting/service/layers/",
    "media/sha256/",
)

ALLOWLISTED_FILES = {
    "docs/yunting-service-doris-qdrant-pipeline.md",
    "docs/yunting-service-remediation-plan.md",
    ".env.example",
}


def should_scan_secrets(path: Path) -> bool:
    path_text = str(path)
    return (
        path.name.startswith(".env")
        or path.suffix.lower() in {".json", ".jsonl"}
        or path_text.startswith("data/")
    )


def should_scan_topology(path: Path) -> bool:
    path_text = str(path)
    return path.name.startswith(".env") or path_text.startswith("docs/")


def tracked_files() -> list[Path]:
    result = subprocess.run(["git", "ls-files"], check=True, capture_output=True, text=True)
    return [Path(line) for line in result.stdout.splitlines() if line]


def main() -> int:
    failures: list[str] = []
    for path in tracked_files():
        if not path.exists() or path.is_dir():
            continue
        if path.suffix.lower() not in {".md", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".example", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.pattern.startswith("\\b100") and not should_scan_topology(path):
                continue
            if not pattern.pattern.startswith("\\b100") and not should_scan_secrets(path):
                continue
            if pattern.search(text):
                failures.append(f"{path}: matched {pattern.pattern}")
        if str(path) not in ALLOWLISTED_FILES and any(marker in text for marker in RAW_DATA_MARKERS):
            failures.append(f"{path}: contains gitignored Yunting data path marker")
    if failures:
        print("Potential secret/raw-data leakage detected:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
