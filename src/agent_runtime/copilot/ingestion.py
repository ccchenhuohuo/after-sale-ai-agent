from __future__ import annotations

import logging
import base64
import mimetypes
from pathlib import Path

import numpy as np
from agents import custom_span

from agent_runtime.copilot.asset_inputs import AssetInputValidationResult, validate_support_asset_input
from agent_runtime.copilot.case_context import (
    AssetRouteDecision,
    IngestionArtifact,
    RouteDecision,
    SupportAsset,
    SupportCaseRequest,
)
from agent_runtime.copilot.evidence import short_hash
from agent_runtime.copilot.ocr import extract_ocr_text
from agent_runtime.copilot.video_sampling import sample_video_frames
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
    ocr_input = validate_support_asset_input(asset, settings, expected_kind="image", allow_url=True)
    if not ocr_input.ok:
        return IngestionArtifact(
            artifact_id=f"ocr:{asset.asset_id}:invalid-input",
            artifact_type="ocr",
            status="unsupported",
            asset_id=asset.asset_id,
            summary=ocr_input.error or "附件缺少可用于 OCR 的本地文件或白名单 URL。",
            model_name=settings.support_ocr_model,
            metadata={"asset_role": decision.asset_role},
        )
    with custom_span(
        "ingestion_ocr",
        {
            "provider": settings.support_ocr_provider,
            "model": settings.support_ocr_model,
            "asset_role": decision.asset_role,
        },
    ):
        result = extract_ocr_text(ocr_input.value, settings)
    if result.status == "ok":
        return IngestionArtifact(
            artifact_id=f"ocr:{asset.asset_id}:{short_hash(result.text)}",
            artifact_type="ocr",
            status="ok",
            asset_id=asset.asset_id,
            text=result.text,
            summary="已完成图片 OCR 识别。",
            source_text_hash=short_hash(result.text),
            model_name=result.model_name,
            metadata={"asset_role": decision.asset_role, "source_kind": result.source_kind},
        )
    if result.status == "empty":
        return IngestionArtifact(
            artifact_id=f"ocr:{asset.asset_id}:empty",
            artifact_type="ocr",
            status="empty",
            asset_id=asset.asset_id,
            summary="OCR 未识别到可读文字。",
            model_name=result.model_name,
            metadata={"asset_role": decision.asset_role, "source_kind": result.source_kind},
        )
    if result.status == "error":
        return IngestionArtifact(
            artifact_id=f"ocr:{asset.asset_id}:error",
            artifact_type="ocr",
            status="error",
            asset_id=asset.asset_id,
            summary="OCR provider 调用失败，图片文字暂未识别。",
            model_name=result.model_name,
            error=result.error,
            metadata={"asset_role": decision.asset_role, "source_kind": result.source_kind},
        )
    return IngestionArtifact(
        artifact_id=f"ocr:{asset.asset_id}:unavailable",
        artifact_type="ocr",
        status="unsupported",
        asset_id=asset.asset_id,
        summary=result.error or "当前 OCR provider 尚未接入可用实现，图片文字暂未识别。",
        model_name=result.model_name or settings.support_ocr_provider,
        metadata={"asset_role": decision.asset_role, "source_kind": result.source_kind},
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

    if asset.media_type == "video":
        return IngestionArtifact(
            artifact_id=f"visual:{asset.asset_id}:video-direct-unsupported",
            artifact_type="image_embedding",
            status="unsupported",
            asset_id=asset.asset_id,
            summary="视频视觉 embedding v1 不直接处理原视频；需等待关键帧向量化能力接入。",
            model_name=settings.media_rag_embedding_model,
            index_namespace=settings.support_vector_index_namespace,
            metadata={"asset_role": decision.asset_role},
        )

    visual_input = validate_support_asset_input(asset, settings, expected_kind="image", allow_url=True)
    if not visual_input.ok:
        return IngestionArtifact(
            artifact_id=f"visual:{asset.asset_id}:invalid-input",
            artifact_type="image_embedding",
            status="unsupported",
            asset_id=asset.asset_id,
            summary=visual_input.error or "附件缺少可用于视觉向量化的本地文件或白名单 URL。",
            model_name=settings.media_rag_embedding_model,
            index_namespace=settings.support_vector_index_namespace,
            metadata={"asset_role": decision.asset_role},
        )

    content = _visual_content(visual_input)
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
    video_input = validate_support_asset_input(
        asset,
        settings,
        expected_kind="video",
        allow_url=False,
        local_only=True,
    )
    if not video_input.ok:
        return IngestionArtifact(
            artifact_id=f"video:{asset.asset_id}:invalid-input",
            artifact_type="video_sampling",
            status="unsupported",
            asset_id=asset.asset_id,
            summary=video_input.error or "附件缺少可用于视频采样的白名单本地文件。",
            model_name="ffmpeg",
            metadata={"asset_role": decision.asset_role},
        )
    sampling = sample_video_frames(video_input.value, asset.asset_id, settings)
    if sampling.status == "ok":
        frame_count = len(sampling.frame_paths)
        return IngestionArtifact(
            artifact_id=f"video:{asset.asset_id}:{short_hash('|'.join(sampling.frame_paths))}",
            artifact_type="video_sampling",
            status="ok",
            asset_id=asset.asset_id,
            summary=f"已从视频中采样 {frame_count} 张关键帧，供视觉检索或人工复核使用。",
            model_name=sampling.model_name,
            metadata={"asset_role": decision.asset_role, "frame_paths": sampling.frame_paths, "frame_count": frame_count},
        )
    if sampling.status == "empty":
        return IngestionArtifact(
            artifact_id=f"video:{asset.asset_id}:empty",
            artifact_type="video_sampling",
            status="empty",
            asset_id=asset.asset_id,
            summary="视频采样未生成可用关键帧。",
            model_name=sampling.model_name,
            metadata={"asset_role": decision.asset_role},
        )
    if sampling.status == "error":
        return IngestionArtifact(
            artifact_id=f"video:{asset.asset_id}:error",
            artifact_type="video_sampling",
            status="error",
            asset_id=asset.asset_id,
            summary="视频采样失败。",
            model_name=sampling.model_name,
            error=sampling.error,
            metadata={"asset_role": decision.asset_role},
        )
    return IngestionArtifact(
        artifact_id=f"video:{asset.asset_id}:unsupported",
        artifact_type="video_sampling",
        status="unsupported",
        asset_id=asset.asset_id,
        summary=sampling.error or "视频采样不可用。",
        model_name=sampling.model_name,
        metadata={"asset_role": decision.asset_role},
    )


def _visual_content(validation: AssetInputValidationResult) -> dict[str, str] | None:
    if validation.source_kind == "url":
        return {"image": validation.value}
    if validation.source_kind != "local_file" or not validation.value:
        return None
    return {"image": _image_data_uri(Path(validation.value))}


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
