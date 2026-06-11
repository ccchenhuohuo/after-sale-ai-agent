from __future__ import annotations

import json
from typing import Optional

import httpx

from agent_runtime.settings import Settings, get_settings


FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"


class TopLevelMessageBlocked(RuntimeError):
    """Raised when code attempts to create a top-level Feishu message without approval."""


async def get_tenant_access_token(settings: Optional[Settings] = None) -> str:
    settings = settings or get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required.")

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        response.raise_for_status()
        payload = response.json()

    token = payload.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"Failed to get Feishu tenant_access_token: {payload}")
    return str(token)


async def send_feishu_text_message(
    chat_id: str,
    text: str,
    settings: Optional[Settings] = None,
    *,
    allow_top_level: bool = False,
    reason: str = "",
) -> str:
    """Create a top-level group message only when an operator explicitly approves it.

    The support bot runtime must use ``FeishuSdkResponder.reply_in_thread`` instead.
    This helper is kept for controlled admin scripts and tests that intentionally
    need a new top-level message.
    """
    if not allow_top_level or not reason.strip():
        raise TopLevelMessageBlocked(
            "Top-level Feishu message creation is blocked. "
            "Use reply_in_thread for bot replies, or pass allow_top_level=True with a reason for an admin operation."
        )

    token = await get_tenant_access_token(settings)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{FEISHU_BASE_URL}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        payload = response.json()

    code = int(payload.get("code", 0) or 0)
    if code != 0:
        raise RuntimeError(f"Failed to create Feishu message: code={code} msg={payload.get('msg')}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    message_id = str(data.get("message_id") or "")
    if not message_id:
        raise RuntimeError(f"Feishu message create returned no message_id: {payload}")
    return message_id
