from __future__ import annotations

from agents import custom_span

from agent_runtime.copilot.case_context import SupportCaseContextResult, SupportCaseRequest
from agent_runtime.copilot.context_assembly import assemble_unified_case_context
from agent_runtime.copilot.ingestion import ingest_support_case
from agent_runtime.copilot.intake import route_support_case
from agent_runtime.observability.tracing import hash_trace_id
from agent_runtime.settings import Settings, get_settings


async def build_support_case_context(
    request: SupportCaseRequest,
    settings: Settings | None = None,
) -> SupportCaseContextResult:
    settings = settings or get_settings()
    with custom_span(
        "support_case_context_pipeline",
        {
            "request_id_hash": hash_trace_id(request.request_id),
            "source": request.source,
            "channel": request.channel,
            "asset_count": len(request.assets),
        },
    ):
        with custom_span(
            "intake_router",
            {
                "request_id_hash": hash_trace_id(request.request_id),
                "router_enabled": settings.support_intake_router_enabled,
                "asset_count": len(request.assets),
            },
        ):
            route = await route_support_case(request, settings)
        with custom_span(
            "intake_router_result",
            {
                "status": "ok",
                "input_modality": route.input_modality,
                "asset_decision_count": len(route.asset_decisions),
                "needs_clarification": route.needs_clarification,
                "confidence": route.confidence,
            },
        ):
            pass
        with custom_span(
            "ingestion_layer",
            {
                "request_id_hash": hash_trace_id(request.request_id),
                "asset_count": len(request.assets),
                "input_modality": route.input_modality,
            },
        ):
            artifacts = await ingest_support_case(request, route, settings)
        with custom_span(
            "ingestion_layer_result",
            {
                "status": "ok",
                "artifact_count": len(artifacts),
                "ok_count": sum(1 for artifact in artifacts if artifact.status == "ok"),
                "error_count": sum(1 for artifact in artifacts if artifact.status == "error"),
                "unsupported_count": sum(1 for artifact in artifacts if artifact.status == "unsupported"),
            },
        ):
            pass
        with custom_span(
            "context_assembly",
            {
                "request_id_hash": hash_trace_id(request.request_id),
                "assembler_enabled": settings.support_context_assembler_enabled,
                "artifact_count": len(artifacts),
            },
        ):
            context = await assemble_unified_case_context(request, route, artifacts, settings)
        with custom_span(
            "context_assembly_result",
            {
                "status": "ok",
                "query_chars": len(context.normalized_query),
                "extracted_text_count": len(context.extracted_texts),
                "visual_summary_count": len(context.visual_summaries),
                "asset_ref_count": len(context.asset_refs),
                "vector_ref_count": len(context.vector_refs),
                "missing_information_count": len(context.missing_information),
            },
        ):
            pass
    return SupportCaseContextResult(
        request=request,
        route=route,
        artifacts=artifacts,
        context=context,
    )
