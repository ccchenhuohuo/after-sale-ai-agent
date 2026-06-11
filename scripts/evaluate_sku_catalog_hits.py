#!/usr/bin/env python3
"""Evaluate exact SKU hit rate against ingested Feishu support messages."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_NAME = "产品质量问题 发错货 售后及其他产品反馈"
MEDIA_RE = re.compile(r"\[(?:Image|Media):[^\]]+\]|<video\b[^>]*>", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")
DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"(?:[A-Za-z]{1,8}\d[A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)"
    r"|(?:\d{4,10}(?:-[A-Za-z0-9]+)*)"
    r")(?![A-Za-z0-9])"
)
NOISE_TOKENS = {
    "WIFI",
    "USB",
    "IOS",
    "IPAD",
    "IPHONE",
    "ANDROID",
    "TYPEC",
    "MP4",
    "MOV",
    "JPG",
    "PNG",
    "WEBP",
}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_catalog(path: Path) -> tuple[dict[str, dict[str, str]], set[str]]:
    catalog: dict[str, dict[str, str]] = {}
    spus: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            sku_code = clean(row.get("sku_code")).upper()
            if sku_code:
                catalog[sku_code] = row
            spu = clean(row.get("spu")).upper()
            if spu:
                spus.add(spu)
    return catalog, spus


def load_messages(path: Path, source_name: str) -> list[dict[str, Any]]:
    messages = json.loads(path.read_text(encoding="utf-8"))
    return [message for message in messages if message.get("source_name") == source_name and not message.get("deleted")]


def normalize_text(text: str) -> str:
    text = MEDIA_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    return text


def candidate_tokens(text: str) -> list[str]:
    normalized = normalize_text(text)
    tokens: list[str] = []
    seen: set[str] = set()
    for match in TOKEN_RE.finditer(normalized):
        token = match.group(1).strip("-").upper()
        if not token or token in NOISE_TOKENS:
            continue
        if len(token) < 3 or DATE_RE.match(token):
            continue
        if token.count("-") >= 2 and len(token) > 18:
            continue
        if token.isdigit() and token.startswith("20"):
            continue
        if token not in seen:
            tokens.append(token)
            seen.add(token)
    return tokens


def parent_candidates(token: str) -> list[str]:
    parts = token.split("-")
    candidates = []
    while len(parts) > 1:
        parts.pop()
        candidates.append("-".join(parts))
    return candidates


def evaluate(catalog_path: Path, messages_path: Path, source_name: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    catalog, spus = load_catalog(catalog_path)
    messages = load_messages(messages_path, source_name)

    rows: list[dict[str, str]] = []
    exact_pair_hits = 0
    parent_pair_hits = 0
    sku_or_spu_pair_hits = 0
    messages_with_candidates = 0
    messages_with_exact_hit = 0
    messages_with_sku_or_spu_hit = 0
    messages_with_parent_hit = 0
    candidate_counter: Counter[str] = Counter()
    exact_counter: Counter[str] = Counter()
    unmatched_counter: Counter[str] = Counter()

    for message in messages:
        tokens = candidate_tokens(str(message.get("content") or ""))
        if not tokens:
            continue
        messages_with_candidates += 1
        exact_hits = [token for token in tokens if token in catalog]
        spu_hits = [token for token in tokens if token not in catalog and token in spus]
        parent_hits = {
            parent
            for token in tokens
            if token not in catalog and token not in spus
            for parent in parent_candidates(token)
            if parent in catalog
        }

        candidate_counter.update(tokens)
        exact_counter.update(exact_hits)
        unmatched_counter.update(token for token in tokens if token not in catalog and token not in spus)
        exact_pair_hits += len(exact_hits)
        sku_or_spu_pair_hits += len(exact_hits) + len(spu_hits)
        parent_pair_hits += len(parent_hits)
        if exact_hits:
            messages_with_exact_hit += 1
        if exact_hits or spu_hits:
            messages_with_sku_or_spu_hit += 1
        if exact_hits or spu_hits or parent_hits:
            messages_with_parent_hit += 1

        rows.append(
            {
                "message_id": clean(message.get("message_id")),
                "create_time": clean(message.get("create_time")),
                "sender_name": clean((message.get("sender") or {}).get("name")),
                "candidate_skus": "|".join(tokens),
                "exact_hit_skus": "|".join(exact_hits),
                "exact_hit_spus": "|".join(spu_hits),
                "parent_fallback_hit_skus": "|".join(sorted(parent_hits)),
                "message_app_link": clean(message.get("message_app_link")),
                "content_excerpt": clean(message.get("content"))[:500],
            }
        )

    candidate_pairs = sum(candidate_counter.values())
    unique_candidates = set(candidate_counter)
    unique_exact_hits = set(exact_counter)
    summary = {
        "catalog_file": str(catalog_path.relative_to(ROOT)),
        "messages_file": str(messages_path.relative_to(ROOT)),
        "source_name": source_name,
        "catalog_sku_count": len(catalog),
        "catalog_spu_count": len(spus),
        "support_group_messages": len(messages),
        "messages_with_sku_candidates": messages_with_candidates,
        "candidate_message_sku_pairs": candidate_pairs,
        "unique_candidate_skus": len(unique_candidates),
        "exact_hit_message_sku_pairs": exact_pair_hits,
        "exact_pair_hit_rate": round(exact_pair_hits / candidate_pairs, 4) if candidate_pairs else 0,
        "messages_with_exact_hit": messages_with_exact_hit,
        "message_exact_hit_rate": round(messages_with_exact_hit / messages_with_candidates, 4)
        if messages_with_candidates
        else 0,
        "sku_or_spu_hit_message_sku_pairs": sku_or_spu_pair_hits,
        "sku_or_spu_pair_hit_rate": round(sku_or_spu_pair_hits / candidate_pairs, 4) if candidate_pairs else 0,
        "messages_with_sku_or_spu_hit": messages_with_sku_or_spu_hit,
        "message_sku_or_spu_hit_rate": round(messages_with_sku_or_spu_hit / messages_with_candidates, 4)
        if messages_with_candidates
        else 0,
        "unique_exact_hit_skus": len(unique_exact_hits),
        "unique_exact_hit_rate": round(len(unique_exact_hits) / len(unique_candidates), 4) if unique_candidates else 0,
        "parent_fallback_hit_message_sku_pairs": parent_pair_hits,
        "messages_with_exact_or_parent_fallback_hit": messages_with_parent_hit,
        "message_exact_or_parent_fallback_hit_rate": round(messages_with_parent_hit / messages_with_candidates, 4)
        if messages_with_candidates
        else 0,
        "top_exact_hit_skus": exact_counter.most_common(50),
        "top_unmatched_candidate_skus": unmatched_counter.most_common(80),
    }
    return summary, rows


def write_outputs(summary: dict[str, Any], rows: list[dict[str, str]], report_dir: Path, date_tag: str) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    summary_path = report_dir / f"sku_catalog_hit_eval-{date_tag}.json"
    rows_path = report_dir / f"sku_catalog_hit_eval_messages-{date_tag}.csv"
    summary["reports"] = {
        "summary_json": str(summary_path.relative_to(ROOT)),
        "message_csv": str(rows_path.relative_to(ROOT)),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "message_id",
        "create_time",
        "sender_name",
        "candidate_skus",
        "exact_hit_skus",
        "exact_hit_spus",
        "parent_fallback_hit_skus",
        "message_app_link",
        "content_excerpt",
    ]
    with rows_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate SKU catalog exact hits against Feishu support messages.")
    parser.add_argument("--catalog-file", default="data/sku_catalog/processed/2026-05-26/sku_support_catalog-2026-05-26.csv")
    parser.add_argument("--messages-file", default="data/feishu_ingest/20260526-014329/messages.cleaned.json")
    parser.add_argument("--source-name", default=DEFAULT_SOURCE_NAME)
    parser.add_argument("--report-dir", default="data/sku_catalog/reports/2026-05-26")
    parser.add_argument("--date-tag", default="2026-05-26")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary, rows = evaluate(ROOT / args.catalog_file, ROOT / args.messages_file, args.source_name)
    write_outputs(summary, rows, ROOT / args.report_dir, args.date_tag)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
