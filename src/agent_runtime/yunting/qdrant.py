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

    def upsert(self, collection: str, points: list[QdrantPoint]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        response = httpx.put(
            f"{self.url}/collections/{collection}/points",
            headers=headers,
            json={"points": [{"id": point.id, "vector": point.vector, "payload": point.payload} for point in points]},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()
