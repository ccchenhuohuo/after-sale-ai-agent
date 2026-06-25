from agent_runtime.feishu.events import FeishuMessageEvent
from agent_runtime.feishu import runtime_store as runtime_store_module
from agent_runtime.feishu.runtime_store import RuntimeStore


def _event(message_id: str = "om_msg") -> FeishuMessageEvent:
    return FeishuMessageEvent(
        event_id="evt_1",
        chat_id="oc_target",
        chat_type="group",
        message_id=message_id,
        message_type="text",
        sender_id="ou_sender",
        content="L023 不亮",
    )


def test_processing_claim_can_retry_after_stale_timeout(monkeypatch, tmp_path):
    times = iter([1000.0, 1001.0, 1600.0])
    monkeypatch.setattr(runtime_store_module.time, "time", lambda: next(times))
    store = RuntimeStore(
        str(tmp_path / "runtime.sqlite3"),
        ttl_seconds=43200,
        max_items=100,
        processing_stale_seconds=600,
    )

    first = store.claim_event(_event())
    duplicate = store.claim_event(_event())
    retry = store.claim_event(_event())

    assert first.status == "processing"
    assert first.should_process is True
    assert duplicate.status == "duplicate"
    assert duplicate.should_process is False
    assert retry.status == "retry_processing_stale"
    assert retry.should_process is True


def test_processing_claim_does_not_retry_when_stale_retry_disabled(monkeypatch, tmp_path):
    times = iter([1000.0, 2000.0])
    monkeypatch.setattr(runtime_store_module.time, "time", lambda: next(times))
    store = RuntimeStore(
        str(tmp_path / "runtime.sqlite3"),
        ttl_seconds=43200,
        max_items=100,
        processing_stale_seconds=0,
    )

    assert store.claim_event(_event()).should_process is True
    duplicate = store.claim_event(_event())

    assert duplicate.status == "duplicate"
    assert duplicate.should_process is False
