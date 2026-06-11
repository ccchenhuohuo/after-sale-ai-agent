import argparse
import base64
import json
import mimetypes
import os
import re
from collections import Counter, defaultdict
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
DEFAULT_OUTPUT_DIR = ROOT / "data" / "media_rag" / "index" / "latest"
DEFAULT_DIMENSION = 1024
DEFAULT_BAILIAN_MULTIMODAL_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
DEFAULT_BAILIAN_VL_EMBEDDING_MODEL = "qwen3-vl-embedding"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".ico", ".dib", ".icns", ".sgi"}
URL_FIELDS = ("媒体URL", "media_url", "url", "文件URL", "file_url", "download_url", "resource_url")
SKU_RE = re.compile(r"\b[A-Z]{1,4}\d{2,5}[A-Z0-9-]*\b")


BASE_METADATA = {
    "source_type": "feishu_raw_media",
    "evidence_level": "media_observation_unreviewed",
    "is_reviewed": False,
    "can_be_formal_evidence": False,
}


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


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


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def tokenize(text: str) -> list[str]:
    normalized = clean_text(text).upper()
    tokens = SKU_RE.findall(normalized)
    tokens.extend(re.findall(r"[A-Z0-9-]{2,}", normalized))
    chinese = re.findall(r"[\u4e00-\u9fff]", normalized)
    tokens.extend(chinese)
    tokens.extend("".join(pair) for pair in zip(chinese, chinese[1:]))
    return [token for token in tokens if token]


def hashed_embedding(text: str, dimension: int = DEFAULT_DIMENSION) -> np.ndarray:
    vector = np.zeros(dimension, dtype=np.float32)
    for token, count in Counter(tokenize(text)).items():
        index = int.from_bytes(sha256(token.encode("utf-8")).digest()[:8], "big") % dimension
        vector[index] += float(count)
    norm = np.linalg.norm(vector)
    if norm:
        vector /= norm
    return vector


def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


