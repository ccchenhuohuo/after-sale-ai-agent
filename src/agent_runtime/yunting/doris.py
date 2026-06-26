from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from agent_runtime.yunting.tables import DORIS_TABLES, TableSpec


@dataclass(frozen=True)
class StreamLoadPlan:
    database: str
    table: str
    label: str
    columns: list[str]
    row_count: int
    dry_run: bool = True


def columns_for(table_name: str, rows: list[dict[str, Any]]) -> list[str]:
    spec = DORIS_TABLES.get(table_name)
    if spec and spec.columns:
        return list(spec.columns)
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    return seen


def build_stream_load_plan(
    *,
    database: str,
    table_name: str,
    rows: list[dict[str, Any]],
    run_id: str,
    batch_no: int = 1,
) -> StreamLoadPlan:
    return StreamLoadPlan(
        database=database,
        table=table_name,
        label=f"yt_{table_name}_{run_id}_{batch_no:04d}",
        columns=columns_for(table_name, rows),
        row_count=len(rows),
        dry_run=True,
    )


class DorisStreamLoadAdapter:
    def __init__(
        self,
        *,
        hosts: list[str],
        port: int = 8040,
        user: str = "",
        password: str = "",
        database: str = "agent_runtime",
        method: str = "POST",
    ) -> None:
        self.hosts = hosts
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.method = method

    def dry_run(self, table_name: str, rows: list[dict[str, Any]], *, run_id: str, batch_no: int = 1) -> StreamLoadPlan:
        return build_stream_load_plan(database=self.database, table_name=table_name, rows=rows, run_id=run_id, batch_no=batch_no)

    def stream_load(self, table_name: str, rows: list[dict[str, Any]], *, run_id: str, batch_no: int = 1) -> dict[str, Any]:
        plan = self.dry_run(table_name, rows, run_id=run_id, batch_no=batch_no)
        if not self.hosts:
            raise ValueError("Doris hosts are required for real Stream Load")
        body = json.dumps(rows, ensure_ascii=False)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Expect": "100-continue",
            "format": "json",
            "strip_outer_array": "true",
            "disable_stream_load_sql_check": "true",
            "ignore_json_size": "true",
            "columns": ",".join(plan.columns),
            "timezone": "+08:00",
            "label": plan.label,
        }
        last_error: Exception | None = None
        for host in self.hosts:
            try:
                response = httpx.request(
                    self.method,
                    f"http://{host}:{self.port}/api/{self.database}/{table_name}/_stream_load",
                    content=body.encode("utf-8"),
                    headers=headers,
                    auth=(self.user, self.password),
                    timeout=120,
                )
                response.raise_for_status()
                result = response.json()
                status = str(result.get("Status", ""))
                filtered_rows = int(result.get("NumberFilteredRows", 0) or 0)
                loaded_rows = int(result.get("NumberLoadedRows", 0) or 0)
                existing_job_status = str(result.get("ExistingJobStatus", ""))
                if status == "Label Already Exists" and existing_job_status.upper() == "FINISHED":
                    return {**result, "IdempotentSuccess": True}
                if status == "Publish Timeout":
                    raise RuntimeError(f"Doris Stream Load publish timeout for {table_name}; label state check required: {json.dumps(result, ensure_ascii=False)}")
                if filtered_rows != 0:
                    raise RuntimeError(f"Doris Stream Load filtered rows for {table_name}: {json.dumps(result, ensure_ascii=False)}")
                if status != "Success" or loaded_rows != len(rows):
                    raise RuntimeError(f"Doris Stream Load rejected rows for {table_name}: {json.dumps(result, ensure_ascii=False)}")
                return result
            except Exception as exc:  # pragma: no cover - network fallback path
                last_error = exc
        raise RuntimeError(f"Doris Stream Load failed for {table_name}: {last_error}") from last_error


def table_specs_by_layer() -> dict[str, list[TableSpec]]:
    grouped: dict[str, list[TableSpec]] = {}
    for spec in DORIS_TABLES.values():
        grouped.setdefault(spec.layer, []).append(spec)
    return grouped
