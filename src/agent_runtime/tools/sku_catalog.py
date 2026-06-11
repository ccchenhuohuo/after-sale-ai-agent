import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from agents import function_tool

from agent_runtime.settings import get_settings


ROOT = Path(__file__).resolve().parents[3]


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _tokens(value: str) -> list[str]:
    normalized = _clean(value).upper()
    if not normalized:
        return []
    pieces = re.split(r"[+/,，、\s]+", normalized)
    output: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        output.append(piece)
        if "*" in piece:
            output.append(piece.split("*", 1)[0])
    output.append(normalized)
    return list(dict.fromkeys(output))


@lru_cache(maxsize=4)
def _load_catalog(path_value: str) -> list[dict[str, str]]:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _score_row(row: Dict[str, str], query: str) -> tuple[int, list[str]]:
    query_upper = query.upper()
    query_tokens = set(_tokens(query))
    score = 0
    reasons: list[str] = []

    sku = _clean(row.get("sku_code")).upper()
    spu = _clean(row.get("spu")).upper()
    fields = {
        "sku_name_cn": _clean(row.get("sku_name_cn")),
        "product_name_cn": _clean(row.get("product_name_cn")),
        "product_owner_name": _clean(row.get("product_owner_name")),
    }

    if sku and sku == query_upper:
        score += 100
        reasons.append("sku精确匹配")
    elif sku and sku in query_tokens:
        score += 85
        reasons.append("sku token匹配")
    elif sku and sku in query_upper:
        score += 70
        reasons.append("sku包含匹配")

    if spu and spu == query_upper:
        score += 70
        reasons.append("spu精确匹配")
    elif spu and spu in query_tokens:
        score += 55
        reasons.append("spu token匹配")
    elif spu and spu in query_upper:
        score += 40
        reasons.append("spu包含匹配")

    for field_name, value in fields.items():
        if value and value in query:
            score += 30
            reasons.append(f"{field_name}包含匹配")
        elif value and query and query in value:
            score += 20
            reasons.append(f"query命中{field_name}")

    return score, reasons


def search_sku_catalog_text(query: str, limit: int = 5) -> str:
    settings = get_settings()
    rows = _load_catalog(settings.sku_catalog_path)
    if not rows:
        return f"SKU目录未找到或为空：{settings.sku_catalog_path}"

    scored: List[tuple[int, list[str], dict[str, str]]] = []
    for row in rows:
        score, reasons = _score_row(row, query)
        if score > 0:
            scored.append((score, reasons, row))
    scored.sort(key=lambda item: (-item[0], item[2].get("sku_code", "")))

    if not scored:
        return "未在SKU目录中命中。请向客服追问客户截图、订单SKU、包装SKU或产品铭牌信息。"

    lines = []
    for score, reasons, row in scored[: max(1, min(limit, 10))]:
        lines.append(
            "\n".join(
                [
                    f"- SKU：{row.get('sku_code', '')}",
                    f"  SPU：{row.get('spu', '')}",
                    f"  SKU品名：{row.get('sku_name_cn', '')}",
                    f"  产品名：{row.get('product_name_cn', '')}",
                    f"  产品负责人：{row.get('product_owner_name', '')}",
                    f"  命中分：{score}",
                    f"  命中原因：{'、'.join(reasons)}",
                ]
            )
        )
    return "\n".join(lines)


@function_tool
def search_sku_catalog(query: str, limit: int = 5) -> str:
    """Search the joined company SKU catalog for after-sales SKU resolution."""
    return search_sku_catalog_text(query, limit)
