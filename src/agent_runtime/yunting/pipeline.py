from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runtime.yunting.common import (
    AUTHORITY_LEVEL,
    AUTHORITY_SCORE,
    MEDIA_COLLECTION,
    REFERENCE_CLASS,
    SOURCE_SYSTEM,
    SOURCE_TYPE,
    TEXT_COLLECTION,
    boolish,
    clean_text,
    compact_json,
    first_present,
    json_array,
    normalize_time,
    now_ts,
    sha256_text,
    source_url_from_content,
    stable_id,
    stable_uuid,
    stat_date_from,
    stat_week_from,
)
from agent_runtime.yunting.tables import DORIS_TABLES


LayerRows = dict[str, list[dict[str, Any]]]

GREETING_REJECTS = {
    "您好",
    "你好",
    "亲",
    "在的",
    "稍等",
    "请稍等",
    "感谢您的咨询",
    "欢迎光临",
}

CUSTOMER_WATER_TEXTS = {
    "好",
    "好的",
    "可以",
    "嗯",
    "恩",
    "哦",
    "是",
    "是的",
    "不是",
    "对",
    "不对",
    "没问题",
    "谢谢",
    "感谢",
    "收到",
    "ok",
    "OK",
}

CUSTOMER_QUESTION_MARKERS = (
    "?",
    "？",
    "怎么",
    "如何",
    "多久",
    "多少",
    "能否",
    "怎么办",
    "为什么",
    "是否",
    "啥",
    "吗",
    "可不可以",
    "能不能",
)

CUSTOMER_PROBLEM_MARKERS = (
    "充不",
    "不充",
    "开不了",
    "打不开",
    "不能",
    "无法",
    "没法",
    "不能开",
    "不能用",
    "不行",
    "用不了",
    "连不上",
    "连接不上",
    "不匹配",
    "不支持",
    "配对不上",
    "没反应",
    "没声音",
    "不亮",
    "不响",
    "不工作",
    "失灵",
    "异常",
    "故障",
    "坏",
    "损坏",
    "破损",
    "裂",
    "少件",
    "少发",
    "缺件",
    "缺了",
    "发错",
    "错发",
    "漏发",
    "收不到",
    "没收到",
    "断了",
    "卡住",
    "黑屏",
    "花屏",
    "闪退",
    "报错",
    "续航短",
    "耗电快",
    "发热",
    "显示不了",
    "识别不了",
)

CUSTOMER_REQUEST_MARKERS = (
    "退货",
    "退款",
    "换货",
    "换一个",
    "维修",
    "返修",
    "保修",
    "补发",
    "重发",
    "寄回",
    "发票",
    "安装不了",
    "设置不了",
    "校准",
)

NON_ANSWER_PATTERNS = (
    "转人工",
    "人工客服",
    "无法回答",
    "不能回答",
    "不清楚",
    "不知道",
    "没有答案",
    "仅供参考",
    "请提供订单",
    "请提供订单号",
    "麻烦提供订单",
    "麻烦您提供订单",
)

ROLE_ALIASES = {
    "CUSTOMER": "CUSTOMER",
    "USER": "CUSTOMER",
    "BUYER": "CUSTOMER",
    "CLIENT": "CUSTOMER",
    "客户": "CUSTOMER",
    "用户": "CUSTOMER",
    "买家": "CUSTOMER",
    "SERVER": "SERVER",
    "SERVICE": "SERVER",
    "AGENT": "SERVER",
    "SELLER": "SERVER",
    "STAFF": "SERVER",
    "客服": "SERVER",
    "商家": "SERVER",
    "卖家": "SERVER",
}


@dataclass(frozen=True)
class PipelineManifest:
    run_id: str
    raw_session_count: int
    std_message_count: int
    media_asset_count: int
    faq_case_count: int
    faq_chunk_count: int
    table_row_counts: dict[str, int]
    missing_unique_count: int = 0
    duplicate_page_token_count: int = 0
    empty_page_guard_triggered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "raw_session_count": self.raw_session_count,
            "std_message_count": self.std_message_count,
            "media_asset_count": self.media_asset_count,
            "faq_case_count": self.faq_case_count,
            "faq_chunk_count": self.faq_chunk_count,
            "table_row_counts": self.table_row_counts,
            "missing_unique_count": self.missing_unique_count,
            "duplicate_page_token_count": self.duplicate_page_token_count,
            "empty_page_guard_triggered": self.empty_page_guard_triggered,
        }


