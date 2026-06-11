from agents import Agent

from agent_runtime.copilot.prompts import SUPPORT_COPILOT_INSTRUCTIONS
from agent_runtime.tools.rag import hybrid_search_kb, search_issue_history
from agent_runtime.tools.sku_catalog import search_sku_catalog
from agent_runtime.tools.ticket import create_ticket_draft


def build_support_copilot(model_name: str) -> Agent:
    return Agent(
        name="ulanzi after-sell copilot",
        instructions=SUPPORT_COPILOT_INSTRUCTIONS,
        model=model_name,
        tools=[search_sku_catalog, hybrid_search_kb, search_issue_history, create_ticket_draft],
    )
