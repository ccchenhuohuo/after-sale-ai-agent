from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from agent_runtime.channels.openclaw_feishu.webhook import router as openclaw_feishu_router
from agent_runtime.feishu.bridge import event_from_payload, process_message_event
from agent_runtime.settings import Settings, get_settings


app = FastAPI(title="ulanzi after-sell copilot Feishu webhook")
app.include_router(openclaw_feishu_router)


def _verify_token(payload: dict[str, Any], settings: Settings) -> None:
    if not settings.feishu_verification_token:
        raise HTTPException(status_code=403, detail="FEISHU_VERIFICATION_TOKEN is required for webhook requests")
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    token = payload.get("token") or header.get("token")
    if not token or token != settings.feishu_verification_token:
        raise HTTPException(status_code=403, detail="invalid Feishu verification token")


def _verify_signature(
    raw_body: bytes,
    settings: Settings,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
) -> None:
    if not settings.feishu_encrypt_key:
        return
    if not signature:
        raise HTTPException(status_code=403, detail="missing Feishu signature")
    if not timestamp or not nonce:
        raise HTTPException(status_code=403, detail="missing Feishu signature headers")
    sign_text = f"{timestamp or ''}{nonce or ''}{settings.feishu_encrypt_key}{raw_body.decode()}".encode()
    expected = hashlib.sha256(sign_text).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="invalid Feishu signature")


def _decrypt_payload(encrypt: str, settings: Settings) -> dict[str, Any]:
    if not settings.feishu_encrypt_key:
        raise HTTPException(status_code=400, detail="encrypted payload requires FEISHU_ENCRYPT_KEY")
    try:
        from cryptography.hazmat.primitives import hashes, padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.hashes import Hash
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="cryptography is required for encrypted Feishu events") from exc

    digest = Hash(hashes.SHA256())
    digest.update(settings.feishu_encrypt_key.encode())
    key = digest.finalize()
    encrypted = base64.b64decode(encrypt)
    if len(encrypted) <= 16:
        raise HTTPException(status_code=400, detail="invalid encrypted payload")

    cipher = Cipher(algorithms.AES(key), modes.CBC(encrypted[:16]))
    decryptor = cipher.decryptor()
    padded = decryptor.update(encrypted[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return json.loads(plaintext.decode("utf-8"))


def _normalize_request_payload(raw_body: bytes, settings: Settings) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if isinstance(payload, dict) and payload.get("encrypt"):
        payload = _decrypt_payload(str(payload["encrypt"]), settings)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    return payload


def _challenge_response(payload: dict[str, Any]) -> dict[str, str] | None:
    event_type = payload.get("type") or payload.get("event_type")
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    if event_type == "url_verification" or header.get("event_type") == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(status_code=400, detail="missing challenge")
        return {"challenge": challenge}
    return None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"ok": "true"}


@app.post("/feishu/events")
async def feishu_events(
    request: Request,
    background_tasks: BackgroundTasks,
    x_lark_request_timestamp: str | None = Header(default=None),
    x_lark_request_nonce: str | None = Header(default=None),
    x_lark_signature: str | None = Header(default=None),
) -> dict[str, str]:
    settings = get_settings()
    raw_body = await request.body()
    _verify_signature(raw_body, settings, x_lark_request_timestamp, x_lark_request_nonce, x_lark_signature)
    payload = _normalize_request_payload(raw_body, settings)
    _verify_token(payload, settings)

    challenge = _challenge_response(payload)
    if challenge is not None:
        return challenge

    event_type = payload.get("type") or payload.get("event_type")
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    if event_type != "im.message.receive_v1" and header.get("event_type") != "im.message.receive_v1":
        return {"status": "ignored"}

    event = event_from_payload(payload)
    if event is None:
        return {"status": "ignored"}
    background_tasks.add_task(process_message_event, event, settings)
    return {"status": "accepted"}
