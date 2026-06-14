from __future__ import annotations

import logging
import base64
import mimetypes
from pathlib import Path

import numpy as np
from agents import custom_span

from agent_runtime.copilot.case_context import (
    AssetRouteDecision,
    IngestionArtifact,
    RouteDecision,
    SupportAsset,
    SupportCaseRequest,
)
from agent_runtime.copilot.evidence import short_hash
from agent_runtime.settings import Settings
from agent_runtime.tools.media_rag import _bailian_vl_embedding


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]


async def ingest_support_case(
    request: SupportCaseRequest,
    route: RouteDecision,
    settings: Settings,
) -> list[IngestionArtifact]:
    artifacts: list[IngestionArtifact] = []
    artifacts.append(_ingest_text(request))
    decisions = {decision.asset_id: decision for decision in route.asset_decisions}
    for asset in request.assets:
        decision = decisions.get(asset.asset_id)
        if decision is None:
            artifacts.append(_metadata_artifact(asset, "error", "缺少附件路由决策。"))
            continue
        artifacts.append(_metadata_artifact(asset, "ok", decision.reason, decision))
        if decision.requires_ocr:
            artifacts.append(_ingest_ocr(asset, decision, settings))
        if decision.requires_visual_embedding:
            artifacts.append(_ingest_visual_embedding(asset, decision, settings))
        if decision.requires_video_sampling:
            artifacts.append(_ingest_video_sampling(asset, decision, settings))
    return artifacts


def _ingest_text(request: SupportCaseRequest) -> IngestionArtifact:
    text = " ".join((request.user_text or "").split())
    if not text:
        return IngestionArtifact(
            artifact_id=f"text:{request.request_id}",
            artifact_type="text",
            status="empty",
            source_text_hash="",
            summary="用户未提供文本描述。",
        )
    return IngestionArtifact(
        artifact_id=f"text:{short_hash(request.request_id + text)}",
        artifact_type="text",
        status="ok",
        text=text,
        summary="用户文本已进入上下文。",
        source_text_hash=short_hash(text),
        metadata={"source": request.source},
    )


def _metadata_artifact(
    asset: SupportAsset,
    status: str,
    summary: str,
    decision: AssetRouteDecision | None = None,
) -> IngestionArtifact:
    metadata = {
        "media_type": asset.media_type,
        "filename": asset.filename,
        "mime_type": asset.mime_type,
        "file_key": asset.file_key,
    }
    if decision is not None:
        metadata.update(
            {
                "asset_role": decision.asset_role,
                "requires_ocr": decision.requires_ocr,
                "requires_visual_embedding": decision.requires_visual_embedding,
                "requires_video_sampling": decision.requires_video_sampling,
            }
        )
    return IngestionArtifact(
        artifact_id=f"metadata:{asset.asset_id}",
        artifact_type="metadata",
        status=status,
        asset_id=asset.asset_id,
        summary=summary,
        metadata=metadata,
    )


def _ingest_ocr(asset: SupportAsset, decision: AssetRouteDecision, settings: Settings) -> IngestionArtifact:
    provided_text = str(asset.metadata.get("ocr_text") or asset.metadata.get("text_hint") or "").strip()
    if provided_text:
        return IngestionArtifact(
            artifact_id=f"ocr:{asset.asset_id}:{short_hash(provided_text)}",
            artifact_type="ocr",
            status="ok",
            asset_id=asset.asset_id,
            text=provided_text,
            summary="已从附件元数据读取 OCR 文本。",
            source_text_hash=short_hash(provided_text),
            model_name="metadata-provided-ocr",
        )
    if settings.support_ocr_provider == "disabled":
        return IngestionArtifact(
            artifact_id=f"ocr:{asset.asset_id}:disabled",
            artifact_type="ocr",
            status="unsupported",
            asset_id=asset.asset_id,
            summary="OCR provider 未启用，图片文字暂未识别。",
            model_name=settings.support_ocr_provider,
            metadata={"asset_role": decision.asset_role},
        )
    return IngestionArtifact(
        artifact_id=f"ocr:{asset.asset_id}:unavailable",
        artifact_type="ocr",
        status="unsupported",
        asset_id=asset.asset_id,
        summary="当前 OCR provider 尚未接入可用实现，图片文字暂未识别。",
        model_name=settings.support_ocr_provider,
        metadata={"asset_role": decision.asset_role},
    )


