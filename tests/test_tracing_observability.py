import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import agent_runtime.channels.feishu_reply as feishu_reply
import agent_runtime.channels.openclaw_feishu.webhook as openclaw_webhook
import agent_runtime.copilot.evidence_collection as evidence_collection
import agent_runtime.copilot.ingestion as ingestion
import agent_runtime.copilot.runtime as support_runtime
import agent_runtime.feishu.bridge as bridge
import agent_runtime.observability.tracing as obs_tracing
import agent_runtime.terminal.runtime as terminal_runtime
from agent_runtime.copilot.answer_contract import SupportAnswer
from agent_runtime.copilot.case_context import (
    AssetRouteDecision,
    RouteDecision,
    SupportAsset,
    SupportCaseRequest,
)
from agent_runtime.copilot.evidence import HistoryEvidence, MediaEvidence, OfficialKbEvidence, SkuEvidence
from agent_runtime.feishu.events import FeishuMessageEvent
from agent_runtime.settings import Settings


class FakeSpan:
    def __init__(self, name: str, attrs: dict | None = None, sink: list | None = None):
        self.name = name
        self.span_data = SimpleNamespace(data=dict(attrs or {}))
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._sink is not None:
            self._sink.append((self.name, dict(self.span_data.data)))
        return False


class FakeTurn:
    def __init__(self, attrs: dict, *, include_full_io: bool = True):
        self.attrs = attrs
        self.include_full_io = include_full_io

    @property
    def active(self):
        return True

    def update(self, attrs: dict):
        self.attrs.update(attrs)

    def set_output(self, text: str, *, output_kind: str, status: str):
        self.attrs.update(
            {
                "output.kind": output_kind,
                "output.status": status,
                "output_chars": len(text or ""),
            }
        )
        if self.include_full_io:
            self.attrs["output.value"] = text or ""


def settings_for_tmp(tmp_path, **overrides):
    values = {
        "feishu_support_group_chat_id": "oc_target",
        "feishu_bot_mention_name": "飞书 CLI",
        "feishu_runtime_db_path": str(tmp_path / "runtime.sqlite3"),
        "support_agent_session_db_path": str(tmp_path / "agent_sessions.sqlite3"),
        "support_agent_tracing_disabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def feishu_event(message_id: str = "om_msg") -> FeishuMessageEvent:
    return FeishuMessageEvent(
        event_id=f"evt_{message_id}",
        chat_id="oc_target",
        chat_type="group",
        message_id=message_id,
        message_type="text",
        sender_id="ou_sender",
        content="@飞书 CLI L023 不亮",
        mention_names=("飞书 CLI",),
        event_source="backfill",
    )


def test_duplicate_event_is_not_traced_by_default(monkeypatch, tmp_path):
    bridge.clear_runtime_state_for_tests()
    runtime_traces = []
    admission_traces = []

    @contextmanager
    def fake_runtime_trace(settings, *, entrypoint, group_id, attrs):
        runtime_traces.append(attrs)
        yield

    @contextmanager
    def fake_admission_trace(settings, attrs):
        admission_traces.append(attrs)
        yield

    async def fake_agent(event, settings):
        return "answer"

    async def fake_reply(message_id, text, settings=None):
        return None

    monkeypatch.setattr(bridge, "runtime_trace", fake_runtime_trace)
    monkeypatch.setattr(bridge, "admission_trace", fake_admission_trace)
    monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", fake_reply)
    monkeypatch.setattr(bridge, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs))

    settings = settings_for_tmp(tmp_path)
    event = feishu_event()

    assert asyncio.run(bridge.process_message_event(event, settings)) == "replied"
    assert asyncio.run(bridge.process_message_event(event, settings)) == "duplicate"

    assert len(runtime_traces) == 1
    assert runtime_traces[0]["trace_kind"] == "runtime"
    assert runtime_traces[0]["event_source"] == "backfill"
    assert admission_traces == []


