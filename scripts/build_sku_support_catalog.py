#!/usr/bin/env python3
"""Build the joined SKU catalog used by after-sales SKU matching."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

S_DEV_STATUS = "产品开发状态 1.未开发 2.开发中 3.开发完成 4.中止开发 5.暂停开发"
S_TASK_STATUS = "任务状态 0待审核，1审核中，2审核通过，3审核不通过，4待提交"
S_DELETED = "逻辑删除 false：没有删除，true: 删除"
S_KINGDEE_ID = "金蝶数据id"
S_UPDATED_AT = "修改时间"
S_CREATED_AT = "创建时间"
S_RECORD_ID = "主键id"
INVALID_SKU_CODES = {"#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!"}
PLACEHOLDER_VALUES = {"", "-", "/", "--", "暂无", "0", "product", "Product"}

OUTPUT_COLUMNS = [
    "sku_code",
    "spu",
    "sku_name_cn",
    "product_name_cn",
    "product_owner_name",
]

SOURCE_FIELD_MAP = {
    "sku_code": "ODS-ERP-PLM产品sku表.sku",
    "spu": "产品信息表.spu",
    "sku_name_cn": "ODS-ERP-PLM产品sku表.品名",
    "product_name_cn": "产品信息表.产品名",
    "product_owner_name": "产品信息表.产品负责人",
}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_optional(value: Any) -> str:
    normalized = clean(value)
    return "" if normalized in PLACEHOLDER_VALUES else normalized


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_timestamp(value: str) -> datetime:
    normalized = clean(value)
    if not normalized:
        return datetime.min
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return datetime.min


def parse_record_id(value: str) -> int:
    normalized = clean(value)
    return int(normalized) if normalized.isdigit() else 0


def sku_latest_key(row: dict[str, str]) -> tuple[datetime, datetime, int]:
    return (
        parse_timestamp(row.get(S_UPDATED_AT, "")),
        parse_timestamp(row.get(S_CREATED_AT, "")),
        parse_record_id(row.get(S_RECORD_ID, "")),
    )


def dedupe_latest_sku_rows(skus: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Keep only the latest source SKU row before joining product metadata."""
    rows_by_sku: dict[str, list[dict[str, str]]] = {}
    for row in skus:
        sku_code = clean(row.get("sku")).upper()
        if sku_code:
            rows_by_sku.setdefault(sku_code, []).append(row)

    latest_rows: list[dict[str, str]] = []
    duplicate_groups = 0
    removed_rows = 0
    for _, rows in rows_by_sku.items():
        if len(rows) > 1:
            duplicate_groups += 1
            removed_rows += len(rows) - 1
        latest_rows.append(max(rows, key=sku_latest_key))

    latest_rows.sort(key=lambda row: clean(row.get("sku")).upper())
    profile = {
        "sku_dedupe_rule": f"dedupe source SKU table before join by upper(sku); keep latest by {S_UPDATED_AT}, then {S_CREATED_AT}, then {S_RECORD_ID}",
        "sku_dedupe_duplicate_codes": duplicate_groups,
        "sku_dedupe_removed_rows": removed_rows,
        "sku_rows_after_source_dedupe": len(latest_rows),
    }
    return latest_rows, profile


def is_composite_sku(sku: str) -> bool:
    raw = clean(sku).upper()
    return bool(re.search(r"[+/,，、\s]", raw) or re.search(r"\*[A-Z0-9-]+|[A-Z0-9-]+\*", raw))


def is_invalid_sku(sku: str) -> bool:
    return clean(sku).upper() in INVALID_SKU_CODES


