from __future__ import annotations

import json
import math
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
DEFAULT_DIMENSION = 768
REMOTE_TEXT_MAX_CHARS = 1200
SKU_RE = re.compile(r"\b[A-Z]{1,4}\d{2,5}[A-Z0-9-]*\b")


@dataclass(frozen=True)
class FormalKbSearchResult:
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
    tokens: list[str] = []
    tokens.extend(SKU_RE.findall(normalized))
    tokens.extend(re.findall(r"[A-Z0-9-]{2,}", normalized))
    chinese = re.findall(r"[\u4e00-\u9fff]", normalized)
    tokens.extend(chinese)
    tokens.extend("".join(pair) for pair in zip(chinese, chinese[1:]))
    return [token for token in tokens if token]


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


def _short_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


def _auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


def _bailian_endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _bailian_embeddings(settings: Settings, texts: list[str], timeout: float = 60.0) -> np.ndarray | None:
    api_key = settings.resolved_bailian_api_key
    if not api_key:
        return None
    try:
        prepared = [_clean(text)[:REMOTE_TEXT_MAX_CHARS] for text in texts]
        response = httpx.post(
            _bailian_endpoint(settings.bailian_embedding_base_url, "embeddings"),
            json={"model": settings.formal_kb_embedding_model, "input": prepared},
            headers=_auth_headers(api_key),
            timeout=timeout,
        )
        response.raise_for_status()
        embeddings = _extract_embedding_payload(response.json())
    except Exception:
        return None
    if len(embeddings) != len(texts):
        return None
    array = np.asarray(embeddings, dtype=np.float32)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return array / norms


def _query_embeddings(settings: Settings, texts: list[str]) -> np.ndarray | None:
    if settings.formal_kb_provider == "bailian":
        return _bailian_embeddings(settings, texts)
    return None


