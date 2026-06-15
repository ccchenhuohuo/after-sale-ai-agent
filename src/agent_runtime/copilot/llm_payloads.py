from __future__ import annotations
from typing import Any

from agent_runtime.copilot.case_context import (
    IngestionArtifact,
    SupportAsset,
    SupportCaseRequest,
)
from agent_runtime.copilot.reference_safety import redact_internal_references


SAFE_ASSET_METADATA_KEYS = frozenset(
    {
        "asset_role",
        "description",
        "source_type",
        "low_quality",
        "rich_tag",
        "feishu_message_type",
    }
)


def safe_asset_payload_for_llm(asset: SupportAsset) -> dict[str, object]:
    return {
        "asset_id": asset.asset_id,
        "media_type": asset.media_type,
        "filename": asset.filename,
        "mime_type": asset.mime_type,
        "safe_metadata": _safe_asset_metadata(asset.metadata),
    }


def safe_request_payload_for_llm(request: SupportCaseRequest) -> dict[str, object]:
    return {
        "request_id": request.request_id,
        "source": request.source,
        "user_text": request.user_text,
        "assets": [safe_asset_payload_for_llm(asset) for asset in request.assets],
    }


def safe_artifact_payload_for_llm(artifact: IngestionArtifact) -> dict[str, object]:
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "status": artifact.status,
        "asset_id": artifact.asset_id,
        "text": artifact.text,
        "summary": artifact.summary,
        "vector_id": artifact.vector_id,
        "model_name": artifact.model_name,
        "index_namespace": artifact.index_namespace,
        "error": _safe_error_for_llm(artifact.error),
    }


def _safe_asset_metadata(metadata: dict[str, Any]) -> dict[str, object]:
    return {key: value for key, value in metadata.items() if key in SAFE_ASSET_METADATA_KEYS}


def _safe_error_for_llm(error: str) -> str:
    return redact_internal_references(error, max_chars=300)