def _ingest_visual_embedding(
    asset: SupportAsset,
    decision: AssetRouteDecision,
    settings: Settings,
) -> IngestionArtifact:
    provided_vector_id = str(asset.metadata.get("vector_id") or "").strip()
    provided_summary = str(asset.metadata.get("visual_summary") or "").strip()
    if provided_vector_id:
        return IngestionArtifact(
            artifact_id=f"visual:{asset.asset_id}:{provided_vector_id}",
            artifact_type="image_embedding",
            status="ok",
            asset_id=asset.asset_id,
            summary=provided_summary or "已从附件元数据读取视觉向量引用。",
            vector_id=provided_vector_id,
            model_name=str(asset.metadata.get("embedding_model") or "metadata-provided-vector"),
            index_namespace=settings.support_vector_index_namespace,
            metadata={"asset_role": decision.asset_role},
        )

    content = _visual_content(asset)
    if content is None:
        return IngestionArtifact(
            artifact_id=f"visual:{asset.asset_id}:unsupported",
            artifact_type="image_embedding",
            status="unsupported",
            asset_id=asset.asset_id,
            summary="附件缺少可用于视觉向量化的 URL 或本地文件。",
            model_name=settings.media_rag_embedding_model,
            index_namespace=settings.support_vector_index_namespace,
            metadata={"asset_role": decision.asset_role},
        )

    vector = _generate_visual_vector(settings, content)
    if vector is None:
        return IngestionArtifact(
            artifact_id=f"visual:{asset.asset_id}:unavailable",
            artifact_type="image_embedding",
            status="unsupported",
            asset_id=asset.asset_id,
            summary="视觉 embedding 服务不可用，未生成向量引用。",
            model_name=settings.media_rag_embedding_model,
            index_namespace=settings.support_vector_index_namespace,
            metadata={"asset_role": decision.asset_role},
        )

    vector_id = _persist_vector(settings, asset, vector)
    return IngestionArtifact(
        artifact_id=f"visual:{asset.asset_id}:{vector_id}",
        artifact_type="image_embedding",
        status="ok",
        asset_id=asset.asset_id,
        summary=provided_summary or f"已生成 {decision.asset_role} 视觉向量引用。",
        vector_id=vector_id,
        model_name=settings.media_rag_embedding_model,
        index_namespace=settings.support_vector_index_namespace,
        metadata={"asset_role": decision.asset_role},
    )


def _ingest_video_sampling(
    asset: SupportAsset,
    decision: AssetRouteDecision,
    settings: Settings,
) -> IngestionArtifact:
    provided_summary = str(asset.metadata.get("video_summary") or asset.metadata.get("visual_summary") or "").strip()
    if provided_summary:
        return IngestionArtifact(
            artifact_id=f"video:{asset.asset_id}:{short_hash(provided_summary)}",
            artifact_type="video_sampling",
            status="ok",
            asset_id=asset.asset_id,
            summary=provided_summary,
            model_name=settings.media_rag_embedding_model,
            metadata={"asset_role": decision.asset_role},
        )
    return IngestionArtifact(
        artifact_id=f"video:{asset.asset_id}:unsupported",
        artifact_type="video_sampling",
        status="unsupported",
        asset_id=asset.asset_id,
        summary="视频采样接口已预留，但当前未完成可用的视频解析实现。",
        model_name=settings.media_rag_embedding_model,
        metadata={"asset_role": decision.asset_role},
    )


def _visual_content(asset: SupportAsset) -> dict[str, str] | None:
    if asset.url:
        if asset.media_type == "video":
            return {"video": asset.url}
        return {"image": asset.url}
    if not asset.local_path:
        return None
    path = Path(asset.local_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return None
    if asset.media_type == "video":
        return {"video": str(path)}
    return {"image": _image_data_uri(path)}


def _generate_visual_vector(settings: Settings, content: dict[str, str]) -> np.ndarray | None:
    if not settings.resolved_bailian_api_key:
        return None
    try:
        with custom_span(
            "ingestion_visual_embedding",
            {
                "provider": settings.media_rag_provider,
                "embedding_model": settings.media_rag_embedding_model,
                "content_type": next(iter(content.keys())),
            },
        ):
            return _bailian_vl_embedding(settings, [content])
    except Exception as exc:
        logger.warning("Visual embedding failed: %s", exc)
        return None


def _persist_vector(settings: Settings, asset: SupportAsset, vector: np.ndarray) -> str:
    vector_id = f"vec_{short_hash(asset.asset_id + asset.media_type + asset.file_key + asset.url)}"
    directory = Path(settings.support_vector_artifact_dir)
    if not directory.is_absolute():
        directory = ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    np.save(directory / f"{vector_id}.npy", vector)
    return vector_id


def _image_data_uri(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"
