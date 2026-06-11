import base64
import json
import math
import mimetypes
import re
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

import httpx
import numpy as np
from agents import custom_span

from agent_runtime.settings import Settings, get_settings


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DIMENSION = 1024
REMOTE_TEXT_MAX_CHARS = 1200
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".ico", ".dib", ".icns", ".sgi"}
SKU_RE = re.compile(r"\b[A-Z]{1,4}\d{2,5}[A-Z0-9-]*\b")


@dataclass(frozen=True)
class MediaSearchResult:
    chunk: dict[str, Any]
    similarity_score: float
    rerank_score: float
    matched_reasons: list[str]


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _tokenize(text: str) -> list[str]:
    normalized = _clean(text).upper()
    tokens = SKU_RE.findall(normalized)
    tokens.extend(re.findall(r"[A-Z0-9-]{2,}", normalized))
    chinese = re.findall(r"[\u4e00-\u9fff]", normalized)
    tokens.extend(chinese)
    tokens.extend("".join(pair) for pair in zip(chinese, chinese[1:]))
    return [token for token in tokens if token]


def _short_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


def _auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


def _image_data_uri(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def _hashed_embedding(text: str, dimension: int = DEFAULT_DIMENSION) -> np.ndarray:
    vector = np.zeros(dimension, dtype=np.float32)
    for token, count in Counter(_tokenize(text)).items():
        index = int.from_bytes(sha256(token.encode("utf-8")).digest()[:8], "big") % dimension
        vector[index] += float(count)
    norm = np.linalg.norm(vector)
    if norm:
        vector /= norm
    return vector


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _load_embeddings(path: Path) -> np.ndarray | None:
    if not path.exists():
        return None
    return np.load(path)


def _extract_embedding_payload(payload: dict[str, Any]) -> list[list[float]]:
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


def _bailian_vl_embedding(
    settings: Settings,
    contents: list[dict[str, str]],
    timeout: float = 120.0,
) -> np.ndarray | None:
    api_key = settings.resolved_bailian_api_key
    if not api_key:
        return None
    try:
        response = httpx.post(
            settings.bailian_multimodal_embedding_base_url,
            json={
                "model": settings.media_rag_embedding_model,
                "input": {"contents": contents},
                "parameters": {"enable_fusion": True, "dimension": settings.media_rag_embedding_dimension},
            },
            headers={**_auth_headers(api_key), "Content-Type": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        embeddings = _extract_embedding_payload(response.json())
    except Exception:
        return None
    if not embeddings:
        return None
    vector = np.asarray(embeddings[0], dtype=np.float32)
    norm = np.linalg.norm(vector)
    if norm:
        vector = vector / norm
    return np.vstack([vector])


def _query_embedding(settings: Settings, query: str) -> np.ndarray | None:
    if settings.media_rag_provider == "bailian_vl":
        return _bailian_vl_embedding(settings, [{"text": _clean(query)[:REMOTE_TEXT_MAX_CHARS]}])
    return None


def _chunk_media_file(chunk: dict[str, Any]) -> Path | None:
    path_text = _clean(chunk.get("media_file_path"))
    if not path_text:
        return None
    path = Path(path_text)
    if path.exists() and path.suffix.lower() in IMAGE_SUFFIXES:
        return path
    return None


def _media_url(chunk: dict[str, Any]) -> str:
    value = _clean(chunk.get("media_url"))
    if value.startswith(("http://", "https://")):
        return value
    return ""


def _rerank_document(chunk: dict[str, Any]) -> dict[str, str]:
    media_url = _media_url(chunk)
    media_type = _clean(chunk.get("media_type")).lower()
    if media_url and "video" in media_type:
        return {"video": media_url}
    if media_url and "image" in media_type:
        return {"image": media_url}
    media_file = _chunk_media_file(chunk)
    if media_file:
        return {"image": _image_data_uri(media_file)}
    return {"text": _clean(chunk.get("text"))[:REMOTE_TEXT_MAX_CHARS]}


def _bailian_vl_rerank(
    settings: Settings,
    query: str,
    chunks: list[dict[str, Any]],
    timeout: float = 120.0,
) -> dict[str, float] | None:
    api_key = settings.resolved_bailian_api_key
    if not api_key:
        return None
    try:
        response = httpx.post(
            settings.bailian_vl_rerank_base_url,
            json={
                "model": settings.media_rag_rerank_model,
                "input": {
                    "query": {"text": _clean(query)[:REMOTE_TEXT_MAX_CHARS]},
                    "documents": [_rerank_document(chunk) for chunk in chunks],
                },
                "parameters": {
                    "return_documents": False,
                    "top_n": len(chunks),
                    "fps": 1.0,
                },
            },
            headers={**_auth_headers(api_key), "Content-Type": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    output = payload.get("output") if isinstance(payload, dict) else None
    results = output.get("results") if isinstance(output, dict) else None
    if not isinstance(results, list):
        return None

    scores: dict[str, float] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        index = result.get("index")
        score = result.get("relevance_score", result.get("score"))
        if isinstance(index, int) and 0 <= index < len(chunks) and score is not None:
            scores[chunks[index]["chunk_id"]] = float(score)
    return scores or None


def media_rag_index_available(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    index_dir = _path(settings.media_rag_index_path)
    chunks = _load_jsonl(index_dir / "media_chunks.jsonl")
    embeddings = _load_embeddings(index_dir / "media_embeddings.npy")
    return bool(chunks) and embeddings is not None and embeddings.ndim == 2 and embeddings.shape[0] == len(chunks)


def _filter_chunks(chunks: Iterable[dict[str, Any]], product_model: str | None) -> tuple[list[dict[str, Any]], str]:
    rows = list(chunks)
    product = _clean(product_model).upper()
    if not product:
        return rows, "no_product_filter"
    matched = [chunk for chunk in rows if product in f"{chunk.get('sku', '')} {chunk.get('text', '')}".upper()]
    if not matched:
        return [], "product_filter_no_match"
    return matched, "product_filter_matched"


def _matched_reasons(query: str, chunk: dict[str, Any], product_model: str | None) -> list[str]:
    text = f"{chunk.get('text', '')} {chunk.get('sku', '')} {chunk.get('media_type', '')} {' '.join(chunk.get('media_types', []))}".upper()
    reasons: list[str] = []
    if product_model and product_model.upper() in text:
        reasons.append("SKU/型号过滤命中")
    overlap = sorted(set(_tokenize(query)) & set(_tokenize(text)))
    if overlap:
        reasons.append(f"关键词重叠：{'、'.join(overlap[:5])}")
    media_type = chunk.get("media_type") or "、".join(chunk.get("media_types", []))
    if media_type:
        reasons.append(f"媒体类型：{media_type}")
    return reasons or ["媒体上下文相似候选"]


def _format_result(result: MediaSearchResult) -> str:
    chunk = result.chunk
    summary = _clean(chunk.get("text"))[:240]
    media_id = chunk.get("media_id") or "、".join(chunk.get("media_ids", []))
    media_type = chunk.get("media_type") or "、".join(chunk.get("media_types", []))
    return "\n".join(
        [
            f"- 话题ID：{chunk.get('topic_id', '')}",
            f"  SKU：{chunk.get('sku') or '未识别'}",
            f"  媒体类型：{media_type or '未识别'}",
            f"  媒体ID：{media_id or '未识别'}",
            f"  媒体观察摘要：{summary}",
            f"  相似原因：{'；'.join(result.matched_reasons)}",
            f"  话题链接：{chunk.get('topic_link', '')}",
            f"  消息链接：{chunk.get('message_link', '')}",
            f"  相似度：{result.similarity_score:.4f}",
            f"  重排分：{result.rerank_score:.4f}",
            "  审核状态：未审核媒体观察证据，需人工确认，不能作为正式依据。",
        ]
    )


def search_media_rag(
    query: str,
    *,
    product_model: str | None = None,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    index_dir = _path(settings.media_rag_index_path)
    chunks = _load_jsonl(index_dir / "media_chunks.jsonl")
    if not chunks:
        return f"未查询到可信媒体观察证据：媒体证据索引不存在或为空（{index_dir}）。"

    filtered_chunks, filter_status = _filter_chunks(chunks, product_model)
    if product_model and filter_status == "product_filter_no_match":
        return f"未查询到可信媒体观察证据：媒体证据索引中没有命中同 SKU/型号（{_clean(product_model)}）的媒体记录。"

    top_k = max(1, settings.media_rag_top_k)
    top_n = max(1, settings.media_rag_top_n)

    with custom_span(
        "media_embedding_search",
        {
            "query_hash": _short_hash(query),
            "product_model_hash": _short_hash(product_model or "") if product_model else "",
            "chunk_count": len(chunks),
            "filtered_chunk_count": len(filtered_chunks),
            "filter_status": filter_status,
            "top_k": top_k,
            "provider": settings.media_rag_provider,
            "embedding_model": settings.media_rag_embedding_model,
        },
    ):
        query_vector = _query_embedding(settings, query)
        if query_vector is None and settings.media_rag_require_vl_models:
            return (
                "未查询到可信媒体观察证据：媒体证据 qwen3-vl-embedding 服务不可用，"
                "且配置禁止本机 fallback。"
            )
        if query_vector is None:
            query_vector = np.vstack([_hashed_embedding(query, settings.media_rag_embedding_dimension)])
        embeddings = _load_embeddings(index_dir / "media_embeddings.npy")
        if embeddings is None or embeddings.shape[0] != len(chunks) or embeddings.shape[1] != query_vector.shape[1]:
            embeddings = np.vstack([_hashed_embedding(chunk["text"], query_vector.shape[1]) for chunk in chunks])

        chunk_to_index = {chunk["chunk_id"]: index for index, chunk in enumerate(chunks)}
        candidates = []
        for chunk in filtered_chunks:
            index = chunk_to_index.get(chunk["chunk_id"])
            if index is None:
                continue
            score = float(np.dot(embeddings[index], query_vector[0]))
            if not math.isfinite(score):
                score = 0.0
            candidates.append((score, chunk))
        candidates.sort(key=lambda item: item[0], reverse=True)
        candidates = candidates[:top_k]

    if not candidates:
        return "未查询到可信媒体观察证据：没有命中相似媒体记录。"

    with custom_span(
        "media_rerank",
        {
            "candidate_count": len(candidates),
            "top_n": top_n,
            "provider": settings.media_rag_provider,
            "rerank_model": settings.media_rag_rerank_model,
        },
    ):
        candidate_chunks = [chunk for _, chunk in candidates]
        rerank_scores = _bailian_vl_rerank(settings, query, candidate_chunks) if settings.media_rag_provider == "bailian_vl" else None
        if rerank_scores is None and settings.media_rag_require_vl_models:
            return (
                "未查询到可信媒体观察证据：媒体证据 qwen3-vl-rerank 服务不可用，"
                "且配置禁止本机 fallback。"
            )
        results: list[MediaSearchResult] = []
        for similarity_score, chunk in candidates:
            lexical_bonus = len(set(_tokenize(query)) & set(_tokenize(chunk.get("text", "")))) / 100
            rerank_score = rerank_scores.get(chunk["chunk_id"], similarity_score + lexical_bonus) if rerank_scores else similarity_score + lexical_bonus
            results.append(
                MediaSearchResult(
                    chunk=chunk,
                    similarity_score=similarity_score,
                    rerank_score=float(rerank_score),
                    matched_reasons=_matched_reasons(query, chunk, product_model),
                )
            )
        results.sort(key=lambda result: result.rerank_score, reverse=True)
        results = results[:top_n]

    with custom_span(
        "media_result_pack",
        {
            "result_count": len(results),
            "topic_id_hashes": [_short_hash(result.chunk.get("topic_id", "")) for result in results],
            "chunk_id_hashes": [_short_hash(result.chunk.get("chunk_id", "")) for result in results],
            "evidence_level": "media_observation_unreviewed",
        },
    ):
        return "\n".join(
            [
                "命中未审核媒体观察证据。以下内容来自飞书 raw media 索引，只能说明存在图片/视频/截图等待核验证据，需人工打开原话题确认，不能作为正式依据：",
                *[_format_result(result) for result in results],
            ]
        )
