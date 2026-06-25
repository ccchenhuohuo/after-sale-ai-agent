from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from urllib.parse import quote

import httpx

from agent_runtime.feishu.message_sender import FEISHU_BASE_URL, get_tenant_access_token
from agent_runtime.settings import Settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkingReaction:
    message_id: str
    reaction_id: str
    emoji_type: str


async def create_working_reaction(message_id: str, settings: Settings) -> WorkingReaction | None:
    if not settings.feishu_working_reaction_enabled:
        return None
    emoji_type = settings.feishu_working_reaction_emoji_type.strip()
    if not message_id or not emoji_type:
        return None
    try:
        token = await get_tenant_access_token(settings)
        async with httpx.AsyncClient(timeout=max(1.0, settings.feishu_working_reaction_timeout_seconds)) as client:
            response = await client.post(
                f"{FEISHU_BASE_URL}/im/v1/messages/{quote(message_id, safe='')}/reactions",
                headers={"Authorization": f"Bearer {token}"},
                json={"reaction_type": {"emoji_type": emoji_type}},
            )
            response.raise_for_status()
            payload = response.json()
        code = int(payload.get("code", 0) or 0) if isinstance(payload, dict) else 0
        if code != 0:
            raise RuntimeError(f"Feishu reaction create failed: code={code} msg={payload.get('msg')}")
        if not isinstance(payload, dict):
            raise RuntimeError("Feishu reaction create returned non-object payload")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        reaction_id = str(data.get("reaction_id") or "")
        if not reaction_id:
            raise RuntimeError("Feishu reaction create returned no reaction_id")
        logger.info(
            "Added Feishu working reaction: message_id_hash=%s reaction_id_hash=%s emoji_type=%s",
            _short_hash(message_id),
            _short_hash(reaction_id),
            emoji_type,
        )
        return WorkingReaction(message_id=message_id, reaction_id=reaction_id, emoji_type=emoji_type)
    except Exception as exc:
        logger.warning(
            "Failed to add Feishu working reaction: message_id_hash=%s emoji_type=%s error_type=%s",
            _short_hash(message_id),
            emoji_type,
            type(exc).__name__,
            exc_info=True,
        )
        return None


async def delete_working_reaction(reaction: WorkingReaction | None, settings: Settings) -> None:
    if reaction is None:
        return
    try:
        token = await get_tenant_access_token(settings)
        async with httpx.AsyncClient(timeout=max(1.0, settings.feishu_working_reaction_timeout_seconds)) as client:
            response = await client.delete(
                (
                    f"{FEISHU_BASE_URL}/im/v1/messages/{quote(reaction.message_id, safe='')}"
                    f"/reactions/{quote(reaction.reaction_id, safe='')}"
                ),
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Feishu reaction delete returned non-object payload")
        code = int(payload.get("code", 0) or 0)
        if code != 0:
            raise RuntimeError(f"Feishu reaction delete failed: code={code} msg={payload.get('msg')}")
        logger.info(
            "Deleted Feishu working reaction: message_id_hash=%s reaction_id_hash=%s emoji_type=%s",
            _short_hash(reaction.message_id),
            _short_hash(reaction.reaction_id),
            reaction.emoji_type,
        )
    except Exception as exc:
        logger.warning(
            "Failed to delete Feishu working reaction: message_id_hash=%s reaction_id_hash=%s error_type=%s",
            _short_hash(reaction.message_id),
            _short_hash(reaction.reaction_id),
            type(exc).__name__,
            exc_info=True,
        )


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12] if value else ""
