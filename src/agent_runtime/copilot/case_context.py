from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


AssetMediaType = Literal["image", "video", "audio", "file", "text", "unknown"]
AssetRole = Literal[
    "chat_screenshot",
    "invoice",
    "error_screenshot",
    "text_document",
    "product_photo",
    "damage_photo",
    "label_photo",
    "packaging_photo",
    "video",
    "unknown",
]
InputModality = Literal["text", "image", "video", "mixed", "unknown", "needs_clarification"]
ArtifactStatus = Literal["ok", "empty", "error", "unsupported"]
ArtifactType = Literal["text", "ocr", "image_embedding", "video_sampling", "metadata", "visual_summary"]
RecommendedAction = Literal["answer", "ask_clarification", "human_review"]


class SupportAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    media_type: AssetMediaType = "unknown"
    source: str = "unknown"
    filename: str = ""
    mime_type: str = ""
    file_key: str = ""
    message_id: str = ""
    url: str = ""
    local_path: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SupportCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    source: Literal["terminal", "feishu", "api", "unknown"] = "unknown"
    user_text: str = ""
    assets: list[SupportAsset] = Field(default_factory=list)
    chat_id: str = ""
    thread_id: str = ""
    message_id: str = ""
    sender_id: str = ""
    session_id: str = ""
    trace_group_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssetRouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    media_type: AssetMediaType = "unknown"
    asset_role: AssetRole = "unknown"
    requires_ocr: bool = False
    requires_visual_embedding: bool = False
    requires_video_sampling: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_modality: InputModality = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    user_text: str = ""
    asset_decisions: list[AssetRouteDecision] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)


class IngestionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    artifact_type: ArtifactType
    status: ArtifactStatus
    asset_id: str = ""
    text: str = ""
    summary: str = ""
    vector_id: str = ""
    source_text_hash: str = ""
    model_name: str = ""
    index_namespace: str = ""
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class UnifiedCaseContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    source: str
    original_user_text: str = ""
    normalized_query: str
    extracted_texts: list[str] = Field(default_factory=list)
    visual_summaries: list[str] = Field(default_factory=list)
    asset_refs: list[str] = Field(default_factory=list)
    vector_refs: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    detected_product: str = ""
    detected_fault: str = ""
    customer_intent: str = ""
    missing_information: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    route: RouteDecision


class DataSourceCoverageItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_name: str
    status: Literal["hit", "miss", "missing", "error", "not_configured"]
    authority: Literal["formal", "reviewed", "unreviewed", "identity_only", "media_observation", "missing"]
    hit_count: int = 0
    confidence: Literal["高", "中", "低", "未知"] = "未知"
    message: str = ""


class DataSourceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DataSourceCoverageItem] = Field(default_factory=list)
    recommended_action: RecommendedAction = "ask_clarification"
    owner_candidate: str = ""
    mention_enabled: bool = False
    reason: str = ""


class SupportCaseContextResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: SupportCaseRequest
    route: RouteDecision
    artifacts: list[IngestionArtifact]
    context: UnifiedCaseContext
