from agents import Agent

from agent_runtime.copilot.answer_contract import SupportAnswer, support_answer_output_guardrail
from agent_runtime.copilot.prompts import SUPPORT_COPILOT_INSTRUCTIONS


def build_support_copilot(model_name: str) -> Agent:
    return Agent(
        name="VIJIM-after-sale-copilot",
        instructions=SUPPORT_COPILOT_INSTRUCTIONS,
        model=model_name,
        tools=[],
        output_type=SupportAnswer,
        output_guardrails=[support_answer_output_guardrail],
    )
