"""Dagster definitions for the Yunting service FAQ pipeline.

The module remains importable when Dagster is absent so application tests do not
take a scheduler dependency. Production loads this module as its own code
location from ``/opt/dagster/workspace.yaml``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


try:  # pragma: no cover - Dagster is only required in the scheduler runtime.
    import dagster as dg
except Exception:  # pragma: no cover
    dg = None  # type: ignore[assignment]


DAGSTER_AVAILABLE = dg is not None
YUNTING_WEEKLY_CRON = "0 3 * * 1"
YUNTING_EXECUTION_TIMEZONE = "Asia/Shanghai"
YUNTING_SERVICE_GROUP = "yunting_service_support_faq"

AGENT_RUNTIME_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_ROOT = "data/yunting/service"


def _previous_week_window() -> tuple[str, str]:
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(YUNTING_EXECUTION_TIMEZONE))
    except Exception:
        now = datetime.now()
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return (this_monday - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"), this_monday.strftime("%Y-%m-%d %H:%M:%S")


def _run_id(context: Any) -> str:
    return os.getenv("YUNTING_SERVICE_RUN_ID") or f"yt_dagster_{context.run_id[:8]}"


def _data_root() -> str:
    return os.getenv("YUNTING_SERVICE_DATA_ROOT", DEFAULT_DATA_ROOT)


def _window() -> tuple[str, str]:
    start = os.getenv("YUNTING_SERVICE_START_TIME")
    end = os.getenv("YUNTING_SERVICE_END_TIME")
    if start and end:
        return start, end
    return _previous_week_window()


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _command_env() -> dict[str, str]:
    env = os.environ.copy()
    env_path = Path(os.getenv("AGENT_RUNTIME_ENV", str(AGENT_RUNTIME_ROOT / ".env")))
    env.update(_load_env_file(env_path))
    return env


def _run_cli(context: Any, args: list[str]) -> None:
    python = os.getenv("AGENT_RUNTIME_PYTHON", str(AGENT_RUNTIME_ROOT / ".venv" / "bin" / "python"))
    process = subprocess.run(
        [python, "scripts/yunting_service_pipeline.py", *args],
        cwd=AGENT_RUNTIME_ROOT,
        env=_command_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if process.stdout:
        context.log.info(process.stdout.strip())
    if process.stderr:
        context.log.warning(process.stderr.strip())
    if process.returncode != 0:
        raise dg.Failure(  # type: ignore[union-attr]
            description=f"Yunting CLI failed: {' '.join(args)}",
            metadata={"stdout": process.stdout[-4000:], "stderr": process.stderr[-4000:]},
        )


if DAGSTER_AVAILABLE:  # pragma: no cover - exercised on the scheduler host.

    @dg.asset(  # type: ignore[union-attr]
        group_name=YUNTING_SERVICE_GROUP,
        owners=["team:data-engineering"],
        tags={"kind": "api-raw", "source": "yunting", "sensitivity": "internal"},
        description="拉取云听客服会话 API raw pages，并写入 gitignored data/yunting/service/raw。",
    )
    def yunting_service_pull_raw_pages(context):
        run_id = _run_id(context)
        window_start, window_end = _window()
        data_root = _data_root()
        _run_cli(
            context,
            [
                "pull-range",
                "--data-root",
                data_root,
                "--run-id",
                run_id,
                "--start-time",
                window_start,
                "--end-time",
                window_end,
                "--max-pages",
                os.getenv("YUNTING_SERVICE_MAX_PAGES", "1000"),
                "--max-empty-pages",
                os.getenv("YUNTING_SERVICE_MAX_EMPTY_PAGES", "2"),
                "--sleep-seconds",
                "0",
            ],
        )
        return dg.MaterializeResult(  # type: ignore[union-attr]
            metadata={
                "run_id": run_id,
                "window_start": window_start,
                "window_end": window_end,
                "raw_dir": dg.MetadataValue.path(str(AGENT_RUNTIME_ROOT / data_root / "raw" / run_id)),  # type: ignore[union-attr]
            }
        )

    @dg.asset(  # type: ignore[union-attr]
        deps=[yunting_service_pull_raw_pages],
        group_name=YUNTING_SERVICE_GROUP,
        owners=["team:data-engineering"],
        tags={"kind": "transform", "storage": "jsonl", "sensitivity": "internal"},
        description="从 raw sessions 生成 ODS/STD/DWD/DIM/DWS/ADS/DM 分层 JSONL 和 manifest。",
    )
    def yunting_service_build_layers(context):
        run_id = _run_id(context)
        data_root = _data_root()
        _run_cli(
            context,
            [
                "dry-run-layers",
                "--input-dir",
                f"{data_root}/raw/{run_id}/sessions",
                "--output-dir",
                f"{data_root}/layers",
                "--run-id",
                run_id,
            ],
        )
        return dg.MaterializeResult(  # type: ignore[union-attr]
            metadata={
                "run_id": run_id,
                "layers_dir": dg.MetadataValue.path(str(AGENT_RUNTIME_ROOT / data_root / "layers" / run_id)),  # type: ignore[union-attr]
            }
        )

    @dg.asset(  # type: ignore[union-attr]
        deps=[yunting_service_build_layers],
        group_name=YUNTING_SERVICE_GROUP,
        owners=["team:data-engineering"],
        tags={"kind": "stream-load", "storage": "doris", "sensitivity": "internal"},
        description="按固定层级顺序 Stream Load 云听分层 JSONL 到 Doris。",
    )
    def yunting_service_stream_load_doris(context):
        run_id = _run_id(context)
        data_root = _data_root()
        _run_cli(context, ["stream-load-doris", "--layers-dir", f"{data_root}/layers/{run_id}", "--run-id", run_id, "--skip-empty"])
        return dg.MaterializeResult(metadata={"run_id": run_id})  # type: ignore[union-attr]

    @dg.asset(  # type: ignore[union-attr]
        deps=[yunting_service_stream_load_doris],
        group_name=YUNTING_SERVICE_GROUP,
        owners=["team:data-engineering"],
        tags={"kind": "vector-upsert", "storage": "qdrant", "sensitivity": "internal"},
        description="使用真实文本 embedding 写入 Qdrant dev collection，并回写 Doris ADS 同步状态。",
    )
    def yunting_service_upsert_qdrant(context):
        run_id = _run_id(context)
        data_root = _data_root()
        _run_cli(context, ["upsert-qdrant", "--layers-dir", f"{data_root}/layers/{run_id}", "--run-id", run_id])
        return dg.MaterializeResult(metadata={"run_id": run_id})  # type: ignore[union-attr]

    @dg.asset(  # type: ignore[union-attr]
        deps=[yunting_service_upsert_qdrant],
        group_name=YUNTING_SERVICE_GROUP,
        owners=["team:data-engineering"],
        tags={"kind": "verification", "storage": "doris-qdrant", "sensitivity": "internal"},
        description="核验 manifest、JSONL 行数和 Qdrant data_version 点数一致。",
    )
    def yunting_service_verify_counts(context):
        run_id = _run_id(context)
        data_root = _data_root()
        _run_cli(
            context,
            [
                "verify-counts",
                "--layers-dir",
                f"{data_root}/layers/{run_id}",
                "--check-qdrant",
                "--data-version",
                run_id,
            ],
        )
        return dg.MaterializeResult(metadata={"run_id": run_id})  # type: ignore[union-attr]

    yunting_service_weekly_ingest_job = dg.define_asset_job(  # type: ignore[union-attr]
        name="yunting_service_weekly_ingest_job",
        selection=dg.AssetSelection.groups(YUNTING_SERVICE_GROUP),  # type: ignore[union-attr]
    )
    yunting_service_weekly_schedule = dg.ScheduleDefinition(  # type: ignore[union-attr]
        name="yunting_service_weekly_schedule",
        job=yunting_service_weekly_ingest_job,
        cron_schedule=YUNTING_WEEKLY_CRON,
        execution_timezone=YUNTING_EXECUTION_TIMEZONE,
    )
    definitions = dg.Definitions(  # type: ignore[union-attr]
        assets=[
            yunting_service_pull_raw_pages,
            yunting_service_build_layers,
            yunting_service_stream_load_doris,
            yunting_service_upsert_qdrant,
            yunting_service_verify_counts,
        ],
        jobs=[yunting_service_weekly_ingest_job],
        schedules=[yunting_service_weekly_schedule],
    )
else:
    yunting_service_weekly_ingest_job = None
    yunting_service_weekly_schedule = None
    definitions = None