def build_catalog(product_path: Path, sku_path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    products = read_csv(product_path)
    raw_skus = read_csv(sku_path)
    non_composite_skus = [row for row in raw_skus if not is_composite_sku(clean(row["sku"]))]
    source_skus = [row for row in non_composite_skus if not is_invalid_sku(clean(row["sku"]))]
    skipped_composites = len(raw_skus) - len(non_composite_skus)
    skipped_invalid_skus = len(non_composite_skus) - len(source_skus)
    skus, dedupe_profile = dedupe_latest_sku_rows(source_skus)
    product_by_id = {clean(row["主键ID"]): row for row in products}
    joined: list[dict[str, str]] = []
    unmatched = 0

    for sku in skus:
        sku_code_raw = clean(sku["sku"])

        product = product_by_id.get(clean(sku["产品表id"]))
        if not product:
            unmatched += 1
            product = {}

        sku_code = sku_code_raw.upper()
        joined.append(
            {
                "sku_code": sku_code,
                "spu": clean_optional(product.get("spu")),
                "sku_name_cn": clean_optional(sku.get("品名")),
                "product_name_cn": clean_optional(product.get("产品名")),
                "product_owner_name": clean_optional(product.get("产品负责人")),
            }
        )

    sku_counts = Counter(row["sku_code"] for row in joined if row["sku_code"])
    profile = {
        "source_files": [str(product_path.relative_to(ROOT)), str(sku_path.relative_to(ROOT))],
        "product_rows": len(products),
        "sku_rows": len(raw_skus),
        "sku_rows_after_composite_filter": len(non_composite_skus),
        "skipped_invalid_sku_rows": skipped_invalid_skus,
        "sku_rows_after_valid_sku_filter": len(source_skus),
        "joined_rows": len(joined),
        "skipped_composite_sku_rows": skipped_composites,
        **dedupe_profile,
        "join_key": "ODS-ERP-PLM产品sku表.产品表id = 产品信息表.主键ID",
        "matched_rows": len(skus) - unmatched,
        "unmatched_rows": unmatched,
        "sku_unique_casefold": len(sku_counts),
        "duplicate_sku_codes": sum(1 for _, count in sku_counts.items() if count > 1),
        "top_duplicate_sku_codes": [(sku, count) for sku, count in sku_counts.most_common() if count > 1][:50],
        "columns": OUTPUT_COLUMNS,
        "source_field_map": SOURCE_FIELD_MAP,
    }
    return joined, profile


def duplicate_reasons(rows: list[dict[str, str]]) -> list[str]:
    reasons: list[str] = ["multiple_sku_records"]

    def unique(field: str) -> set[str]:
        return {row.get(field, "") for row in rows}

    deleted_values = unique(f"sku.{S_DELETED}")
    if len(unique("sku.产品表id")) > 1:
        reasons.append("same_sku_different_product_id")
    if len(deleted_values) > 1:
        reasons.append("mixed_deleted_status")
    elif deleted_values == {"1"}:
        reasons.append("all_deleted")
    if len(unique(S_DEV_STATUS if S_DEV_STATUS.startswith("sku.") else f"sku.{S_DEV_STATUS}")) > 1:
        reasons.append("different_dev_status")
    if len(unique(f"sku.{S_TASK_STATUS}")) > 1:
        reasons.append("different_approval_status")
    if len(unique("sku.品名")) > 1 or len(unique("product.产品名")) > 1:
        reasons.append("different_names")
    if len(unique(f"sku.{S_KINGDEE_ID}")) > 1:
        reasons.append("different_kingdee_id")
    if len(unique("product.spu")) > 1:
        reasons.append("different_spu")
    return reasons


def write_duplicate_reports(product_path: Path, sku_path: Path, report_dir: Path, date_tag: str) -> None:
    products = read_csv(product_path)
    skus = read_csv(sku_path)
    product_by_id = {clean(row["主键ID"]): row for row in products}
    raw_rows_by_sku: dict[str, list[dict[str, str]]] = {}

    for sku in skus:
        sku_code = clean(sku.get("sku")).upper()
        if not sku_code or is_composite_sku(sku_code) or is_invalid_sku(sku_code):
            continue
        raw_rows_by_sku.setdefault(sku_code, []).append(sku)

    rows_by_sku: dict[str, list[dict[str, str]]] = {}
    kept_by_sku: dict[str, dict[str, str]] = {}
    for sku_code, raw_rows in raw_rows_by_sku.items():
        kept_by_sku[sku_code] = max(raw_rows, key=sku_latest_key)
        rows: list[dict[str, str]] = []
        for sku in raw_rows:
            product = product_by_id.get(clean(sku.get("产品表id")), {})
            row = {f"sku.{key}": clean(value) for key, value in sku.items()}
            row.update({f"product.{key}": clean(value) for key, value in product.items()})
            rows.append(row)
        rows_by_sku[sku_code] = rows

    duplicate_groups = {sku: rows for sku, rows in rows_by_sku.items() if len(rows) > 1}
    field_diff_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    report_rows: list[dict[str, str]] = []

    for sku, rows in sorted(duplicate_groups.items(), key=lambda item: (-len(item[1]), item[0])):
        fields = sorted(set().union(*(row.keys() for row in rows)))
        differing_fields = [field for field in fields if len({row.get(field, "") for row in rows}) > 1]
        field_diff_counts.update(differing_fields)
        reasons = duplicate_reasons(rows)
        reason_counts.update(reasons)

        def values(field: str) -> str:
            return "|".join(sorted({row.get(field, "") for row in rows if row.get(field, "")}))

        kept = kept_by_sku[sku]
        kept_record_id = clean(kept.get(S_RECORD_ID))
        dropped_record_ids = "|".join(
            sorted(
                clean(row.get(f"sku.{S_RECORD_ID}", ""))
                for row in rows
                if clean(row.get(f"sku.{S_RECORD_ID}", "")) != kept_record_id
            )
        )
        report_rows.append(
            {
                "sku_code": sku,
                "row_count": str(len(rows)),
                "reasons": ";".join(reasons),
                "kept_record_id": kept_record_id,
                "kept_updated_at": clean(kept.get(S_UPDATED_AT)),
                "kept_created_at": clean(kept.get(S_CREATED_AT)),
                "dropped_record_ids": dropped_record_ids,
                "differing_fields": ";".join(differing_fields[:80]),
                "product_ids": values("sku.产品表id"),
                "sku_record_ids": values("sku.主键id"),
                "spu_values": values("product.spu"),
                "sku_names": values("sku.品名"),
                "product_names": values("product.产品名"),
                "deleted_values": values(f"sku.{S_DELETED}"),
                "dev_status_values": values(f"sku.{S_DEV_STATUS}"),
                "task_status_values": values(f"sku.{S_TASK_STATUS}"),
                "kingdee_ids": values(f"sku.{S_KINGDEE_ID}"),
                "owner_values": values("sku.产品负责人姓名"),
            }
        )

    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / f"duplicate_sku_diff_groups-{date_tag}.csv"
    json_path = report_dir / f"duplicate_sku_diff_summary-{date_tag}.json"
    fields = [
        "sku_code",
        "row_count",
        "reasons",
        "kept_record_id",
        "kept_updated_at",
        "kept_created_at",
        "dropped_record_ids",
        "differing_fields",
        "product_ids",
        "sku_record_ids",
        "spu_values",
        "sku_names",
        "product_names",
        "deleted_values",
        "dev_status_values",
        "task_status_values",
        "kingdee_ids",
        "owner_values",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report_rows)

    json_path.write_text(
        json.dumps(
            {
                "duplicate_sku_codes": len(duplicate_groups),
                "duplicate_rows_total": sum(len(rows) for rows in duplicate_groups.values()),
                "dedupe_removed_rows": sum(len(rows) - 1 for rows in duplicate_groups.values()),
                "dedupe_rule": f"before join, keep max row by {S_UPDATED_AT}, then {S_CREATED_AT}, then {S_RECORD_ID}",
                "reason_counts": reason_counts.most_common(),
                "field_diff_counts": field_diff_counts.most_common(),
                "reports": {
                    "groups_csv": str(csv_path.relative_to(ROOT)),
                    "summary_json": str(json_path.relative_to(ROOT)),
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def write_outputs(rows: list[dict[str, str]], profile: dict[str, Any], output_dir: Path, report_dir: Path, date_tag: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    joined_path = output_dir / f"sku_support_catalog-{date_tag}.csv"
    profile_path = report_dir / f"sku_support_catalog_profile-{date_tag}.json"
    sample_path = report_dir / f"sku_support_catalog_sample-{date_tag}.json"

    with joined_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    profile = {**profile, "output_file": str(joined_path.relative_to(ROOT))}
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    sample_path.write_text(json.dumps(rows[:20], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(profile, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build joined SKU catalog for support matching.")
    parser.add_argument("--raw-dir", default="data/sku_catalog/raw/2026-05-26")
    parser.add_argument("--date-tag", default="2026-05-26")
    parser.add_argument("--product-file", default="产品信息表-2026-05-26.csv")
    parser.add_argument("--sku-file", default="ODS-ERP-PLM产品sku表-2026-05-26.csv")
    parser.add_argument("--output-dir", default="data/sku_catalog/processed/2026-05-26")
    parser.add_argument("--report-dir", default="data/sku_catalog/reports/2026-05-26")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw_dir = ROOT / args.raw_dir
    product_path = raw_dir / args.product_file
    sku_path = raw_dir / args.sku_file
    rows, profile = build_catalog(product_path, sku_path)
    write_outputs(rows, profile, ROOT / args.output_dir, ROOT / args.report_dir, args.date_tag)
    write_duplicate_reports(product_path, sku_path, ROOT / args.report_dir, args.date_tag)


if __name__ == "__main__":
    main()