def test_duplicate_event_can_be_traced_in_full_debug_mode(monkeypatch, tmp_path):
    bridge.clear_runtime_state_for_tests()
    admission_traces = []

    @contextmanager
    def fake_runtime_trace(settings, *, entrypoint, group_id, attrs):
        yield

    @contextmanager
    def fake_admission_trace(settings, attrs):
        admission_traces.append(attrs)
        yield

    async def fake_agent(event, settings):
        return "answer"

    async def fake_reply(message_id, text, settings=None):
        return None

    monkeypatch.setattr(bridge, "runtime_trace", fake_runtime_trace)
    monkeypatch.setattr(bridge, "admission_trace", fake_admission_trace)
    monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", fake_reply)
    monkeypatch.setattr(bridge, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs))

    settings = settings_for_tmp(
        tmp_path,
        support_trace_admission_mode="full",
        support_trace_duplicate_events=True,
    )
    event = feishu_event()

    assert asyncio.run(bridge.process_message_event(event, settings)) == "replied"
    assert asyncio.run(bridge.process_message_event(event, settings)) == "duplicate"

    assert len(admission_traces) == 1
    assert admission_traces[0]["trace_kind"] == "admission"
    assert admission_traces[0]["event_status"] == "duplicate"


def test_core_runtime_does_not_flush_traces():
    source = Path(support_runtime.__file__).read_text(encoding="utf-8")

    assert "flush_traces(" not in source


def test_core_runtime_namespaces_internal_io_fields():
    source = Path(support_runtime.__file__).read_text(encoding="utf-8")

    assert '"support_core_runtime"' in source
    assert '"support_runtime_turn"' not in source
    assert '"runner.input.value"' in source
    assert '"retrieval.input.value"' in source
    assert '"context.input.value"' in source
    assert '"internal_answer.value"' in source
    assert '"input.value": agent_input' not in source
    assert '"output.value": internal_text' not in source


def test_span_if_tracing_without_active_trace_does_not_create_span(monkeypatch):
    calls = []

    def fake_custom_span(name, attrs=None):
        calls.append((name, attrs))
        return FakeSpan(name, attrs)

    monkeypatch.setattr(obs_tracing, "get_current_trace", lambda: None)
    monkeypatch.setattr(obs_tracing, "custom_span", fake_custom_span)

    with obs_tracing.span_if_tracing("orphan_span", {"status": "ignored"}) as span:
        assert span is None

    assert calls == []


def test_span_if_tracing_inside_active_trace_creates_child_span(monkeypatch):
    spans = []

    monkeypatch.setattr(obs_tracing, "get_current_trace", lambda: object())
    monkeypatch.setattr(obs_tracing, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs, spans))

    with obs_tracing.span_if_tracing("child_span", {"status": "ok"}) as span:
        assert span is not None
        span.span_data.data["child_updated"] = True

    assert spans == [("child_span", {"status": "ok", "child_updated": True})]


def test_runtime_trace_nested_current_trace_does_not_create_second_root_trace(monkeypatch):
    trace_calls = []

    def fake_trace(*args, **kwargs):
        trace_calls.append((args, kwargs))
        return FakeSpan("runtime_trace")

    monkeypatch.setattr(obs_tracing, "get_current_trace", lambda: object())
    monkeypatch.setattr(obs_tracing, "trace", fake_trace)

    with obs_tracing.runtime_trace(Settings(), entrypoint="feishu", group_id="group", attrs={"trace_kind": "runtime"}) as turn:
        assert not turn.active

    assert trace_calls == []