def _bailian_rerank(settings: Settings, query: str, chunks: list[dict[str, Any]], timeout: float = 60.0) -> dict[str, float] | None:
    api_key = settings.resolved_bailian_api_key
    if not api_key:
        return None
    documents = [_clean(chunk.get("text"))[:REMOTE_TEXT_MAX_CHARS] for chunk in chunks]
    try:
        response = httpx.post(
            _bailian_endpoint(settings.bailian_rerank_base_url, "reranks"),
            json={
                "model": settings.formal_kb_rerank_model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
                "instruct": "Retrieve official after-sales support knowledge.",
            },
            headers=_auth_headers(api_key),
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    results = payload.get("results") if isinstance(payload, dict) else None
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


def formal_kb_index_available(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    index_dir = _path(settings.formal_kb_index_path)
    chunks = _load_jsonl(index_dir / "formal_chunks.jsonl")
    embeddings = _load_embeddings(index_dir / "embeddings.npy")
    return bool(chunks) and embeddings is not None and embeddings.ndim == 2 and embeddings.shape[0] == len(chunks)


def _filter_chunks(
    chunks: Iterable[dict[str, Any]],
    product_model: str | None,
    module: str | None,
    issue_type: str | None,
) -> tuple[list[dict[str, Any]], str]:
    rows = list(chunks)
    product = _clean(product_model).upper()
    status = "no_product_filter"
    if product:
        matched = [
            chunk
            for chunk in rows
            if product in f"{chunk.get('product_model', '')} {chunk.get('sku', '')} {chunk.get('text', '')}".upper()
        ]
        if matched:
            rows = matched
            status = "product_filter_matched"
        else:
            return [], "product_filter_no_match"
    filter_text = " ".join(_clean(value) for value in (module, issue_type) if _clean(value))
    if filter_text:
        matched = [
            chunk
            for chunk in rows
            if any(token in f"{chunk.get('source_type', '')} {chunk.get('section', '')} {chunk.get('text', '')}" for token in filter_text.split())
        ]
        if matched:
            rows = matched
            status = f"{status}+topic_filter_matched"
    return rows, status


def _matched_reasons(query: str, chunk: dict[str, Any], product_model: str | None) -> list[str]:
    text = (
        f"{chunk.get('text', '')} {chunk.get('product_model', '')} {chunk.get('sku', '')} "
        f"{chunk.get('source_type', '')} {chunk.get('title', '')} {chunk.get('section', '')}"
    ).upper()
    reasons: list[str] = []
    if product_model and product_model.upper() in text:
        reasons.append("SKU/型号过滤命中")
    for sku in SKU_RE.findall(query.upper()):
        if sku in text:
            reasons.append(f"查询SKU命中：{sku}")
    overlap = sorted(set(_tokenize(query)) & set(_tokenize(text)))
    if overlap:
        reasons.append(f"关键词重叠：{'、'.join(overlap[:5])}")
    return reasons or ["正式源语义相似候选"]


def _format_result(result: FormalKbSearchResult) -> str:
    chunk = result.chunk
    snippet = _clean(chunk.get("text"))[:260]
    return "\n".join(
        [
            f"- 来源ID：{chunk.get('source_id', '')}",
            f"  标题：{chunk.get('title') or '未命名正式资料'}",
            f"  类型：{chunk.get('source_type') or 'official_kb'}",
            f"  章节：{chunk.get('section') or '未标注章节'}",
            f"  SKU/型号：{chunk.get('product_model') or chunk.get('sku') or '未限定'}",
            f"  摘要：{snippet}",
            f"  相似原因：{'；'.join(result.matched_reasons)}",
            f"  链接：{chunk.get('source_url', '')}",
            f"  相似度：{result.similarity_score:.4f}",
            f"  重排分：{result.rerank_score:.4f}",
            "  审核状态：正式依据，verified=true。",
        ]
    )


def search_formal_kb(
    query: str,
    *,
    product_model: str | None = None,
    module: str | None = None,
    issue_type: str | None = None,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    index_dir = _path(settings.formal_kb_index_path)
    chunks = _load_jsonl(index_dir / "formal_chunks.jsonl")
    if not chunks:
        return f"未查询到可信正式依据：正式 KB/MRD/手册索引不存在或为空（{index_dir}）。"

    filtered_chunks, filter_status = _filter_chunks(chunks, product_model, module, issue_type)
    if product_model and filter_status == "product_filter_no_match":
        return f"未查询到可信正式依据：正式资料索引中没有命中同 SKU/型号（{_clean(product_model)}）的内容。"

    top_k = max(1, settings.formal_kb_top_k)
    top_n = max(1, settings.formal_kb_top_n)

    with custom_span(
        "formal_kb_embedding_search",
        {
            "query_hash": _short_hash(query),
            "query_chars": len(query),
            "product_model_hash": _short_hash(product_model or "") if product_model else "",
            "chunk_count": len(chunks),
            "filtered_chunk_count": len(filtered_chunks),
            "filter_status": filter_status,
            "top_k": top_k,
            "provider": settings.formal_kb_provider,
            "embedding_model": settings.formal_kb_embedding_model,
        },
    ):
        query_vector = _query_embeddings(settings, [query])
        if query_vector is None and settings.formal_kb_require_remote_models:
            return (
                "未查询到可信正式依据：正式 KB Embedding 服务不可用，"
                f"当前 provider={settings.formal_kb_provider}，且配置禁止本机模型 fallback。"
            )
        if query_vector is None:
            query_vector = np.vstack([_hashed_embedding(query, settings.formal_kb_embedding_dimension)])

        embeddings = _load_embeddings(index_dir / "embeddings.npy")
        if embeddings is None or embeddings.shape[0] != len(chunks) or embeddings.shape[1] != query_vector.shape[1]:
            if settings.formal_kb_require_remote_models:
                chunk_vectors = _query_embeddings(settings, [chunk["text"] for chunk in chunks])
                if chunk_vectors is None:
                    return "未查询到可信正式依据：正式 KB Embedding 服务不可用，且索引向量缺失或维度不匹配。"
                embeddings = chunk_vectors
            else:
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
        return "未查询到可信正式依据：正式 KB/MRD/手册没有命中相似内容。"

    with custom_span(
        "formal_kb_rerank",
        {
            "candidate_count": len(candidates),
            "top_n": top_n,
            "provider": settings.formal_kb_provider,
            "rerank_model": settings.formal_kb_rerank_model,
        },
    ):
        candidate_chunks = [chunk for _, chunk in candidates]
        rerank_scores = _bailian_rerank(settings, query, candidate_chunks) if settings.formal_kb_provider == "bailian" else None
        if rerank_scores is None and settings.formal_kb_require_remote_models:
            return (
                "未查询到可信正式依据：正式 KB Reranker 服务不可用，"
                f"当前 provider={settings.formal_kb_provider}，且配置禁止本机 rerank fallback。"
            )

        results: list[FormalKbSearchResult] = []
        for similarity_score, chunk in candidates:
            lexical_bonus = len(set(_tokenize(query)) & set(_tokenize(chunk.get("text", "")))) / 100
            rerank_score = rerank_scores.get(chunk["chunk_id"], similarity_score + lexical_bonus) if rerank_scores else similarity_score + lexical_bonus
            results.append(
                FormalKbSearchResult(
                    chunk=chunk,
                    similarity_score=similarity_score,
                    rerank_score=float(rerank_score),
                    matched_reasons=_matched_reasons(query, chunk, product_model),
                )
            )
        results.sort(key=lambda result: result.rerank_score, reverse=True)
        results = results[:top_n]

    with custom_span(
        "formal_kb_result_pack",
        {
            "result_count": len(results),
            "source_id_hashes": [_short_hash(result.chunk.get("source_id", "")) for result in results],
            "chunk_id_hashes": [_short_hash(result.chunk.get("chunk_id", "")) for result in results],
            "evidence_level": "formal",
            "is_reviewed": True,
        },
    ):
        return "\n".join(
            [
                "命中正式依据。以下内容来自正式 KB/MRD/手册/政策文件索引，可作为正式依据：",
                *[_format_result(result) for result in results],
            ]
        )
