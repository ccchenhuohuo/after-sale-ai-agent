"""Optional Dagster handoff for the server-side scheduler.

The production Dagster deployment is maintained on the server. This module stays
importable without Dagster so the application and CI do not gain a hard runtime
dependency; server definitions can call the pure Python pipeline entrypoints.
"""

from __future__ import annotations

import os


if os.getenv("YUNTING_ENABLE_DAGSTER_DEFS") == "1":  # pragma: no cover - server-only path.
    import dagster as dg
else:  # Avoid importing Dagster on local/CI machines where it may be absent or slow to initialize.
    dg = None


DAGSTER_AVAILABLE = dg is not None
YUNTING_WEEKLY_CRON = "0 3 * * 1"
YUNTING_EXECUTION_TIMEZONE = "Asia/Shanghai"


if DAGSTER_AVAILABLE:  # pragma: no cover
    yunting_service_weekly_ingest_job = dg.define_asset_job("yunting_service_weekly_ingest_job", selection=[])
    yunting_service_weekly_schedule = dg.ScheduleDefinition(
        job=yunting_service_weekly_ingest_job,
        cron_schedule=YUNTING_WEEKLY_CRON,
        execution_timezone=YUNTING_EXECUTION_TIMEZONE,
        description="Server-owned weekly Yunting customer-service ingestion entrypoint.",
    )
    definitions = dg.Definitions(jobs=[yunting_service_weekly_ingest_job], schedules=[yunting_service_weekly_schedule])
else:
    yunting_service_weekly_ingest_job = None
    yunting_service_weekly_schedule = None
    definitions = None