def test_runtime_trace_creates_turn_span_and_updates_output(monkeypatch):
    trace_calls = []
    spans = []

    def fake_trace(*args, **kwargs):
        trace_calls.append((args, kwargs))
        return FakeSpan("runtime_trace")

    monkeypatch.setattr(obs_tracing, "get_current_trace", lambda: None)
    monkeypatch.setattr(obs_tracing, "trace", fake_trace)
    monkeypatch.setattr(obs_tracing, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs, spans))

    attrs = {
        "trace_kind": "runtime",
        "entrypoint": "feishu",
        "session.id": "session-hash",
        "input.value": "客户原始问题",
        "user.input": "客户原始问题",
    }
    with obs_tracing.runtime_trace(Settings(), entrypoint="feishu", group_id="group", attrs=attrs) as turn:
        assert turn.active
        turn.set_output("最终可见回复", output_kind="feishu_visible_reply", status="replied")

    assert len(trace_calls) == 1
    assert "input.value" not in trace_calls[0][1]["metadata"]
    assert "user.input" not in trace_calls[0][1]["metadata"]
    assert spans == [
        (
            "support_runtime_turn",
            {
                "span.kind": "runtime_turn",
                "trace_kind": "runtime",
                "entrypoint": "feishu",
                "session.id": "session-hash",
                "input.value": "客户原始问题",
                "user.input": "客户原始问题",
                "output.kind": "feishu_visible_reply",
                "output.status": "replied",
                "output_chars": len("最终可见回复"),
                "output.value": "最终可见回复",
            },
        )
    ]


def test_accepted_feishu_runtime_trace_contains_reply_render_and_reply_spans(monkeypatch, tmp_path):
    bridge.clear_runtime_state_for_tests()
    runtime_traces = []
    turn_spans = []
    spans = []
    flushes = []

    @contextmanager
    def fake_runtime_trace(settings, *, entrypoint, group_id, attrs):
        runtime_traces.append({"entrypoint": entrypoint, "group_id": group_id, **obs_tracing.trace_metadata_attrs(attrs)})
        turn_span = {"span.kind": "runtime_turn", "entrypoint": entrypoint, "group_id": group_id, **attrs}
        turn_spans.append(turn_span)
        yield FakeTurn(turn_span, include_full_io=settings.support_agent_trace_include_sensitive_data)

    async def fake_agent(event, settings):
        result = SimpleNamespace(
            contract_issues=[],
            answer=_support_answer(),
            coverage=SimpleNamespace(recommended_action="answer", mention_enabled=False),
            trace_include_full_io=settings.support_agent_trace_include_sensitive_data,
        )
        return feishu_reply.render_feishu_visible_runtime_reply(result).safe_text

    async def fake_reply(message_id, text, settings=None):
        return SimpleNamespace(reply_message_id="om_reply")

    monkeypatch.setattr(bridge, "runtime_trace", fake_runtime_trace)
    monkeypatch.setattr(bridge, "run_support_agent_for_event", fake_agent)
    monkeypatch.setattr(bridge, "reply_in_thread", fake_reply)
    monkeypatch.setattr(bridge, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs, spans))
    monkeypatch.setattr(obs_tracing, "get_current_trace", lambda: object())
    monkeypatch.setattr(obs_tracing, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs, spans))
    monkeypatch.setattr(bridge, "flush_traces", lambda: flushes.append("flush"))

    settings = settings_for_tmp(tmp_path)

    assert asyncio.run(bridge.process_message_event(feishu_event(), settings)) == "replied"

    assert len(runtime_traces) == 1
    assert len(turn_spans) == 1
    assert runtime_traces[0]["trace_kind"] == "runtime"
    assert runtime_traces[0]["entrypoint"] == "feishu"
    assert runtime_traces[0]["session.id"]
    assert runtime_traces[0]["session_id_hash"]
    assert runtime_traces[0]["chat_id_hash"]
    assert runtime_traces[0]["message_id_hash"]
    assert runtime_traces[0]["input_chars"] == len("@飞书 CLI L023 不亮")
    assert "session_id" not in runtime_traces[0]
    assert "chat_id" not in runtime_traces[0]
    assert "message_id" not in runtime_traces[0]
    assert "input.value" not in runtime_traces[0]
    assert "user.input" not in runtime_traces[0]
    assert turn_spans[0]["input.value"] == "@飞书 CLI L023 不亮"
    assert turn_spans[0]["user.input"] == "@飞书 CLI L023 不亮"
    assert turn_spans[0]["output.value"]
    assert turn_spans[0]["output.kind"] == "feishu_visible_reply"
    assert turn_spans[0]["output.status"] == "replied"
    span_names = [name for name, _ in spans]
    assert "visible_reply_render" in span_names
    assert "channel_reply" in span_names
    assert "channel_reply_result" in span_names
    visible_reply_span = dict(spans)["visible_reply_render"]
    assert "output.value" not in visible_reply_span
    assert visible_reply_span["visible_reply.value"]
    assert visible_reply_span["visible_reply"] == visible_reply_span["visible_reply.value"]
    assert turn_spans[0]["output.value"] == visible_reply_span["visible_reply.value"]
    assert flushes == ["flush"]