def empty_layers() -> LayerRows:
    return {name: [] for name in DORIS_TABLES}


def extract_sessions(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("data"), list):
        return [item for item in result["data"] if isinstance(item, dict)]
    if isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)]
    if payload.get("unique") or payload.get("unique_id"):
        return [payload]
    return []


def load_raw_sessions(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return extract_sessions(payload)


def load_raw_sessions_from_dir(path: Path) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for candidate in sorted(path.rglob("*.json")):
        sessions.extend(load_raw_sessions(candidate))
    seen: dict[str, dict[str, Any]] = {}
    for session in sessions:
        unique_id = session_unique_id(session)
        if unique_id:
            seen[unique_id] = session
    return list(seen.values())


def session_unique_id(session: dict[str, Any]) -> str:
    return clean_text(first_present(session, "unique", "unique_id", "id"))


def session_contents(session: dict[str, Any]) -> list[dict[str, Any]]:
    contents = first_present(session, "contents", "contentList", default=[])
    contents = _parse_json_value(contents, [])
    if isinstance(contents, list):
        messages = [item for item in contents if isinstance(item, dict)]
        return sorted(messages, key=_message_sort_key)
    return []


def _parse_json_value(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text and text[0] in "[{":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return default
    return value


def _message_sort_key(message: dict[str, Any]) -> tuple[str, str]:
    publish_time = normalize_time(first_present(message, "publishTime", "publish_time", default=""))
    content_id = clean_text(first_present(message, "contentId", "content_id", "id"))
    return publish_time or "", content_id


def _normalize_role(value: Any) -> str:
    raw = clean_text(value)
    upper = raw.upper()
    return ROLE_ALIASES.get(upper) or ROLE_ALIASES.get(raw) or upper


def _normalize_message_type(value: Any) -> str:
    raw = clean_text(value).upper()
    if raw in {"IMAGE", "IMG", "PICTURE", "PIC", "图片"}:
        return "IMAGE"
    if raw in {"VIDEO", "VID", "视频"}:
        return "VIDEO"
    if raw in {"TEXT", "TXT", "文本", "文字", ""}:
        return "TEXT"
    return raw


def topic_values(session: dict[str, Any]) -> dict[str, list[str]]:
    topics: dict[str, list[str]] = defaultdict(list)
    topic_configs = _parse_json_value(first_present(session, "topicConfigs", "topic_configs", default=[]), [])
    for topic in topic_configs or []:
        if not isinstance(topic, dict):
            continue
        name = clean_text(first_present(topic, "topicName", "topic_name", "name"))
        values = first_present(topic, "topicValue", "topic_value", "values", default=[])
        if not isinstance(values, list):
            values = [values]
        for value in values:
            cleaned = clean_text(value)
            if name and cleaned:
                topics[name].append(cleaned)
    return dict(topics)


def tag_names(session: dict[str, Any]) -> list[str]:
    names: list[str] = []
    tag_list = _parse_json_value(first_present(session, "tagList", "tag_list", default=[]), [])
    for tag in tag_list or []:
        if isinstance(tag, dict):
            names.append(clean_text(first_present(tag, "tagName", "tag_name", "name", "tag")))
        else:
            names.append(clean_text(tag))
    return [name for name in names if name]


def _message_row(session: dict[str, Any], message: dict[str, Any], index: int, run_ts: str) -> dict[str, Any]:
    unique_id = session_unique_id(session)
    raw_content_id = clean_text(first_present(message, "contentId", "content_id", "id"))
    publish_time = normalize_time(first_present(message, "publishTime", "publish_time", default=first_present(session, "publishTime", "publish_time")))
    message_type = _normalize_message_type(first_present(message, "messageType", "message_type", "type"))
    role = _normalize_role(first_present(message, "role", "senderRole", default=""))
    content = clean_text(first_present(message, "content", "text", "contentText"))
    fallback_content_id = stable_id(unique_id, publish_time or "", role, message_type, content)
    content_id = raw_content_id or fallback_content_id
    message_pk = (
        stable_id("message", unique_id, content_id)
        if raw_content_id
        else stable_id("message", unique_id, publish_time or "", role, message_type, content)
    )
    return {
        "message_pk": message_pk,
        "content_id": content_id,
        "unique_id": unique_id,
        "message_index": index,
        "publish_time": publish_time,
        "role": role,
        "message_type": message_type,
        "user_name": clean_text(first_present(message, "userName", "user_name")),
        "content_text": content,
        "product_title_list_json": json_array(first_present(message, "productTitleList", "product_title_list", default=[])),
        "raw_json": compact_json(message),
        "source_system": SOURCE_SYSTEM,
        "create_time": run_ts,
        "update_time": run_ts,
        "dt": stat_date_from(publish_time),
    }


def _media_asset_row(message_row: dict[str, Any], run_ts: str) -> dict[str, Any] | None:
    if message_row["message_type"] not in {"IMAGE", "VIDEO"}:
        return None
    source_url = source_url_from_content(message_row["content_text"])
    asset_id = stable_id(message_row["unique_id"], message_row["content_id"], message_row["message_type"], source_url)
    ext = "mp4" if message_row["message_type"] == "VIDEO" else "jpg"
    return {
        "asset_id": asset_id,
        "unique_id": message_row["unique_id"],
        "content_id": message_row["content_id"],
        "message_type": message_row["message_type"],
        "role": message_row["role"],
        "source_url": source_url,
        "content_type": "",
        "file_sha256": "",
        "file_size": 0,
        "download_status": "not_downloaded",
        "media_object_key": f"media/sha256/pending/{asset_id}.{ext}" if source_url else "",
        "error_message": "" if source_url else "missing_source_url",
        "source_system": SOURCE_SYSTEM,
        "create_time": run_ts,
        "update_time": run_ts,
        "dt": message_row["dt"],
    }


def _is_substantial_answer(text: str) -> bool:
    stripped = clean_text(text)
    if len(stripped) < 6:
        return False
    if stripped in GREETING_REJECTS:
        return False
    if any(stripped.startswith(prefix) and len(stripped) < 12 for prefix in GREETING_REJECTS):
        return False
    if any(pattern in stripped and len(stripped) < 28 for pattern in NON_ANSWER_PATTERNS):
        return False
    return True


def _is_customer_question(text: str) -> bool:
    stripped = clean_text(text)
    if len(stripped) < 4:
        return False
    if _is_customer_water_text(stripped):
        return False
    return any(marker in stripped for marker in CUSTOMER_QUESTION_MARKERS)


def _is_customer_water_text(text: str) -> bool:
    stripped = clean_text(text)
    if len(stripped) < 2:
        return True
    return stripped in CUSTOMER_WATER_TEXTS


def _is_customer_support_request(text: str) -> bool:
    stripped = clean_text(text)
    if len(stripped) < 4:
        return False
    if _is_customer_water_text(stripped):
        return False
    if _is_customer_question(stripped):
        return True
    return any(marker in stripped for marker in CUSTOMER_PROBLEM_MARKERS + CUSTOMER_REQUEST_MARKERS)


def _customer_context_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        if message.get("message_type") in {"IMAGE", "VIDEO"}:
            parts.append(f"客户发送{message['message_type']}媒体资料")
            continue
        text = clean_text(message.get("content_text", ""))
        if text:
            parts.append(text)
    return " / ".join(parts)


def _is_evidence_customer_message(message: dict[str, Any]) -> bool:
    if message["role"] != "CUSTOMER":
        return False
    if message["message_type"] in {"IMAGE", "VIDEO"}:
        return True
    return message["message_type"] == "TEXT" and _is_customer_support_request(message["content_text"])


def _is_rag_message(message: dict[str, Any]) -> bool:
    if message["role"] == "CUSTOMER":
        return _is_evidence_customer_message(message)
    if message["role"] == "SERVER" and message["message_type"] == "TEXT":
        return _is_substantial_answer(message["content_text"])
    return False


def _answer_unit_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    customer_context: list[dict[str, Any]] = []
    context_answered = False
    for message in messages:
        if _is_evidence_customer_message(message):
            if context_answered:
                customer_context = []
                context_answered = False
            customer_context.append(message)
            customer_context = customer_context[-3:]
            continue
        if (
            message["role"] == "SERVER"
            and message["message_type"] == "TEXT"
            and _is_substantial_answer(message["content_text"])
        ):
            if not customer_context:
                continue
            question = _customer_context_text(customer_context)
            if not question:
                continue
            pairs.append(
                {
                    "question": question,
                    "answer": message["content_text"],
                    "source_content_ids": [context_message["content_id"] for context_message in customer_context]
                    + [message["content_id"]],
                }
            )
            context_answered = True
    return pairs


def _authority_fields() -> dict[str, Any]:
    return {
        "source_type": SOURCE_TYPE,
        "reference_class": REFERENCE_CLASS,
        "authority_level": AUTHORITY_LEVEL,
        "authority_score": AUTHORITY_SCORE,
        "can_be_reference": True,
    }


def _build_faq_rows(
    session: dict[str, Any],
    std_session: dict[str, Any],
    messages: list[dict[str, Any]],
    media_assets: list[dict[str, Any]],
    run_ts: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if boolish(std_session["is_default"]) or not messages:
        return [], [], []

    unique_id = std_session["unique_id"]
    stat_date = stat_date_from(std_session["session_start_time"] or messages[0].get("publish_time"))
    stat_week = stat_week_from(std_session["session_start_time"] or messages[0].get("publish_time"))
    topics = topic_values(session)
    tags = tag_names(session)
    answer_pairs = _answer_unit_pairs(messages)
    if not answer_pairs:
        return [], [], []
    answer_source_content_ids = {
        content_id
        for pair in answer_pairs
        for content_id in pair["source_content_ids"]
    }
    paired_customer_content_ids = {
        message["content_id"]
        for message in messages
        if message["role"] == "CUSTOMER"
        if message["content_id"] in answer_source_content_ids
    }
    evidence_customer_messages = [
        message
        for message in messages
        if message["content_id"] in paired_customer_content_ids and _is_evidence_customer_message(message)
    ]
    customer_question_summary = _customer_context_text(evidence_customer_messages)
    server_texts = [pair["answer"] for pair in answer_pairs]

    case_id = stable_id("case", unique_id)
    title = clean_text(first_present(session, "title", default="")) or (customer_question_summary[:80] if customer_question_summary else unique_id)
    authority = _authority_fields()
    case = {
        "case_id": case_id,
        "unique_id": unique_id,
        "case_title": title,
        "customer_question_summary": customer_question_summary,
        "answer_summary": " / ".join(server_texts[:3]),
        "symptom_summary": customer_question_summary,
        "resolution_summary": server_texts[-1],
        "product_summary": " / ".join(topics.get("品名", []) + topics.get("型号", []) + topics.get("SKU", [])),
        "brand_json": json_array(topics.get("品牌", [])),
        "sku_json": json_array(topics.get("SKU", [])),
        "spu_json": json_array(topics.get("SPU", [])),
        "product_name_json": json_array(topics.get("品名", [])),
        "tags_json": json_array(tags),
        "topic_values_json": compact_json(topics),
        "evidence_level": "history_service_case",
        **authority,
        "quality_status": "candidate",
        "source_name": std_session["source_name"],
        "shop_name": std_session["shop_name"],
        "session_type": std_session["session_type"],
        "stat_date": stat_date,
        "stat_week": stat_week,
        "create_time": run_ts,
        "update_time": run_ts,
    }

    chunks: list[dict[str, Any]] = []

    def add_chunk(chunk_type: str, text: str, question: str = "", answer: str = "", ids: list[str] | None = None) -> None:
        chunk_id = stable_id("chunk", case_id, chunk_type, len(chunks), text)
        chunks.append(
            {
                "chunk_id": chunk_id,
                "case_id": case_id,
                "unique_id": unique_id,
                "chunk_type": chunk_type,
                "chunk_text": text,
                "question": question,
                "answer": answer,
                "source_content_ids_json": json_array(ids or []),
                "linked_asset_ids_json": json_array([asset["asset_id"] for asset in media_assets]),
                "embedding_text_hash": sha256_text(text),
                "quality_status": "candidate",
                **authority,
                "stat_date": stat_date,
                "stat_week": stat_week,
                "create_time": run_ts,
                "update_time": run_ts,
            }
        )

    rag_messages = [
        message
        for message in messages
        if _is_rag_message(message)
    ]
    timeline = "\n".join(
        f"{m['message_index']}. {m['publish_time']} {m['role']} {m['message_type']}: {m['content_text']}"
        for m in rag_messages
        if m["content_text"]
    )
    add_chunk("case_overview", f"问题：{case['customer_question_summary']}\n客服回答：{case['answer_summary']}", ids=[m["content_id"] for m in rag_messages[:8]])
    add_chunk("conversation_timeline", timeline, ids=[m["content_id"] for m in rag_messages])
    add_chunk("conversation_window", "\n".join(timeline.splitlines()[:12]), ids=[m["content_id"] for m in rag_messages[:12]])

    for pair in answer_pairs:
        text = f"客户问题：{pair['question']}\n客服回答：{pair['answer']}"
        add_chunk("answer_unit", text, question=pair["question"], answer=pair["answer"], ids=pair["source_content_ids"])

    media_observations = [
        {
            "media_chunk_id": stable_id("media_chunk", asset["asset_id"]),
            "asset_id": asset["asset_id"],
            "unique_id": unique_id,
            "content_id": asset["content_id"],
            "message_type": asset["message_type"],
            "ocr_text": "",
            "visual_summary": "媒体已识别但尚未下载解析，等待服务器多模态处理。",
            "video_summary": "视频已识别但尚未抽帧解析，等待服务器多模态处理。" if asset["message_type"] == "VIDEO" else "",
            "keyframe_refs_json": json_array([]),
            "evidence_level": "media_observation_unreviewed",
            **authority,
            "stat_date": stat_date,
            "stat_week": stat_week,
            "create_time": run_ts,
            "update_time": run_ts,
        }
        for asset in media_assets
    ]
    return [case], chunks, media_observations


def build_yunting_layers(
    sessions: list[dict[str, Any]],
    *,
    run_id: str,
    raw_file_path: str = "",
    page_payloads: list[dict[str, Any]] | None = None,
    text_collection: str = TEXT_COLLECTION,
    media_collection: str = MEDIA_COLLECTION,
) -> tuple[LayerRows, PipelineManifest]:
    run_ts = now_ts()
    layers = empty_layers()
    page_payloads = page_payloads or []
    missing_unique_count = sum(1 for session in sessions if not session_unique_id(session))
    page_tokens = [
        clean_text(payload.get("result", {}).get("pageToken", ""))
        for payload in page_payloads
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict)
    ]
    non_empty_page_tokens = [token for token in page_tokens if token]
    duplicate_page_token_count = len(non_empty_page_tokens) - len(set(non_empty_page_tokens))

    for page_no, payload in enumerate(page_payloads, start=1):
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        layers["ods_api_yunting_service_page_log_d"].append(
            {
                "run_id": run_id,
                "page_no": page_no,
                "request_body_json": compact_json(payload.get("_request", {}) if isinstance(payload, dict) else {}),
                "response_code": payload.get("code", 0) if isinstance(payload, dict) else 0,
                "response_msg": payload.get("msg", "") if isinstance(payload, dict) else "",
                "trace_id": payload.get("traceId", "") if isinstance(payload, dict) else "",
                "has_more": bool(result.get("hasMore", False)) if isinstance(result, dict) else False,
                "page_token_hash": sha256_text(clean_text(result.get("pageToken", ""))) if isinstance(result, dict) else "",
                "raw_file_path": raw_file_path,
                "source_system": SOURCE_SYSTEM,
                "create_time": run_ts,
                "update_time": run_ts,
                "dt": stat_date_from(run_ts),
            }
        )

    all_message_rows: list[dict[str, Any]] = []
    all_media_rows: list[dict[str, Any]] = []

    for session in sessions:
        unique_id = session_unique_id(session)
        if not unique_id:
            continue
        insert_timestamp = normalize_time(first_present(session, "insertTimestamp", "insert_timestamp", "publishTime", "publish_time"))
        dt = stat_date_from(insert_timestamp)
        raw_json = compact_json(session)
        layers["ods_api_yunting_service_session_raw_f_d"].append(
            {
                "unique_id": unique_id,
                "run_id": run_id,
                "insert_timestamp": insert_timestamp,
                "source_name": clean_text(first_present(session, "sourceName", "source_name")),
                "project_name": clean_text(first_present(session, "projectName", "project_name")),
                "category_id": clean_text(first_present(session, "categoryId", "category_id")),
                "raw_json": raw_json,
                "raw_json_hash": sha256_text(raw_json),
                "raw_file_path": raw_file_path,
                "source_system": SOURCE_SYSTEM,
                "create_time": run_ts,
                "update_time": run_ts,
                "dt": dt,
            }
        )

        contents = session_contents(session)
        std_session = {
            "unique_id": unique_id,
            "oid": clean_text(first_present(session, "oid")),
            "project_name": clean_text(first_present(session, "projectName", "project_name")),
            "source_name": clean_text(first_present(session, "sourceName", "source_name")),
            "shop_name": clean_text(first_present(session, "shopName", "shop_name", "connectionName", "connection_name")),
            "session_type": clean_text(first_present(session, "sessionType", "session_type")),
            "session_start_time": normalize_time(first_present(session, "sessionStartTime", "session_start_time", "publishTime", "publish_time")),
            "session_close_time": normalize_time(first_present(session, "sessionCloseTime", "session_close_time")),
            "user_id": clean_text(first_present(session, "userId", "user_id")),
            "user_name": clean_text(first_present(session, "userName", "user_name")),
            "server_name_list_json": json_array(first_present(session, "serverNameList", "server_name_list", default=[])),
            "order_no_list_json": json_array(first_present(session, "orderNoList", "order_no_list", default=[])),
            "escore": first_present(session, "escore", default=0) or 0,
            "score": first_present(session, "score", default=0) or 0,
            "is_default": first_present(session, "isDefault", "is_default", default=""),
            "category_id": clean_text(first_present(session, "categoryId", "category_id")),
            "contents_json": compact_json(contents),
            "topic_configs_json": compact_json(_parse_json_value(first_present(session, "topicConfigs", "topic_configs", default=[]), [])),
            "tag_list_json": compact_json(_parse_json_value(first_present(session, "tagList", "tag_list", default=[]), [])),
            "raw_file_path": raw_file_path,
            "source_system": SOURCE_SYSTEM,
            "biz_create_time": insert_timestamp,
            "biz_update_time": normalize_time(first_present(session, "crawlTimestamp", "crawl_timestamp", "updateTime", "update_time")),
            "create_time": run_ts,
            "update_time": run_ts,
            "dt": dt,
        }
        layers["std_api_yunting_service_session_f_d"].append(std_session)
        layers["dwd_api_yunting_service_session_f_d"].append({**std_session, "source_type": SOURCE_TYPE})

        message_rows = [_message_row(session, message, index, run_ts) for index, message in enumerate(contents, start=1)]
        media_rows = [row for message in message_rows if (row := _media_asset_row(message, run_ts))]
        layers["std_api_yunting_service_message_f_d"].extend(message_rows)
        layers["dwd_api_yunting_service_message_f_d"].extend({**row, "source_type": SOURCE_TYPE} for row in message_rows)
        layers["std_api_yunting_service_media_asset_f_d"].extend(media_rows)
        layers["dwd_api_yunting_service_media_asset_f_d"].extend({**row, "source_type": SOURCE_TYPE} for row in media_rows)
        all_message_rows.extend(message_rows)
        all_media_rows.extend(media_rows)

        for topic_name, values in topic_values(session).items():
            for value in values:
                layers["dim_yunting_topic_value"].append(
                    {
                        "unique_id": unique_id,
                        "topic_name": topic_name,
                        "topic_value": value,
                        "topic_value_hash": sha256_text(f"{topic_name}:{value}"),
                        "source_system": SOURCE_SYSTEM,
                        "create_time": run_ts,
                        "update_time": run_ts,
                        "dt": dt,
                    }
                )
        tag_list = _parse_json_value(first_present(session, "tagList", "tag_list", default=[]), [])
        for tag in tag_list or []:
            tag_name = clean_text(first_present(tag, "tagName", "tag_name", "name", "tag")) if isinstance(tag, dict) else clean_text(tag)
            if not tag_name:
                continue
            layers["dim_yunting_tag"].append(
                {
                    "unique_id": unique_id,
                    "tag_name": tag_name,
                    "tag_escore": first_present(tag, "escore", default=0) if isinstance(tag, dict) else 0,
                    "topic_configs_json": std_session["topic_configs_json"],
                    "source_system": SOURCE_SYSTEM,
                    "create_time": run_ts,
                    "update_time": run_ts,
                    "dt": dt,
                }
            )

        cases, chunks, media_observations = _build_faq_rows(session, std_session, message_rows, media_rows, run_ts)
        layers["dws_yunting_service_faq_case_d"].extend(cases)
        layers["dws_yunting_service_faq_chunk_d"].extend(chunks)
        layers["dws_yunting_service_media_observation_d"].extend(media_observations)

    for enum_type, values in {
        "role": ["CUSTOMER", "SERVER"],
        "message_type": ["TEXT", "IMAGE", "VIDEO"],
        "download_status": ["not_downloaded", "downloaded", "failed"],
        "authority_level": [AUTHORITY_LEVEL],
    }.items():
        for value in values:
            layers["dim_yunting_service_enum"].append(
                {
                    "enum_type": enum_type,
                    "enum_code": value,
                    "enum_name": value,
                    "source_system": SOURCE_SYSTEM,
                    "create_time": run_ts,
                    "update_time": run_ts,
                    "dt": stat_date_from(run_ts),
                }
            )

    _build_ads_and_dm(layers, run_id, run_ts, text_collection=text_collection, media_collection=media_collection)
    manifest = PipelineManifest(
        run_id=run_id,
        raw_session_count=len(layers["ods_api_yunting_service_session_raw_f_d"]),
        std_message_count=len(all_message_rows),
        media_asset_count=len(all_media_rows),
        faq_case_count=len(layers["dws_yunting_service_faq_case_d"]),
        faq_chunk_count=len(layers["dws_yunting_service_faq_chunk_d"]),
        table_row_counts={table: len(rows) for table, rows in layers.items()},
        missing_unique_count=missing_unique_count,
        duplicate_page_token_count=duplicate_page_token_count,
        empty_page_guard_triggered=False,
    )
    return layers, manifest


def _build_ads_and_dm(
    layers: LayerRows,
    run_id: str,
    run_ts: str,
    *,
    text_collection: str,
    media_collection: str,
) -> None:
    for chunk in layers["dws_yunting_service_faq_chunk_d"]:
        payload = {
            "run_id": run_id,
            "data_version": run_id,
            "chunk_id": chunk["chunk_id"],
            "case_id": chunk["case_id"],
            "unique_id": chunk["unique_id"],
            "chunk_type": chunk["chunk_type"],
            "source_type": chunk["source_type"],
            "reference_class": chunk["reference_class"],
            "authority_level": chunk["authority_level"],
            "authority_score": chunk["authority_score"],
            "can_be_reference": chunk["can_be_reference"],
            "source_content_ids": json.loads(chunk["source_content_ids_json"]),
            "stat_date": chunk["stat_date"],
        }
        layers["ads_agent_yunting_faq_vector_api_d"].append(
            {
                "point_id": stable_uuid(text_collection, chunk["chunk_id"]),
                "collection_name": text_collection,
                "chunk_id": chunk["chunk_id"],
                "case_id": chunk["case_id"],
                "unique_id": chunk["unique_id"],
                "vector_model": "text-embedding-v4",
                "vector_dimension": int(os.getenv("YUNTING_TEXT_EMBEDDING_DIMENSION", "1024")),
                "payload_json": compact_json(payload),
                "payload_hash": sha256_text(compact_json(payload)),
                "embedding_text": chunk["chunk_text"],
                "embedding_text_hash": chunk["embedding_text_hash"],
                "sync_status": "pending",
                "last_synced_at": None,
                "error_message": "",
                "stat_date": chunk["stat_date"],
                "stat_week": chunk["stat_week"],
                "create_time": run_ts,
                "update_time": run_ts,
            }
        )

    media_object_keys = {
        row["asset_id"]: row.get("media_object_key", "")
        for row in layers["dwd_api_yunting_service_media_asset_f_d"]
    }
    for media in layers["dws_yunting_service_media_observation_d"]:
        payload = {
            "run_id": run_id,
            "data_version": run_id,
            "media_chunk_id": media["media_chunk_id"],
            "asset_id": media["asset_id"],
            "unique_id": media["unique_id"],
            "content_id": media["content_id"],
            "message_type": media["message_type"],
            "source_type": media["source_type"],
            "reference_class": media["reference_class"],
            "authority_level": media["authority_level"],
            "authority_score": media["authority_score"],
            "can_be_reference": media["can_be_reference"],
        }
        layers["ads_agent_yunting_media_vector_api_d"].append(
            {
                "point_id": stable_uuid(media_collection, media["media_chunk_id"]),
                "collection_name": media_collection,
                "media_chunk_id": media["media_chunk_id"],
                "asset_id": media["asset_id"],
                "unique_id": media["unique_id"],
                "vector_model": "qwen3-vl-embedding",
                "vector_dimension": 1024,
                "payload_json": compact_json(payload),
                "media_object_key": media_object_keys.get(media["asset_id"], ""),
                "sync_status": "skipped_no_semantic_vector",
                "last_synced_at": None,
                "error_message": "",
                "stat_date": media["stat_date"],
                "stat_week": media["stat_week"],
                "create_time": run_ts,
                "update_time": run_ts,
            }
        )

    stat_date = stat_date_from(run_ts)
    stat_week = stat_week_from(run_ts)
    layers["ads_agent_yunting_pipeline_dashboard_d"].append(
        {
            "stat_date": stat_date,
            "stat_week": stat_week,
            "run_id": run_id,
            "api_page_count": len(layers["ods_api_yunting_service_page_log_d"]),
            "raw_session_count": len(layers["ods_api_yunting_service_session_raw_f_d"]),
            "std_session_count": len(layers["std_api_yunting_service_session_f_d"]),
            "valid_case_count": len(layers["dws_yunting_service_faq_case_d"]),
            "faq_chunk_count": len(layers["dws_yunting_service_faq_chunk_d"]),
            "media_asset_count": len(layers["std_api_yunting_service_media_asset_f_d"]),
            "download_success_count": 0,
            "embedding_success_count": 0,
            "qdrant_upsert_success_count": 0,
            "failed_count": 0,
            "create_time": run_ts,
            "update_time": run_ts,
        }
    )

    role_counts = Counter(row["role"] for row in layers["dwd_api_yunting_service_message_f_d"])
    layers["dm_yunting_service_quality_d"].append(
        {
            "stat_date": stat_date,
            "stat_week": stat_week,
            "session_count": len(layers["dwd_api_yunting_service_session_f_d"]),
            "valid_case_count": len(layers["dws_yunting_service_faq_case_d"]),
            "message_count": len(layers["dwd_api_yunting_service_message_f_d"]),
            "customer_message_count": role_counts.get("CUSTOMER", 0),
            "server_message_count": role_counts.get("SERVER", 0),
            "source_type": SOURCE_TYPE,
            "reference_class": REFERENCE_CLASS,
            "authority_level": AUTHORITY_LEVEL,
            "create_time": run_ts,
            "update_time": run_ts,
        }
    )
    layers["dm_yunting_service_product_tag_d"].append(
        {
            "stat_date": stat_date,
            "stat_week": stat_week,
            "topic_value_count": len(layers["dim_yunting_topic_value"]),
            "tag_count": len(layers["dim_yunting_tag"]),
            "faq_count": len(layers["dws_yunting_service_faq_case_d"]),
            "media_evidence_count": len(layers["dws_yunting_service_media_observation_d"]),
            "source_type": SOURCE_TYPE,
            "create_time": run_ts,
            "update_time": run_ts,
        }
    )
    layers["dm_yunting_service_media_d"].append(
        {
            "stat_date": stat_date,
            "stat_week": stat_week,
            "image_count": sum(row["message_type"] == "IMAGE" for row in layers["dwd_api_yunting_service_media_asset_f_d"]),
            "video_count": sum(row["message_type"] == "VIDEO" for row in layers["dwd_api_yunting_service_media_asset_f_d"]),
            "download_success_count": 0,
            "ocr_success_count": 0,
            "visual_summary_success_count": 0,
            "media_upsert_success_count": 0,
            "source_type": SOURCE_TYPE,
            "create_time": run_ts,
            "update_time": run_ts,
        }
    )


def write_layers(output_dir: Path, layers: LayerRows, manifest: PipelineManifest) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for table_name, rows in layers.items():
        path = output_dir / f"{table_name}.jsonl"
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
    (output_dir / "manifest.json").write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
