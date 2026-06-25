import asyncio

from agent_runtime.feishu.events import FeishuMessageEvent
from agent_runtime.feishu.queues import PerThreadQueue


def _event(message_id: str, root_id: str = "") -> FeishuMessageEvent:
    return FeishuMessageEvent(
        event_id=f"evt_{message_id}",
        chat_id="oc_target",
        chat_type="group",
        message_id=message_id,
        message_type="text",
        sender_id="ou_sender",
        content="L023 不亮",
        root_id=root_id,
    )


def test_queue_does_not_evict_busy_lock_when_at_capacity():
    async def scenario():
        queue = PerThreadQueue(max_items=1)
        first = queue.lock_for_event(_event("om_first"))
        await first.acquire()
        try:
            second = queue.lock_for_event(_event("om_second"))
            first_again = queue.lock_for_event(_event("om_first"))
        finally:
            first.release()
        return first, first_again, second

    first, first_again, second = asyncio.run(scenario())

    assert first_again is first
    assert second is not first
