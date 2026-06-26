from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np


@dataclass(frozen=True)
class QdrantPoint:
    id: str
    vector: list[float]
    payload: dict[str, Any]


def mock_vector(text: str, dimension: int) -> list[float]:
    vector = np.zeros(dimension, dtype=np.float32)
    for index, byte in enumerate(text.encode("utf-8")):
        vector[(byte + index) % dimension] += 1.0
    norm = np.linalg.norm(vector)
    if norm:
        vector /= norm
    return vector.tolist()


def _payload_with_backend(row: dict[str, Any], *, backend: str, model: str, is_semantic: bool) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    payload["embedding_backend"] = backend
    payload["embedding_model"] = model
    payload["is_semantic_vector"] = is_semantic
    return payload


def text_points_from_ads(rows: list[dict[str, Any]], *, mock_dimension: int = 8) -> list[QdrantPoint]:
    points: list[QdrantPoint] = []
    for row in rows:
        payload = _payload_with_backend(row, backend="mock", model="mock-deterministic-vector", is_semantic=False)
        payload["embedding_text_hash"] = row["embedding_text_hash"]
        points.append(QdrantPoint(id=row["point_id"], vector=mock_vector(row["embedding_text"], mock_dimension), payload=payload))
    return points


def media_points_from_ads(rows: list[dict[str, Any]], *, mock_dimension: int = 8) -> list[QdrantPoint]:
    points: list[QdrantPoint] = []
    for row in rows:
        payload = _payload_with_backend(row, backend="mock", model="mock-deterministic-vector", is_semantic=False)
        payload["media_object_key"] = row.get("media_object_key", "")
        vector_seed = " ".join(
            str(part or "")
            for part in (
                payload.get("message_type"),
                payload.get("content_id"),
                payload.get("asset_id"),
                row.get("media_object_key"),
            )
        )
        points.append(QdrantPoint(id=row["point_id"], vector=mock_vector(vector_seed, mock_dimension), payload=payload))
    return points


def _validate_ads_rows(
    rows: list[dict[str, Any]],
    *,
    collection: str,
    vector_model: str,
    vector_dimension: int,
) -> None:
    for row in rows:
        if row.get("collection_name") != collection:
            raise ValueError(f"ADS row collection_name={row.get('collection_name')} does not match {collection}")
        if row.get("vector_model") != vector_model:
            raise ValueError(f"ADS row vector_model={row.get('vector_model')} does not match {vector_model}")
        if int(row.get("vector_dimension") or 0) != vector_dimension:
            raise ValueError(f"ADS row vector_dimension={row.get('vector_dimension')} does not match {vector_dimension}")