def image_data_uri(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def find_media_file(staging_dir: Path, media_id: str | None) -> Path | None:
    if not media_id:
        return None
    media_dir = staging_dir / "media"
    if not media_dir.exists():
        return None
    for path in sorted(media_dir.glob(f"{media_id}.*")):
        if path.is_file():
            return path
    return None


def media_url(asset: dict[str, Any]) -> str:
    for field in URL_FIELDS:
        value = clean_text(asset.get(field))
        if value.startswith(("http://", "https://")):
            return value
    return ""


def vl_content_for_media(media_type: str, media_url_value: str, media_file: Path | None) -> dict[str, str] | None:
    normalized_type = clean_text(media_type).lower()
    if media_url_value:
        if "video" in normalized_type:
            return {"video": media_url_value}
        if "image" in normalized_type:
            return {"image": media_url_value}
    if media_file and media_file.exists() and media_file.suffix.lower() in IMAGE_SUFFIXES:
        return {"image": image_data_uri(media_file)}
    return None


def extract_embedding_payload(payload: dict[str, Any]) -> list[list[float]]:
    output = payload.get("output")
    if isinstance(output, dict) and isinstance(output.get("embeddings"), list):
        embeddings = []
        for item in output["embeddings"]:
            if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                embeddings.append(item["embedding"])
        if embeddings:
            return embeddings
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


def bailian_vl_embedding(
    endpoint: str,
    api_key: str,
    model: str,
    contents: list[dict[str, str]],
    *,
    dimension: int = DEFAULT_DIMENSION,
    timeout: float = 120.0,
) -> np.ndarray:
    if not api_key:
        raise RuntimeError("BAILIAN_API_KEY or DASHSCOPE_API_KEY is required for provider=bailian_vl.")
    response = httpx.post(
        endpoint,
        json={
            "model": model,
            "input": {"contents": contents},
            "parameters": {"enable_fusion": True, "dimension": dimension},
        },
        headers={**auth_headers(api_key), "Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    embeddings = extract_embedding_payload(response.json())
    if not embeddings:
        raise RuntimeError("Bailian qwen3-vl-embedding returned invalid payload.")
    vector = np.asarray(embeddings[0], dtype=np.float32)
    norm = np.linalg.norm(vector)
    if norm:
        vector = vector / norm
    return vector


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def build_media_cases(staging_dir: Path, events: list[dict[str, Any]], media_assets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events_by_topic = {event.get("话题ID") or event.get("__topicId"): event for event in events}
    media_by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for asset in media_assets:
        topic_id = asset.get("话题ID") or asset.get("__topicId")
        if topic_id:
            media_by_topic[topic_id].append(asset)

    cases: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    for topic_id, assets in sorted(media_by_topic.items()):
        event = events_by_topic.get(topic_id, {})
        media_types = sorted({clean_text(asset.get("媒体类型")) for asset in assets if clean_text(asset.get("媒体类型"))})
        media_ids = [asset.get("媒体ID") for asset in assets if asset.get("媒体ID")]
        topic_link = clean_text(event.get("话题链接") or event.get("原始消息链接") or assets[0].get("原消息链接"))
        case = {
            **BASE_METADATA,
            "case_id": f"media::{topic_id}",
            "topic_id": topic_id,
            "topic_link": topic_link,
            "sku": clean_text(event.get("产品型号/SKU")),
            "issue_category": clean_text(event.get("问题大类")),
            "solution_type": clean_text(event.get("解决方案类型")),
            "symptom_summary": clean_text(event.get("客户症状摘要") or event.get("AI问题总结")),
            "resolution_summary": clean_text(event.get("处理建议/结论")),
            "media_count": len(assets),
            "media_types": media_types,
            "media_ids": media_ids,
        }
        cases.append(case)

        overview = "\n".join(
            [
                "媒体观察证据概览",
                f"话题ID：{topic_id}",
                f"SKU：{case['sku'] or '未识别'}",
                f"媒体数量：{len(assets)}",
                f"媒体类型：{', '.join(media_types) or 'unknown'}",
                f"问题分类：{case['issue_category']}",
                f"症状上下文：{case['symptom_summary']}",
                f"处理结论：{case['resolution_summary']}",
                "证据边界：当前仅索引媒体元数据、占位文本和上下文摘要；尚未直接解析图片、视频或截图内容。",
                "审核状态：未审核媒体观察证据，需人工确认，不能作为正式依据。",
            ]
        )
        chunks.append(
            {
                **BASE_METADATA,
                "chunk_id": f"media::{topic_id}::overview",
                "chunk_type": "media_observation_overview",
                "topic_id": topic_id,
                "topic_link": topic_link,
                "sku": case["sku"],
                "media_types": media_types,
                "media_ids": media_ids,
                "text": overview,
            }
        )

        for asset in assets:
            media_type = clean_text(asset.get("媒体类型")) or "unknown"
            media_file = find_media_file(staging_dir, asset.get("媒体ID"))
            media_url_value = media_url(asset)
            media_vl_content = vl_content_for_media(media_type, media_url_value, media_file)
            text = "\n".join(
                [
                    "媒体资产记录",
                    f"媒体ID：{asset.get('媒体ID', '')}",
                    f"媒体类型：{media_type}",
                    f"归档策略：{asset.get('归档策略', '')}",
                    f"分析状态：{asset.get('分析状态', '')}",
                    f"话题上下文：{case['symptom_summary']}",
                    f"处理结论：{case['resolution_summary']}",
                    "证据边界：该条记录用于定位图片、视频、截图、外观瑕疵、安装姿态、配件缺失或包装损坏等媒体证据；当前未直接解析媒体内容。",
                ]
            )
            chunks.append(
                {
                    **BASE_METADATA,
                    "chunk_id": f"media::{asset.get('媒体ID')}",
                    "chunk_type": "media_asset",
                    "topic_id": topic_id,
                    "topic_link": topic_link,
                    "sku": case["sku"],
                    "media_type": media_type,
                    "media_id": asset.get("媒体ID"),
                    "message_id": asset.get("message_id"),
                    "message_link": asset.get("原消息链接"),
                    "media_url": media_url_value,
                    "media_url_available": bool(media_url_value),
                    "media_file_path": str(media_file) if media_file else "",
                    "media_file_available": bool(media_file),
                    "media_file_is_image": bool(media_file and media_file.suffix.lower() in IMAGE_SUFFIXES),
                    "media_vl_content_type": next(iter(media_vl_content.keys())) if media_vl_content else "",
                    "text": text,
                }
            )

    return cases, chunks


def build_index(
    staging_dir: Path,
    output_dir: Path,
    dimension: int = DEFAULT_DIMENSION,
    provider: str = "local_hash",
    bailian_api_key: str = "",
    bailian_multimodal_embedding_base_url: str = DEFAULT_BAILIAN_MULTIMODAL_EMBEDDING_BASE_URL,
    embedding_model: str = DEFAULT_BAILIAN_VL_EMBEDDING_MODEL,
    embedding_text_max_chars: int = 1200,
) -> dict[str, Any]:
    events = load_json(staging_dir / "event_candidates.json")
    media_assets = load_json(staging_dir / "media_assets.json")
    cases, chunks = build_media_cases(staging_dir, events, media_assets)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "media_cases.jsonl", cases)
    write_jsonl(output_dir / "media_chunks.jsonl", chunks)
    vectors: list[np.ndarray] = []
    vl_embedding_count = 0
    local_hash_fallback_count = 0
    for index, chunk in enumerate(chunks, start=1):
        vector: np.ndarray | None = None
        media_file = Path(chunk.get("media_file_path", ""))
        media_url_value = clean_text(chunk.get("media_url"))
        media_vl_content = vl_content_for_media(chunk.get("media_type", ""), media_url_value, media_file)
        if provider == "bailian_vl" and media_vl_content:
            contents = [{"text": clean_text(chunk.get("text"))[:embedding_text_max_chars]}, media_vl_content]
            print(f"bailian vl embedding media chunk {index}/{len(chunks)} {chunk.get('media_id')}", flush=True)
            vector = bailian_vl_embedding(
                bailian_multimodal_embedding_base_url,
                bailian_api_key,
                embedding_model,
                contents,
                dimension=dimension,
            )
            chunk["embedding_backend"] = "bailian_vl_fusion"
            vl_embedding_count += 1
        if vector is None:
            vector = hashed_embedding(chunk["text"], dimension)
            chunk["embedding_backend"] = "local_hash_media_text"
            local_hash_fallback_count += 1
        vectors.append(vector)

    embeddings = np.vstack(vectors) if vectors else np.zeros((0, dimension), dtype=np.float32)
    np.save(output_dir / "media_embeddings.npy", embeddings)
    write_jsonl(output_dir / "media_chunks.jsonl", chunks)

    manifest = {
        "index_type": "media_evidence_rag_mvp",
        "source_staging_dir": str(staging_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "chunk_count": len(chunks),
        "embedding_dimension": int(embeddings.shape[1]) if embeddings.ndim == 2 else dimension,
        "embedding_backend": "bailian_vl_mixed" if provider == "bailian_vl" else "local_hashed_media_bootstrap",
        "media_rag_provider": provider,
        "embedding_model": embedding_model if provider == "bailian_vl" else None,
        "bailian_multimodal_embedding_base_url": bailian_multimodal_embedding_base_url if provider == "bailian_vl" else None,
        "vl_embedding_count": vl_embedding_count,
        "local_hash_fallback_count": local_hash_fallback_count,
        "media_url_count": sum(1 for chunk in chunks if chunk.get("media_url_available")),
        "local_media_file_count": sum(1 for chunk in chunks if chunk.get("media_file_available")),
        "local_image_file_count": sum(1 for chunk in chunks if chunk.get("media_file_is_image")),
        "embedding_text_max_chars": embedding_text_max_chars if provider == "bailian_vl" else None,
        "planned_embedding_model": "qwen3-vl-embedding",
        "planned_rerank_model": "qwen3-vl-rerank",
        "source_type": "feishu_raw_media",
        "evidence_level": "media_observation_unreviewed",
        "is_reviewed": False,
        "can_be_formal_evidence": False,
        "media_boundary": "Chunks with downloaded local images use qwen3-vl-embedding fusion vectors; unavailable media falls back to metadata text vectors and still requires human confirmation.",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build media observation evidence index from Feishu topic staging data.")
    parser.add_argument("--staging-dir", default=str(DEFAULT_TOPIC_STAGING_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dimension", type=int, default=DEFAULT_DIMENSION)
    parser.add_argument("--provider", choices=["local_hash", "bailian_vl"], default="local_hash")
    parser.add_argument("--bailian-api-key", default=env_value("BAILIAN_API_KEY") or env_value("DASHSCOPE_API_KEY"))
    parser.add_argument("--bailian-multimodal-embedding-base-url", default=env_value("BAILIAN_MULTIMODAL_EMBEDDING_BASE_URL", DEFAULT_BAILIAN_MULTIMODAL_EMBEDDING_BASE_URL))
    parser.add_argument("--embedding-model", default=env_value("MEDIA_RAG_EMBEDDING_MODEL", DEFAULT_BAILIAN_VL_EMBEDDING_MODEL))
    parser.add_argument("--embedding-text-max-chars", type=int, default=1200)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = build_index(
        Path(args.staging_dir),
        Path(args.output_dir),
        args.dimension,
        provider=args.provider,
        bailian_api_key=args.bailian_api_key,
        bailian_multimodal_embedding_base_url=args.bailian_multimodal_embedding_base_url,
        embedding_model=args.embedding_model,
        embedding_text_max_chars=args.embedding_text_max_chars,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
