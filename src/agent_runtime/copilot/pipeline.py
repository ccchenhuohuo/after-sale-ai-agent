from __future__ import annotations

from agents import custom_span

from agent_runtime.copilot.case_context import SupportCaseContextResult, SupportCaseRequest
from agent_runtime.copilot.context_assembly import assemble_unified_case_context
from agent_runtime.copilot.ingestion import ingest_support_case
from agent_runtime.copilot.intake import route_support_case
from agent_runtime.settings import Settings, get_settings


async def build_support_case_context(
    request: SupportCaseRequest,
    settings: Settings | None = None,
) -> SupportCaseContextResult:
    settings = settings or get_settings()
    with custom_span(
        "support_case_context_pipeline",
        {
            "request_id": request.request_id,
            "source": request.source,
            "asset_count": len(request.assets),
        },
    ):
        route = await route_support_case(request, settings)
        artifacts = await ingest_support_case(request, route, settings)
        context = await assemble_unified_case_context(request, route, artifacts, settings)
    return SupportCaseContextResult(
        request=request,
        route=route,
        artifacts=artifacts,
        context=context,
    )