def test_openclaw_support_case_uses_one_runtime_trace_for_runtime_and_reply(monkeypatch):
    runtime_traces = []
    turn_spans = []
    spans = []
    flushes = []

    @contextmanager
    def fake_runtime_trace(settings, *, entrypoint, group_id, attrs):
        runtime_traces.append({"entrypoint": entrypoint, "group_id": group_id, **obs_tracing.trace_metadata_attrs(attrs)})
        turn_span = {"span.kind": "runtime_turn", "entrypoint": entrypoint, "group_id": group_id, **attrs}
        turn_spans.append(turn_span)
        yield FakeTurn(turn_span, include_full_io=settings.support_agent_trace_include_sensitive_data)

    async def fake_run_support_case_request(request, settings, **kwargs):
        return SimpleNamespace(
            contract_issues=[],
            answer=_support_answer(),
            request=request,
            coverage=SimpleNamespace(recommended_action="answer", mention_enabled=False),
            trace_include_full_io=True,
        )

    monkeypatch.setattr(
        openclaw_webhook,
        "get_settings",
        lambda: Settings(llm_api_key="test-key", openclaw_feishu_bridge_secret="secret"),
    )
    monkeypatch.setattr(openclaw_webhook, "configure_agents_runtime", lambda settings: settings)
    monkeypatch.setattr(openclaw_webhook, "build_support_runtime_session", lambda settings, session_id: "session")
    monkeypatch.setattr(openclaw_webhook, "run_support_case_request", fake_run_support_case_request)
    monkeypatch.setattr(openclaw_webhook, "runtime_trace", fake_runtime_trace)
    monkeypatch.setattr(openclaw_webhook, "flush_traces", lambda: flushes.append("flush"))
    monkeypatch.setattr(obs_tracing, "get_current_trace", lambda: object())
    monkeypatch.setattr(obs_tracing, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs, spans))

    reply = asyncio.run(
        openclaw_webhook.openclaw_feishu_support_case(
            {
                "message": {
                    "chatId": "oc_chat",
                    "messageId": "om_msg",
                    "threadId": "omt_thread",
                    "senderId": "ou_sender",
                    "content": "客户反馈 L023 不亮",
                }
            },
            x_openclaw_feishu_secret="secret",
        )
    )

    assert reply["mode"] == "thread_reply"
    assert len(runtime_traces) == 1
    assert len(turn_spans) == 1
    assert runtime_traces[0]["entrypoint"] == "openclaw_feishu"
    assert runtime_traces[0]["trace_kind"] == "runtime"
    assert runtime_traces[0]["message_id_hash"]
    assert runtime_traces[0]["request_id_hash"]
    assert runtime_traces[0]["session.id"]
    assert runtime_traces[0]["session_id_hash"]
    assert runtime_traces[0]["chat_id_hash"]
    assert runtime_traces[0]["thread_id_hash"]
    assert runtime_traces[0]["input_chars"] == len("客户反馈 L023 不亮")
    assert "request_id" not in runtime_traces[0]
    assert "chat_id" not in runtime_traces[0]
    assert "thread_id" not in runtime_traces[0]
    assert "message_id" not in runtime_traces[0]
    assert "input.value" not in runtime_traces[0]
    assert "user.input" not in runtime_traces[0]
    assert turn_spans[0]["input.value"] == "客户反馈 L023 不亮"
    assert turn_spans[0]["user.input"] == "客户反馈 L023 不亮"
    assert turn_spans[0]["output.value"] == reply["text"]
    assert turn_spans[0]["output.kind"] == "openclaw_thread_reply_payload"
    assert turn_spans[0]["output.status"] == "payload_built"
    assert {name for name, _ in spans} >= {"visible_reply_render", "channel_reply", "channel_reply_result"}
    visible_reply_span = dict(spans)["visible_reply_render"]
    assert "output.value" not in visible_reply_span
    assert visible_reply_span["visible_reply.value"]
    assert visible_reply_span["visible_reply"] == visible_reply_span["visible_reply.value"]
    assert visible_reply_span["output_chars"] > 0
    reply_result_span = dict(spans)["channel_reply_result"]
    assert reply_result_span["reply_status"] == "payload_built"
    assert reply_result_span["reply_transport"] == "payload_only"
    assert flushes == ["flush"]