def text_points_from_vectors(
    rows: list[dict[str, Any]],
    vectors: list[list[float]],
    *,
    collection: str,
    vector_model: str,
    vector_dimension: int,
    backend: str,
) -> list[QdrantPoint]:
    if len(rows) != len(vectors):
        raise ValueError(f"Embedding row/vector count mismatch: rows={len(rows)}, vectors={len(vectors)}")
    _validate_ads_rows(rows, collection=collection, vector_model=vector_model, vector_dimension=vector_dimension)
    points: list[QdrantPoint] = []
    for row, vector in zip(rows, vectors, strict=True):
        if len(vector) != vector_dimension:
            raise ValueError(f"Vector length {len(vector)} does not match expected dimension {vector_dimension}")
        payload = _payload_with_backend(row, backend=backend, model=vector_model, is_semantic=True)
        payload["embedding_text_hash"] = row["embedding_text_hash"]
        points.append(QdrantPoint(id=row["point_id"], vector=vector, payload=payload))
    return points


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dimension: int,
        backend: str = "openai-compatible",
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.dimension = dimension
        self.backend = backend
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "OpenAICompatibleEmbeddingProvider":
        api_key = (
            os.getenv("YUNTING_TEXT_EMBEDDING_API_KEY")
            or os.getenv("BAILIAN_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        )
        base_url = os.getenv("YUNTING_TEXT_EMBEDDING_BASE_URL") or os.getenv("BAILIAN_EMBEDDING_BASE_URL") or ""
        model = os.getenv("YUNTING_TEXT_EMBEDDING_MODEL", "text-embedding-v4")
        dimension = int(os.getenv("YUNTING_TEXT_EMBEDDING_DIMENSION", "1024"))
        backend = os.getenv("YUNTING_TEXT_EMBEDDING_BACKEND", "openai-compatible")
        timeout_seconds = float(os.getenv("YUNTING_TEXT_EMBEDDING_TIMEOUT_SECONDS", "60"))
        if not api_key or not base_url:
            raise RuntimeError("Configure YUNTING_TEXT_EMBEDDING_API_KEY and YUNTING_TEXT_EMBEDDING_BASE_URL for production Qdrant upsert.")
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            dimension=dimension,
            backend=backend,
            timeout_seconds=timeout_seconds,
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout_seconds)
        response = client.embeddings.create(model=self.model, input=texts)
        vectors = [list(item.embedding) for item in response.data]
        for vector in vectors:
            if len(vector) != self.dimension:
                raise RuntimeError(f"Embedding provider returned dimension {len(vector)}, expected {self.dimension}")
        return vectors


class QdrantAdapter:
    def __init__(self, *, url: str, api_key: str = "") -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        return headers

    def ensure_collection(self, collection: str, *, vector_size: int, distance: str = "Cosine") -> dict[str, Any]:
        headers = self._headers()
        response = httpx.get(f"{self.url}/collections/{collection}", headers=headers, timeout=30)
        if response.status_code == 200:
            payload = response.json()
            vectors = payload.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
            if isinstance(vectors, dict) and "size" not in vectors:
                raise RuntimeError(f"Qdrant collection {collection} uses unsupported named vector schema")
            current_size = vectors.get("size") if isinstance(vectors, dict) else None
            current_distance = vectors.get("distance") if isinstance(vectors, dict) else None
            if current_size is not None and int(current_size) != vector_size:
                raise RuntimeError(f"Qdrant collection {collection} has vector size {current_size}, expected {vector_size}")
            if current_distance is not None and str(current_distance).lower() != distance.lower():
                raise RuntimeError(f"Qdrant collection {collection} has distance {current_distance}, expected {distance}")
            return {"collection": collection, "created": False, "vector_size": vector_size, "distance": distance}
        if response.status_code != 404:
            response.raise_for_status()
        create = httpx.put(
            f"{self.url}/collections/{collection}",
            headers=headers,
            json={"vectors": {"size": vector_size, "distance": distance}},
            timeout=120,
        )
        create.raise_for_status()
        return {"collection": collection, "created": True, "vector_size": vector_size, "distance": distance, "result": create.json()}

    def ensure_keyword_payload_index(self, collection: str, field_name: str) -> dict[str, Any]:
        response = httpx.put(
            f"{self.url}/collections/{collection}/index",
            headers=self._headers(),
            json={"field_name": field_name, "field_schema": "keyword"},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()

    def dry_run_upsert(self, collection: str, points: list[QdrantPoint]) -> dict[str, Any]:
        return {
            "collection": collection,
            "operation": "upsert",
            "point_count": len(points),
            "point_ids": [point.id for point in points],
            "dry_run": True,
        }

    def dry_run_delete_by_unique_id(self, collection: str, unique_id: str) -> dict[str, Any]:
        return {
            "collection": collection,
            "operation": "delete",
            "filter": {"must": [{"key": "unique_id", "match": {"value": unique_id}}]},
            "dry_run": True,
        }

    def dry_run_delete_stale_by_unique_id(self, collection: str, unique_id: str, data_version: str) -> dict[str, Any]:
        return {
            "collection": collection,
            "operation": "delete",
            "filter": {
                "must": [{"key": "unique_id", "match": {"value": unique_id}}],
                "must_not": [{"key": "data_version", "match": {"value": data_version}}],
            },
            "dry_run": True,
        }

    def count_by_data_version(self, collection: str, data_version: str) -> int:
        response = httpx.post(
            f"{self.url}/collections/{collection}/points/count",
            headers=self._headers(),
            json={
                "exact": True,
                "filter": {"must": [{"key": "data_version", "match": {"value": data_version}}]},
            },
            timeout=120,
        )
        response.raise_for_status()
        return int(response.json().get("result", {}).get("count", 0) or 0)

    def delete_by_unique_id(self, collection: str, unique_id: str) -> dict[str, Any]:
        response = httpx.post(
            f"{self.url}/collections/{collection}/points/delete",
            params={"wait": "true"},
            headers=self._headers(),
            json={"filter": {"must": [{"key": "unique_id", "match": {"value": unique_id}}]}},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()

    def delete_stale_by_unique_id(self, collection: str, unique_id: str, data_version: str) -> dict[str, Any]:
        response = httpx.post(
            f"{self.url}/collections/{collection}/points/delete",
            params={"wait": "true"},
            headers=self._headers(),
            json={
                "filter": {
                    "must": [{"key": "unique_id", "match": {"value": unique_id}}],
                    "must_not": [{"key": "data_version", "match": {"value": data_version}}],
                }
            },
            timeout=120,
        )
        response.raise_for_status()
        return response.json()

    def upsert(self, collection: str, points: list[QdrantPoint]) -> dict[str, Any]:
        response = httpx.put(
            f"{self.url}/collections/{collection}/points",
            params={"wait": "true"},
            headers=self._headers(),
            json={"points": [{"id": point.id, "vector": point.vector, "payload": point.payload} for point in points]},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()
