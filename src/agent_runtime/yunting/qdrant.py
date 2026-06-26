from __future__ import annotations

import json
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


def text_points_from_ads(rows: list[dict[str, Any]], *, mock_dimension: int = 8) -> list[QdrantPoint]:
    points: list[QdrantPoint] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload["embedding_text_hash"] = row["embedding_text_hash"]
        points.append(QdrantPoint(id=row["point_id"], vector=mock_vector(row["embedding_text"], mock_dimension), payload=payload))
    return points


def media_points_from_ads(rows: list[dict[str, Any]], *, mock_dimension: int = 8) -> list[QdrantPoint]:
    points: list[QdrantPoint] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
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
            current_size = payload.get("result", {}).get("config", {}).get("params", {}).get("vectors", {}).get("size")
            if current_size is not None and int(current_size) != vector_size:
                raise RuntimeError(f"Qdrant collection {collection} has vector size {current_size}, expected {vector_size}")
            return {"collection": collection, "created": False, "vector_size": vector_size}
        if response.status_code != 404:
            response.raise_for_status()
        create = httpx.put(
            f"{self.url}/collections/{collection}",
            headers=headers,
            json={"vectors": {"size": vector_size, "distance": distance}},
            timeout=120,
        )
        create.raise_for_status()
        return {"collection": collection, "created": True, "vector_size": vector_size, "result": create.json()}

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
