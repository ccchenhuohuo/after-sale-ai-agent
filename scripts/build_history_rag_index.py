import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOPIC_STAGING_DIR = (
    ROOT.parent
    / "工单自动化工作流"
    / "tmp"
    / "topic_backfills"
    / "topic-dryrun-20260530-20260601-full"
)
DEFAULT_OUTPUT_DIR = ROOT / "data" / "history_rag" / "index" / "latest"
DEFAULT_DIMENSION = 768
DEFAULT_BAILIAN_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_BAILIAN_EMBEDDING_MODEL = "text-embedding-v4"

BASE_METADATA = {
    "source_type": "reviewed_feishu_history_faq",
    "evidence_level": "reviewed_case",
    "is_reviewed": True,
    "can_be_formal_evidence": False,
}

SOLUTION_SIGNAL_RE = re.compile(
    r"正常|制成|制程|供应商|更换|换新|补发|排查|退款|退货|客户使用|使用问题|维修|扭紧|拧紧|确认处理方案"
)
SKU_RE = re.compile(r"\b[A-Z]{1,4}\d{2,5}[A-Z0-9-]*\b")


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("[Invalid text JSON]", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def env_value(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value:
        return value
    env_path = ROOT / ".env"
    if not env_path.exists():
        return default
    for line in env_path.read_text(errors="ignore").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key == name:
            return raw_value.strip()
    return default


def tokenize(text: str) -> list[str]:
    normalized = clean_text(text).upper()
    tokens: list[str] = []
    tokens.extend(SKU_RE.findall(normalized))
    tokens.extend(re.findall(r"[A-Z0-9-]{2,}", normalized))
    chinese = re.findall(r"[\u4e00-\u9fff]", normalized)
    tokens.extend(chinese)
    tokens.extend("".join(pair) for pair in zip(chinese, chinese[1:]))
    return [token for token in tokens if token]


def hashed_embedding(text: str, dimension: int = DEFAULT_DIMENSION) -> np.ndarray:
    vector = np.zeros(dimension, dtype=np.float32)
    counts = Counter(tokenize(text))
    for token, count in counts.items():
        index = int.from_bytes(sha256(token.encode("utf-8")).digest()[:8], "big") % dimension
        vector[index] += float(count)
    norm = np.linalg.norm(vector)
    if norm:
        vector /= norm
    return vector


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_topics(path: Path) -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                topics.append(json.loads(line))
    return topics


def event_by_topic_id(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {event.get("话题ID") or event.get("__topicId"): event for event in events}


def message_line(message: dict[str, Any]) -> str:
    sender = clean_text(message.get("sender_name")) or "未知"
    content = clean_text(message.get("content_text"))
    time = clean_text(message.get("create_time"))
    sequence = message.get("topic_sequence", "")
    return f"{sequence}. {time} {sender}: {content}".strip()


def visible_messages(topic: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        message
        for message in topic.get("messages", [])
        if message.get("msg_type") != "system" and clean_text(message.get("content_text"))
    ]


def media_observation(event: dict[str, Any]) -> str:
    value = clean_text(event.get("非文本信息提取"))
    if value:
        return value
    media_count = event.get("媒体数量") or 0
    if media_count:
        return f"该话题包含 {media_count} 个媒体资源；当前 RAG MVP 仅引用已提取的非文本摘要，不直接解析图片或视频。"
    return ""


def build_case(topic: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    messages = visible_messages(topic)
    title = clean_text(event.get("事件标题")) or clean_text(messages[0].get("content_text") if messages else "")
    return {
        **BASE_METADATA,
        "case_id": event.get("事件ID") or topic.get("topicId"),
        "topic_id": topic.get("topicId"),
        "thread_id": topic.get("threadId"),
        "root_message_id": topic.get("rootMessageId"),
        "topic_link": topic.get("topicLink") or event.get("话题链接") or event.get("原始消息链接"),
        "created_at": topic.get("topicCreateTime") or event.get("首次反馈时间"),
        "updated_at": topic.get("topicLastMessageTime") or event.get("最后更新时间"),
        "title": title,
        "sku": clean_text(event.get("产品型号/SKU")),
        "issue_category": clean_text(event.get("问题大类")),
        "abnormal_category": clean_text(event.get("异常分类")),
        "solution_type": clean_text(event.get("解决方案类型")),
        "symptom_summary": clean_text(event.get("客户症状摘要")),
        "resolution_summary": clean_text(event.get("处理建议/结论")),
        "media_observation": media_observation(event),
        "message_count": topic.get("messageCount", len(messages)),
        "media_count": topic.get("mediaCount", event.get("媒体数量", 0)),
    }


def chunk_text(header: str, lines: list[str]) -> str:
    return "\n".join([header, *[line for line in lines if clean_text(line)]]).strip()


def build_chunks(case: dict[str, Any], topic: dict[str, Any]) -> list[dict[str, Any]]:
    messages = visible_messages(topic)
    base = {
        **BASE_METADATA,
        "case_id": case["case_id"],
        "topic_id": case["topic_id"],
        "topic_link": case["topic_link"],
        "sku": case["sku"],
        "issue_category": case["issue_category"],
        "abnormal_category": case["abnormal_category"],
        "solution_type": case["solution_type"],
        "created_at": case["created_at"],
    }

    chunks = [
        {
            **base,
            "chunk_id": f"{case['case_id']}::topic_overview",
            "chunk_type": "topic_overview",
            "text": chunk_text(
                "话题概览",
                [
                    f"标题：{case['title']}",
                    f"SKU：{case['sku'] or '未识别'}",
                    f"问题大类：{case['issue_category']}",
                    f"异常分类：{case['abnormal_category']}",
                    f"解决方案类型：{case['solution_type']}",
                    f"客户症状：{case['symptom_summary']}",
                    f"处理结论：{case['resolution_summary']}",
                    "审核状态：已审核群聊历史 FAQ，可作为可靠售后参考；不是正式政策源。",
                ],
            ),
        },
        {
            **base,
            "chunk_id": f"{case['case_id']}::message_timeline",
            "chunk_type": "message_timeline",
            "text": chunk_text("话题消息时间线", [message_line(message) for message in messages]),
        },
    ]

    solution_lines = [message_line(message) for message in messages if SOLUTION_SIGNAL_RE.search(clean_text(message.get("content_text")))]
    if solution_lines:
        chunks.append(
            {
                **base,
                "chunk_id": f"{case['case_id']}::solution_evidence",
                "chunk_type": "solution_evidence",
                "text": chunk_text(
                    "处理结论相关消息",
                    [*solution_lines, "审核状态：已审核群聊历史 FAQ，可作为可靠售后参考；不是正式政策源。"],
                ),
            }
        )

    if case["media_observation"]:
        chunks.append(
            {
                **base,
                "chunk_id": f"{case['case_id']}::media_observation",
                "chunk_type": "media_observation",
                "text": chunk_text(
                    "非文本信息摘要",
                    [
                        case["media_observation"],
                        "说明：当前 RAG MVP 不直接解析图片或视频，只引用已存在的非文本摘要。",
                    ],
                ),
            }
        )

    return chunks


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


def endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def extract_embedding_payload(payload: dict[str, Any]) -> list[list[float]]:
    if isinstance(payload.get("embeddings"), list):
        return payload["embeddings"]
    data = payload.get("data")
    if isinstance(data, list):
        embeddings = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                embeddings.append(item["embedding"])
        if embeddings:
            return embeddings
    return []


def bailian_embeddings(
    base_url: str,
    api_key: str,
    model: str,
    texts: list[str],
    *,
    batch_size: int = 8,
    text_max_chars: int = 1600,
    timeout: float = 120.0,
) -> np.ndarray:
    if not api_key:
        raise RuntimeError("BAILIAN_API_KEY or DASHSCOPE_API_KEY is required for provider=bailian.")

    vectors: list[list[float]] = []
    prepared_texts = [clean_text(text)[:text_max_chars] if text_max_chars > 0 else text for text in texts]
    for start in range(0, len(prepared_texts), batch_size):
        batch = prepared_texts[start : start + batch_size]
        print(f"bailian embedding batch {start + 1}-{start + len(batch)} / {len(prepared_texts)}", flush=True)
        response = httpx.post(
            endpoint(base_url, "embeddings"),
            json={"model": model, "input": batch},
            headers=auth_headers(api_key),
            timeout=timeout,
        )
        response.raise_for_status()
        embeddings = extract_embedding_payload(response.json())
        if not isinstance(embeddings, list) or len(embeddings) != len(batch):
            raise RuntimeError(f"Bailian embedding returned invalid payload for batch starting at {start}.")
        vectors.extend(embeddings)

    array = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return array / norms


def build_index(
    staging_dir: Path,
    output_dir: Path,
    dimension: int = DEFAULT_DIMENSION,
    provider: str = "local_hash",
    embedding_batch_size: int = 8,
    embedding_text_max_chars: int = 1600,
    bailian_api_key: str = "",
    bailian_embedding_base_url: str = DEFAULT_BAILIAN_EMBEDDING_BASE_URL,
    embedding_model: str = DEFAULT_BAILIAN_EMBEDDING_MODEL,
) -> dict[str, Any]:
    topics = load_topics(staging_dir / "topics.ndjson")
    events = load_json(staging_dir / "event_candidates.json")
    events_by_topic = event_by_topic_id(events)

    cases: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    for topic in topics:
        topic_id = topic.get("topicId")
        event = events_by_topic.get(topic_id)
        if not event:
            continue
        case = build_case(topic, event)
        cases.append(case)
        chunks.extend(build_chunks(case, topic))

    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "history_cases.jsonl", cases)
    write_jsonl(output_dir / "history_chunks.jsonl", chunks)

    embedding_backend = "local_hashed_bootstrap"
    if provider == "bailian" and chunks:
        embeddings = bailian_embeddings(
            bailian_embedding_base_url,
            bailian_api_key,
            embedding_model,
            [chunk["text"] for chunk in chunks],
            batch_size=embedding_batch_size,
            text_max_chars=embedding_text_max_chars,
        )
        embedding_backend = "bailian"
    else:
        embeddings = (
            np.vstack([hashed_embedding(chunk["text"], dimension) for chunk in chunks])
            if chunks
            else np.zeros((0, dimension), dtype=np.float32)
        )
    np.save(output_dir / "embeddings.npy", embeddings)

    manifest = {
        "index_type": "reviewed_history_faq_rag_v1",
        "source_staging_dir": str(staging_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "chunk_count": len(chunks),
        "embedding_dimension": int(embeddings.shape[1]) if embeddings.ndim == 2 else dimension,
        "embedding_backend": embedding_backend,
        "history_rag_provider": provider,
        "embedding_model": embedding_model if provider == "bailian" else None,
        "bailian_embedding_base_url": bailian_embedding_base_url if provider == "bailian" else None,
        "embedding_text_max_chars": embedding_text_max_chars if provider == "bailian" else None,
        "source_type": "reviewed_feishu_history_faq",
        "evidence_level": "reviewed_case",
        "is_reviewed": True,
        "can_be_formal_evidence": False,
        "model_plan": {
            "embedding": "Bailian API provider only",
            "reranker": "Bailian API provider only",
            "generation": "existing support Agent model",
            "current_backend": embedding_backend,
        },
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build local RAG index from Feishu raw topic JSON staging data.")
    parser.add_argument("--staging-dir", default=str(DEFAULT_TOPIC_STAGING_DIR), help="Topic backfill staging directory.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="RAG index output directory.")
    parser.add_argument("--dimension", type=int, default=DEFAULT_DIMENSION, help="Hashed embedding dimension.")
    parser.add_argument("--provider", choices=["local_hash", "bailian"], default="local_hash")
    parser.add_argument("--embedding-batch-size", type=int, default=8, help="Remote embedding batch size.")
    parser.add_argument("--embedding-text-max-chars", type=int, default=1600, help="Maximum characters sent per chunk to the remote embedding service.")
    parser.add_argument("--bailian-api-key", default=env_value("BAILIAN_API_KEY") or env_value("DASHSCOPE_API_KEY"))
    parser.add_argument("--bailian-embedding-base-url", default=env_value("BAILIAN_EMBEDDING_BASE_URL", DEFAULT_BAILIAN_EMBEDDING_BASE_URL))
    parser.add_argument("--embedding-model", default=env_value("HISTORY_RAG_EMBEDDING_MODEL", DEFAULT_BAILIAN_EMBEDDING_MODEL))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = build_index(
        Path(args.staging_dir),
        Path(args.output_dir),
        args.dimension,
        provider=args.provider,
        embedding_batch_size=args.embedding_batch_size,
        embedding_text_max_chars=args.embedding_text_max_chars,
        bailian_api_key=args.bailian_api_key,
        bailian_embedding_base_url=args.bailian_embedding_base_url,
        embedding_model=args.embedding_model,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