def test_openclaw_contract_only_does_not_create_runtime_trace(monkeypatch):
    runtime_traces = []
    flushes = []
    spans = []

    @contextmanager
    def fake_runtime_trace(settings, *, entrypoint, group_id, attrs):
        runtime_traces.append(attrs)
        yield

    monkeypatch.setattr(
        openclaw_webhook,
        "get_settings",
        lambda: Settings(openclaw_feishu_bridge_secret="", openclaw_feishu_require_secret=False),
    )
    monkeypatch.setattr(openclaw_webhook, "runtime_trace", fake_runtime_trace)
    monkeypatch.setattr(openclaw_webhook, "flush_traces", lambda: flushes.append("flush"))
    monkeypatch.setattr(obs_tracing, "get_current_trace", lambda: None)
    monkeypatch.setattr(obs_tracing, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs, spans))

    reply = asyncio.run(
        openclaw_webhook.openclaw_feishu_support_case(
            {
                "contractOnly": True,
                "message": {
                    "chatId": "oc_chat",
                    "messageId": "om_msg",
                    "threadId": "omt_thread",
                    "content": "contract smoke",
                },
            }
        )
    )

    assert reply["mode"] == "thread_reply"
    assert runtime_traces == []
    assert flushes == []
    assert spans == []


def test_terminal_turn_records_turn_level_input_and_output(monkeypatch, capsys):
    runtime_traces = []
    turn_spans = []
    flushes = []

    @contextmanager
    def fake_runtime_trace(settings, *, entrypoint, group_id, attrs):
        runtime_traces.append({"entrypoint": entrypoint, "group_id": group_id, **obs_tracing.trace_metadata_attrs(attrs)})
        turn_span = {"span.kind": "runtime_turn", "entrypoint": entrypoint, "group_id": group_id, **attrs}
        turn_spans.append(turn_span)
        yield FakeTurn(turn_span, include_full_io=settings.support_agent_trace_include_sensitive_data)

    async def fake_run_support_case_request(request, settings, **kwargs):
        return SimpleNamespace(internal_text="终端最终输出", contract_issues=[])

    monkeypatch.setattr(terminal_runtime, "runtime_trace", fake_runtime_trace)
    monkeypatch.setattr(terminal_runtime, "run_support_case_request", fake_run_support_case_request)
    monkeypatch.setattr(terminal_runtime, "flush_traces", lambda: flushes.append("flush"))

    asyncio.run(terminal_runtime.run_turn(object(), Settings(), object(), "L023 不亮"))

    assert len(runtime_traces) == 1
    assert runtime_traces[0]["entrypoint"] == "terminal"
    assert "input.value" not in runtime_traces[0]
    assert len(turn_spans) == 1
    assert turn_spans[0]["input.value"] == "L023 不亮"
    assert turn_spans[0]["user.input"] == "L023 不亮"
    assert turn_spans[0]["output.value"] == "终端最终输出"
    assert turn_spans[0]["output.kind"] == "terminal_internal_answer"
    assert turn_spans[0]["output.status"] == "ready"
    assert flushes == ["flush"]
    assert "终端最终输出" in capsys.readouterr().out


