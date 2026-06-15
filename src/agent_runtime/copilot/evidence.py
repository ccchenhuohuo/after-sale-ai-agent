from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal


EvidenceLevel = Literal[
    "identity_only",
    "formal",
    "reviewed_case",
    "unreviewed_history",
    "unreviewed_media",
    "empty",
    "error",
]
EvidenceStatus = Literal["hit", "empty", "error"]


def short_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class SkuEvidence:
    status: EvidenceStatus
    evidence_level: EvidenceLevel
    verified: bool
    query_hash: str
    sku: str = ""
    spu: str = ""
    sku_name_cn: str = ""
    product_name_cn: str = ""
    product_owner_name: str = ""
    score: float = 0.0
    matched_reasons: list[str] = field(default_factory=list)
    message: str = ""
    source_type: str = "sku_catalog"
    reference_url: str = ""


@dataclass(frozen=True)
class OfficialKbEvidence:
    status: EvidenceStatus
    evidence_level: EvidenceLevel
    verified: bool
    query_hash: str
    title: str = ""
    section: str = ""
    reference_url: str = ""
    snippet: str = ""
    score: float = 0.0
    matched_reasons: list[str] = field(default_factory=list)
    message: str = ""
    source_type: str = "official_kb"


@dataclass(frozen=True)
class HistoryEvidence:
    status: EvidenceStatus
    evidence_level: EvidenceLevel
    verified: bool
    query_hash: str
    topic_id: str = ""
    sku: str = ""
    summary: str = ""
    solution_type: str = ""
    reference_url: str = ""
    score: float = 0.0
    matched_reasons: list[str] = field(default_factory=list)
    message: str = ""
    source_type: str = "history_rag"


@dataclass(frozen=True)
class MediaEvidence:
    status: EvidenceStatus
    evidence_level: EvidenceLevel
    verified: bool
    query_hash: str
    topic_id: str = ""
    sku: str = ""
    media_type: str = ""
    media_id: str = ""
    summary: str = ""
    reference_url: str = ""
    score: float = 0.0
    matched_reasons: list[str] = field(default_factory=list)
    message: str = ""
    source_type: str = "media_rag"


@dataclass(frozen=True)
class SupportEvidencePack:
    raw_issue_hash: str
    query_chars: int
    issue_type: str
    product_model: str
    sku: list[SkuEvidence]
    official: list[OfficialKbEvidence]
    history: list[HistoryEvidence]
    media: list[MediaEvidence]

    @property
    def sku_hit_count(self) -> int:
        return _hit_count(self.sku)

    @property
    def official_hit_count(self) -> int:
        return _hit_count(self.official)

    @property
    def history_hit_count(self) -> int:
        return _hit_count(self.history)

    @property
    def media_hit_count(self) -> int:
        return _hit_count(self.media)

    @property
    def has_formal_evidence(self) -> bool:
        return any(item.evidence_level == "formal" and item.status == "hit" for item in self.official)

    @property
    def has_reviewed_history(self) -> bool:
        return any(item.evidence_level == "reviewed_case" and item.status == "hit" for item in self.history)


def _hit_count(items: list[object]) -> int:
    return sum(1 for item in items if getattr(item, "status", "") == "hit")


def render_sku_evidence(items: list[SkuEvidence]) -> str:
    if not items:
        return "未在SKU目录中命中。请向客服追问客户截图、订单SKU、包装SKU或产品铭牌信息。"

    if all(item.status != "hit" for item in items):
        return items[0].message or "未在SKU目录中命中。请向客服追问客户截图、订单SKU、包装SKU或产品铭牌信息。"

    lines: list[str] = []
    for item in items:
        if item.status != "hit":
            continue
        score = float(item.score)
        score_text = int(score) if score.is_integer() else score
        lines.append(
            "\n".join(
                [
                    f"- SKU：{item.sku}",
                    f"  SPU：{item.spu}",
                    f"  SKU品名：{item.sku_name_cn}",
                    f"  产品名：{item.product_name_cn}",
                    f"  产品负责人：{item.product_owner_name}",
                    f"  命中分：{score_text}",
                    f"  命中原因：{'、'.join(item.matched_reasons)}",
                ]
            )
        )
    return "\n".join(lines)


def render_official_evidence(items: list[OfficialKbEvidence]) -> str:
    if not items:
        return "未查询到可信正式依据：正式知识库/RAG 索引尚未接入当前终端测试运行。不要编造文档名称、章节、链接、政策或技术结论。"
    if all(item.status != "hit" for item in items):
        return items[0].message
    return "\n".join(
        f"- {item.title or '正式依据'} / {item.section or '未标注章节'} / {item.reference_url or '无链接'}"
        for item in items
        if item.status == "hit"
    )


def render_history_evidence(items: list[HistoryEvidence]) -> str:
    if not items:
        return "未查询到可信历史参考：没有命中相似话题。"
    if all(item.status != "hit" for item in items):
        return items[0].message
    return "\n".join(item.message for item in items if item.status == "hit")


def render_media_evidence(items: list[MediaEvidence]) -> str:
    if not items:
        return "未查询到可信媒体观察证据：没有命中相似媒体记录。"
    if all(item.status != "hit" for item in items):
        return items[0].message
    return "\n".join(item.message for item in items if item.status == "hit")


def render_evidence_pack(pack: SupportEvidencePack) -> str:
    return "\n".join(
        [
            "结构化证据包",
            f"- 问题哈希：{pack.raw_issue_hash}",
            f"- 问题类型初判：{pack.issue_type}",
            f"- 产品型号/SKU候选：{pack.product_model or '未识别'}",
            "",
            "SKU 识别证据：",
            render_sku_evidence(pack.sku),
            "",
            "正式依据：",
            render_official_evidence(pack.official),
            "",
            "历史参考：",
            render_history_evidence(pack.history),
            "",
            "媒体观察证据：",
            render_media_evidence(pack.media),
            "",
            "证据边界：",
            "- SKU 识别只用于产品身份、SPU、品名和负责人流转，不能作为故障原因或售后政策依据。",
            "- 正式依据只有 evidence_level=formal 且 verified=true 时，才能作为技术结论或政策依据。",
            "- 已审核群聊历史 FAQ 是可靠售后参考，可用于客服排查与历史处理经验；它不是正式政策源，不能覆盖正式 KB/MRD/SOP。",
            "- 未审核媒体观察证据必须标注需人工确认，不能作为正式依据。",
            "- 退款、赔偿、换新、补发、维修时效或最终判责，必须等待正式依据或人工复核。",
        ]
    )


def evidence_pack_trace_attributes(pack: SupportEvidencePack) -> dict[str, object]:
    return {
        "raw_issue_hash": pack.raw_issue_hash,
        "query_chars": pack.query_chars,
        "issue_type": pack.issue_type,
        "product_model_hash": short_hash(pack.product_model) if pack.product_model else "",
        "sku_hit_count": pack.sku_hit_count,
        "official_hit_count": pack.official_hit_count,
        "history_hit_count": pack.history_hit_count,
        "media_hit_count": pack.media_hit_count,
        "has_formal_evidence": pack.has_formal_evidence,
        "has_reviewed_history": pack.has_reviewed_history,
    }
