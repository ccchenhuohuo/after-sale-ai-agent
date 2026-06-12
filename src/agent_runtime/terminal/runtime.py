from agents import Agent, Runner, SQLiteSession, custom_span, flush_traces
from agents.exceptions import OutputGuardrailTripwireTriggered

from agent_runtime.copilot.answer_contract import render_support_answer, validate_answer_contract
from agent_runtime.copilot.evidence import evidence_pack_trace_attributes, short_hash
from agent_runtime.copilot.evidence_collection import collect_support_evidence
from agent_runtime.copilot.prompts import build_agent_input
from agent_runtime.llm import build_run_config
from agent_runtime.terminal.session import session_items_to_text
from agent_runtime.terminal.ui import active_model_label, cyan, dim, green, yellow
from agent_runtime.tools.history_rag import history_rag_index_available
from agent_runtime.tools.media_rag import media_rag_index_available


def build_compactor(model_name: str) -> Agent:
    return Agent(
        name="终端上下文压缩器",
        model=model_name,
        instructions=(
            "你负责把售后客服 Agent 终端会话压缩成后续可用的上下文摘要。"
            "保留用户明确要求、项目状态、模型/工具选择、重要技术结论、未解决问题和后续计划。"
            "删除临时命令输出、重复内容、冗长回答和无用细节。"
            "不要新增事实。用中文输出，结构清晰，控制在 1200 字以内。"
        ),
        tools=[],
    )


async def compact_context(settings, session: SQLiteSession) -> None:
    items = await session.get_items(limit=2_147_483_647)
    if not items:
        print(yellow("当前没有可压缩的上下文。\n"))
        return

    print(dim("\n正在压缩当前上下文..."))
    transcript = session_items_to_text(items)
    if len(transcript) > 24000:
        transcript = transcript[-24000:]
    compactor = build_compactor(settings.support_agent_model)
    result = await Runner.run(
        compactor,
        "请压缩以下终端会话上下文，供后续 ulanzi after-sell copilot 继续参考：\n\n" + transcript,
        run_config=build_run_config(
            settings,
            group_id="terminal-chat",
            metadata={"source": "terminal-compact", "model_label": active_model_label(settings)},
        ),
    )
    flush_traces()

    summary = result.final_output.strip()
    await session.clear_session()
    await session.add_items(
        [
            {
                "role": "user",
                "content": "【上下文压缩摘要，供后续回答参考】\n" + summary,
            }
        ]
    )
    print(green("已压缩上下文：保留 1 条摘要，旧上下文已清理。\n"))


async def run_turn(agent, settings, session: SQLiteSession, user_input: str) -> None:
    print(dim("\nAgent 正在分析..."))
    try:
        with custom_span(
            "support_turn",
            {
                "entrypoint": "terminal",
                "loop_version": "v2",
                "raw_issue_hash": short_hash(user_input),
            },
        ):
            evidence_pack = await collect_support_evidence(user_input, settings)
            result = await Runner.run(
                agent,
                build_agent_input(user_input, source="本地终端", evidence_pack=evidence_pack),
                context=evidence_pack,
                session=session,
                run_config=build_run_config(
                    settings,
                    group_id="terminal-chat",
                    metadata={
                        "source": "terminal-chat",
                        "entrypoint": "terminal",
                        "loop_version": "v2",
                        "model_label": active_model_label(settings),
                        "history_index_available": history_rag_index_available(settings),
                        "media_index_available": media_rag_index_available(settings),
                    },
                ),
            )
            final_output = render_support_answer(result.final_output)
            with custom_span(
                "answer_contract_check",
                {
                    **evidence_pack_trace_attributes(evidence_pack),
                    "entrypoint": "terminal",
                },
            ):
                contract_issues = validate_answer_contract(
                    final_output,
                    history_connected=history_rag_index_available(settings),
                )
    except OutputGuardrailTripwireTriggered as exc:
        flush_traces()
        print("\n" + yellow("输出安全校验未通过，已阻断本轮答案。"))
        for message in _guardrail_messages(exc):
            print(f"- {message}")
        print("")
        return

    flush_traces()
    print("\n" + cyan("─" * 78))
    print(final_output)
    if contract_issues:
        print("\n" + yellow("内部校验提醒"))
        for issue in contract_issues:
            print(f"- {issue.code}: {issue.message}")
    print(cyan("─" * 78) + "\n")


def _guardrail_messages(exc: OutputGuardrailTripwireTriggered) -> list[str]:
    guardrail_result = getattr(exc, "guardrail_result", None)
    output = getattr(guardrail_result, "output", None)
    output_info = getattr(output, "output_info", None)
    if isinstance(output_info, list):
        messages = []
        for item in output_info:
            if isinstance(item, dict) and item.get("message"):
                messages.append(str(item["message"]))
        if messages:
            return messages
    return ["输出不符合售后安全边界，请人工处理。"]
