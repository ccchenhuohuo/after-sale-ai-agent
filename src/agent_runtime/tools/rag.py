from typing import Optional

from agents import function_tool

from agent_runtime.tools.history_rag import search_history_rag
from agent_runtime.tools.media_rag import search_media_rag
from agent_runtime.tools.sku_catalog import search_sku_catalog_text


def hybrid_search_kb_text(
    query: str,
    product_model: Optional[str] = None,
    module: Optional[str] = None,
    issue_type: Optional[str] = None,
) -> str:
    return (
        "未查询到可信正式依据：正式知识库/RAG 索引尚未接入当前终端测试运行。"
        "不要编造文档名称、章节、链接、政策或技术结论。"
    )


def search_issue_history_text(
    query: str,
    product_model: Optional[str] = None,
    issue_type: Optional[str] = None,
) -> str:
    sku_match = search_sku_catalog_text(product_model or query, limit=3)
    text_history = search_history_rag(query, product_model=product_model, issue_type=issue_type)
    media_evidence = search_media_rag(query, product_model=product_model)
    return "\n".join(
        [
            "混合 RAG 证据打包结果",
            "SKU 精准匹配：",
            sku_match,
            "",
            "文本历史参考：",
            text_history,
            "",
            "媒体观察证据：",
            media_evidence,
            "",
            "证据边界：",
            "- SKU 精准匹配只用于产品识别、SPU/品名/负责人流转，不能作为故障原因、售后政策或处理结论依据。",
            "- 文本历史参考来自飞书 raw JSON 话题索引，默认未审核，需人工确认，不能作为正式依据。",
            "- 媒体观察证据来自飞书 raw media 元数据和话题上下文，默认未审核，需人工打开原话题确认，不能作为正式依据。",
            "- 任何退款、换新、补发、维修时效或最终判责，必须等待正式依据或人工复核。",
        ]
    )


@function_tool
def hybrid_search_kb(
    query: str,
    product_model: Optional[str] = None,
    module: Optional[str] = None,
    issue_type: Optional[str] = None,
) -> str:
    """
    Query official product knowledge.

    The durable official KB/RAG index is not connected yet. Return an explicit
    empty result so the agent cannot treat local demo snippets as evidence.
    """
    return hybrid_search_kb_text(query, product_model=product_model, module=module, issue_type=issue_type)


@function_tool
def search_issue_history(
    query: str,
    product_model: Optional[str] = None,
    issue_type: Optional[str] = None,
) -> str:
    """
    Query reviewed historical QA cases and support-group experience.

    The first MVP index uses Feishu raw JSON topic data as unreviewed historical
    reference only. It must not be treated as formal evidence.
    """
    return search_issue_history_text(query, product_model=product_model, issue_type=issue_type)
