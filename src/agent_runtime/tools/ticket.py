from agents import function_tool


@function_tool
def create_ticket_draft(
    product_model: str,
    issue_category: str,
    severity: str,
    summary: str,
    missing_info: str,
    suggested_owner: str,
    next_action: str,
) -> str:
    """Create a human-confirmed ticket draft. This does not create a formal ticket."""
    return f"""工单草稿：
产品型号：{product_model}
问题类型：{issue_category}
严重程度：{severity}
问题摘要：{summary}
缺失信息：{missing_info}
建议负责人：{suggested_owner}
下一步：{next_action}
"""
