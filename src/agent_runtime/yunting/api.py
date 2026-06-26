from __future__ import annotations

import json
import time
from hashlib import sha256
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


class YuntingClient:
    def __init__(self, *, base_url: str, access_token: str, timeout_seconds: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_source(
        cls,
        *,
        base_url: str,
        source: str,
        third_party_id: str,
        timeout_seconds: float = 60.0,
    ) -> "YuntingClient":
        token = fetch_access_token(
            base_url=base_url,
            source=source,
            third_party_id=third_party_id,
            timeout_seconds=timeout_seconds,
        )
        return cls(base_url=base_url, access_token=token, timeout_seconds=timeout_seconds)

    def pull_service_page(
        self,
        *,
        project_id: str,
        start_time: str = "",
        end_time: str = "",
        page_token: str = "",
    ) -> dict[str, Any]:
        request_body = {
            "projectId": project_id,
            "startTime": start_time,
            "endTime": end_time,
            "pageToken": page_token,
        }
        response = httpx.post(
            f"{self.base_url}/api/comment/v1/service/pull",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json=request_body,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload["_request"] = request_body
            code = payload.get("code")
            if code not in (20000, "20000"):
                message = payload.get("msg", "")
                trace_id = payload.get("traceId", "")
                raise RuntimeError(f"Yunting service API failed: code={code}, msg={message}, traceId={trace_id}")
        return payload

    def pull_service_sessions(
        self,
        *,
        project_id: str,
        start_time: str = "",
        end_time: str = "",
        limit: int = 10,
        max_pages: int = 100,
        max_empty_pages: int = 2,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sessions: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        page_token = ""
        seen_tokens: set[str] = set()
        empty_pages = 0
        page_limit = max_pages if max_pages > 0 else 100
        while len(sessions) < limit:
            if page_token:
                if page_token in seen_tokens:
                    raise RuntimeError(f"Yunting service API repeated pageToken: {page_token}")
                seen_tokens.add(page_token)
            if len(pages) >= page_limit:
                raise RuntimeError(f"Yunting service API exceeded max_pages={page_limit}")
            payload = self.pull_service_page(
                project_id=project_id,
                start_time=start_time,
                end_time=end_time,
                page_token=page_token,
            )
            pages.append(payload)
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            data = result.get("data", []) if isinstance(result, dict) else []
            if isinstance(data, list):
                sessions.extend(item for item in data if isinstance(item, dict))
                empty_pages = 0 if data else empty_pages + 1
            else:
                empty_pages += 1
            has_more = bool(result.get("hasMore")) if isinstance(result, dict) else False
            page_token = str(result.get("pageToken") or "") if isinstance(result, dict) else ""
            if has_more and empty_pages > max_empty_pages:
                trace_id = payload.get("traceId", "") if isinstance(payload, dict) else ""
                raise RuntimeError(f"Yunting service API returned too many empty pages: count={empty_pages}, traceId={trace_id}")
            if not has_more or not page_token:
                break
        return sessions[:limit], pages

    def pull_service_pages(
        self,
        *,
        project_id: str,
        start_time: str = "",
        end_time: str = "",
        max_pages: int = 0,
        max_empty_pages: int = 2,
        sleep_seconds: float = 0.0,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sessions: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        page_token = ""
        seen_tokens: set[str] = set()
        empty_pages = 0
        page_limit = max_pages if max_pages > 0 else 1000
        while True:
            if page_token:
                if page_token in seen_tokens:
                    raise RuntimeError(f"Yunting service API repeated pageToken: {page_token}")
                seen_tokens.add(page_token)
            if len(pages) >= page_limit:
                raise RuntimeError(f"Yunting service API exceeded max_pages={page_limit}")
            payload = self.pull_service_page(
                project_id=project_id,
                start_time=start_time,
                end_time=end_time,
                page_token=page_token,
            )
            pages.append(payload)
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            data = result.get("data", []) if isinstance(result, dict) else []
            if isinstance(data, list):
                sessions.extend(item for item in data if isinstance(item, dict))
                empty_pages = 0 if data else empty_pages + 1
            else:
                empty_pages += 1
            has_more = bool(result.get("hasMore")) if isinstance(result, dict) else False
            page_token = str(result.get("pageToken") or "") if isinstance(result, dict) else ""
            if has_more and empty_pages > max_empty_pages:
                trace_id = payload.get("traceId", "") if isinstance(payload, dict) else ""
                raise RuntimeError(f"Yunting service API returned too many empty pages: count={empty_pages}, traceId={trace_id}")
            if not has_more or not page_token:
                break
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        return sessions, pages


def fetch_access_token(*, base_url: str, source: str, third_party_id: str, timeout_seconds: float = 60.0) -> str:
    response = httpx.get(
        f"{base_url.rstrip('/')}/oauth2/token",
        params={"source": source, "third_party_id": third_party_id},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (20000, "20000") or not payload.get("result", {}).get("access_token"):
        raise RuntimeError(f"Yunting token fetch failed: code={payload.get('code')}, msg={payload.get('msg')}")
    return str(payload["result"]["access_token"])


def default_time_window(days: int = 14) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def write_raw_run(root: Path, run_id: str, sessions: list[dict[str, Any]], pages: list[dict[str, Any]]) -> None:
    api_dir = root / "raw" / run_id / "api_pages"
    session_dir = root / "raw" / run_id / "sessions"
    api_dir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    for index, page in enumerate(pages, start=1):
        (api_dir / f"page_{index:04d}.json").write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")
    for index, session in enumerate(sessions, start=1):
        raw_text = json.dumps(session, ensure_ascii=False, sort_keys=True)
        fallback_id = f"session_{index:04d}_{sha256(raw_text.encode('utf-8')).hexdigest()[:12]}"
        unique_id = str(session.get("unique") or session.get("unique_id") or fallback_id)
        (session_dir / f"{unique_id}.json").write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def message_tree_preview(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    preview: list[dict[str, Any]] = []
    for session in sessions:
        contents = session.get("contents") or []
        if not isinstance(contents, list):
            contents = []
        preview.append(
            {
                "unique": session.get("unique") or session.get("unique_id"),
                "sourceName": session.get("sourceName"),
                "shopName": session.get("shopName"),
                "sessionType": session.get("sessionType"),
                "contentCount": len(contents),
                "messages": [
                    {
                        "contentId": message.get("contentId"),
                        "publishTime": message.get("publishTime"),
                        "role": message.get("role"),
                        "messageType": message.get("messageType"),
                        "content": message.get("content"),
                    }
                    for message in contents[:50]
                    if isinstance(message, dict)
                ],
            }
        )
    return {"session_count": len(sessions), "sessions": preview}