def test_retrieval_spans_include_tool_like_attributes(monkeypatch):
    spans = []

    monkeypatch.setattr(evidence_collection, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs, spans))
    monkeypatch.setattr(
        evidence_collection,
        "resolve_sku_evidence",
        lambda query, limit=5, settings=None: [
            SkuEvidence(
                status="hit",
                evidence_level="identity_only",
                verified=True,
                query_hash="sku-hash",
                sku="L023",
                score=100,
            )
        ],
    )
    monkeypatch.setattr(
        evidence_collection,
        "search_official_kb_evidence",
        lambda query, product_model=None, module=None, issue_type=None, settings=None: [
            OfficialKbEvidence(status="empty", evidence_level="empty", verified=False, query_hash="official-hash")
        ],
    )
    monkeypatch.setattr(
        evidence_collection,
        "search_history_evidence",
        lambda query, product_model=None, issue_type=None, settings=None: [
            HistoryEvidence(status="hit", evidence_level="reviewed_case", verified=True, query_hash="history-hash")
        ],
    )
    monkeypatch.setattr(
        evidence_collection,
        "search_media_evidence",
        lambda query, product_model=None, settings=None, vector_refs=None: [
            MediaEvidence(status="empty", evidence_level="empty", verified=False, query_hash="media-hash")
        ],
    )

    asyncio.run(evidence_collection.collect_support_evidence("L023 不亮", Settings()))

    retrieval_spans = {name: attrs for name, attrs in spans if attrs.get("operation") == "tool_like_retrieval"}
    assert retrieval_spans["sku_resolve"]["tool_name"] == "sku_catalog"
    assert retrieval_spans["sku_resolve"]["status"] == "hit"
    assert retrieval_spans["history_search"]["tool_name"] == "history_rag"
    assert retrieval_spans["history_search"]["status"] == "hit"
    assert retrieval_spans["media_search"]["status"] == "empty"
    assert "query_hash" in retrieval_spans["official_kb_search"]


def test_ingestion_spans_include_status_for_skipped_and_unsupported_tools(monkeypatch):
    spans = []
    monkeypatch.setattr(ingestion, "custom_span", lambda name, attrs=None: FakeSpan(name, attrs, spans))
    request = SupportCaseRequest(
        request_id="req_img",
        source="feishu",
        assets=[SupportAsset(asset_id="img_chat", media_type="image", filename="chat_screenshot.png")],
    )
    route = RouteDecision(
        input_modality="image",
        confidence=0.8,
        asset_decisions=[
            AssetRouteDecision(
                asset_id="img_chat",
                media_type="image",
                asset_role="chat_screenshot",
                requires_ocr=True,
                requires_visual_embedding=False,
                requires_video_sampling=False,
            )
        ],
    )

    artifacts = asyncio.run(ingestion.ingest_support_case(request, route, Settings()))

    assert any(artifact.artifact_type == "ocr" and artifact.status == "unsupported" for artifact in artifacts)
    ingestion_spans = [attrs for _, attrs in spans if attrs.get("operation") == "ingestion_tool"]
    assert any(attrs["tool_name"] == "ocr" and attrs["status"] == "unsupported" for attrs in ingestion_spans)
    assert any(attrs["tool_name"] == "image_embedding" and attrs["status"] == "skipped" for attrs in ingestion_spans)
    assert any(attrs["tool_name"] == "video_sampling" and attrs["status"] == "skipped" for attrs in ingestion_spans)


def _support_answer() -> SupportAnswer:
    return SupportAnswer(
        issue_type="unknown",
        run_mode="Agent SDK",
        confidence="低",
        confidence_reason="未查询到可信正式依据。",
        user_issue_summary="客户反馈设备异常。",
        sku_match="未在 SKU 目录中命中；需要补充订单 SKU、包装 SKU、产品铭牌或图片。",
        suggested_reply="建议先安抚客户，并说明需要补充信息后再确认处理方式。",
        troubleshooting_steps=["确认型号"],
        follow_up_questions=["请补充 SKU"],
        official_evidence="未查询到可信正式依据，不可编造。",
        history_reference="未查询到可信历史参考，不可编造。",
        ticket_draft="不建议生成工单，并说明原因。",
    )
