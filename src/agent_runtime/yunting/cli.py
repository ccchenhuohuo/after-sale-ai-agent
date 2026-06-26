import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_runtime.yunting.api import YuntingClient, default_time_window, message_tree_preview, write_raw_run
from agent_runtime.yunting.doris import DorisStreamLoadAdapter
from agent_runtime.yunting.pipeline import build_yunting_layers, extract_sessions, load_raw_sessions, load_raw_sessions_from_dir, write_layers
from agent_runtime.yunting.qdrant import QdrantAdapter, media_points_from_ads, text_points_from_ads


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_ROOT = ROOT / "data" / "yunting" / "service"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def run_id() -> str:
    return "yt_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def _build_yunting_client(args: argparse.Namespace) -> YuntingClient:
    base_url = args.base_url or env("YUNTING_API_BASE_URL", "https://opendata.yuntingai.com")
    access_token = env("YUNTING_ACCESS_TOKEN")
    if access_token:
        return YuntingClient(base_url=base_url, access_token=access_token, timeout_seconds=args.timeout_seconds)
    source = env("YUNTING_SOURCE")
    third_party_id = env("YUNTING_THIRD_PARTY_ID")
    if not source or not third_party_id:
        raise SystemExit("Set YUNTING_ACCESS_TOKEN or both YUNTING_SOURCE and YUNTING_THIRD_PARTY_ID.")
    return YuntingClient.from_source(
        base_url=base_url,
        source=source,
        third_party_id=third_party_id,
        timeout_seconds=args.timeout_seconds,
    )


def cmd_pull_latest_10(args: argparse.Namespace) -> None:
    start_time, end_time = (args.start_time, args.end_time) if args.start_time and args.end_time else default_time_window(args.days)
    client = _build_yunting_client(args)
    project_id = args.project_id or env("YUNTING_PROJECT_ID")
    if not client.access_token or not project_id:
        raise SystemExit("YUNTING_PROJECT_ID is required for real API pulls.")
    current_run_id = args.run_id or run_id()
    sessions, pages = client.pull_service_sessions(project_id=project_id, start_time=start_time, end_time=end_time, limit=args.limit)
    data_root = Path(args.data_root)
    write_raw_run(data_root, current_run_id, sessions, pages)
    preview = message_tree_preview(sessions)
    preview_path = data_root / "raw" / current_run_id / "message_tree_preview.json"
    preview_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": current_run_id, "session_count": len(sessions), "preview_path": str(preview_path)}, ensure_ascii=False, indent=2))


def cmd_pull_range(args: argparse.Namespace) -> None:
    if not args.start_time or not args.end_time:
        raise SystemExit("--start-time and --end-time are required for pull-range.")
    client = _build_yunting_client(args)
    project_id = args.project_id or env("YUNTING_PROJECT_ID")
    if not client.access_token or not project_id:
        raise SystemExit("YUNTING_PROJECT_ID is required for real API pulls.")
    current_run_id = args.run_id or run_id()
    sessions, pages = client.pull_service_pages(
        project_id=project_id,
        start_time=args.start_time,
        end_time=args.end_time,
        max_pages=args.max_pages,
        sleep_seconds=args.sleep_seconds,
    )
    data_root = Path(args.data_root)
    write_raw_run(data_root, current_run_id, sessions, pages)
    preview = message_tree_preview(sessions[: args.preview_sessions])
    preview_path = data_root / "raw" / current_run_id / "message_tree_preview.json"
    preview_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "run_id": current_run_id,
                "start_time": args.start_time,
                "end_time": args.end_time,
                "page_count": len(pages),
                "session_count": len(sessions),
                "preview_path": str(preview_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _load_sessions_arg(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.input_file:
        return load_raw_sessions(Path(args.input_file))
    return load_raw_sessions_from_dir(Path(args.input_dir))


def _load_page_payloads_arg(args: argparse.Namespace) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    if args.input_file:
        path = Path(args.input_file)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [payload] if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else []

    input_dir = Path(args.input_dir)
    if input_dir.name == "sessions" and input_dir.parent.name:
        candidates.extend(sorted((input_dir.parent / "api_pages").glob("*.json")))
    candidates.extend(sorted(input_dir.rglob("api_pages/*.json")))

    payloads: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            payloads.append(payload)
    return payloads


def cmd_dry_run_layers(args: argparse.Namespace) -> None:
    current_run_id = args.run_id or run_id()
    sessions = _load_sessions_arg(args)
    page_payloads = _load_page_payloads_arg(args)
    if page_payloads:
        page_sessions: dict[str, dict[str, Any]] = {}
        for payload in page_payloads:
            for session in extract_sessions(payload):
                unique_id = str(session.get("unique") or session.get("unique_id") or "")
                if unique_id:
                    page_sessions[unique_id] = session
        if page_sessions:
            session_by_id = {str(session.get("unique") or session.get("unique_id") or ""): session for session in sessions}
            session_by_id.update(page_sessions)
            sessions = list(session_by_id.values())
    layers, manifest = build_yunting_layers(
        sessions,
        run_id=current_run_id,
        raw_file_path=args.input_file or args.input_dir,
        page_payloads=page_payloads,
        text_collection=env("QDRANT_TEXT_COLLECTION", "yunting_service_text_v1_dev"),
        media_collection=env("QDRANT_MEDIA_COLLECTION", "yunting_service_media_v1_dev"),
    )
    output_dir = Path(args.output_dir) / current_run_id
    write_layers(output_dir, layers, manifest)
    print(json.dumps({"run_id": current_run_id, "output_dir": str(output_dir), **manifest.to_dict()}, ensure_ascii=False, indent=2))


def cmd_dry_run_doris(args: argparse.Namespace) -> None:
    layers_dir = Path(args.layers_dir)
    adapter = DorisStreamLoadAdapter(
        hosts=[host for host in env("DORIS_STREAM_LOAD_HOSTS").split(",") if host],
        port=int(env("DORIS_STREAM_LOAD_PORT", "8040")),
        user=env("DORIS_USER"),
        password=env("DORIS_PASSWORD"),
        database=args.database or env("YUNTING_DORIS_DATABASE", "agent_runtime"),
    )
    plans = []
    for path in sorted(layers_dir.glob("*.jsonl")):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        plans.append(adapter.dry_run(path.stem, rows, run_id=args.run_id or layers_dir.name).__dict__)
    print(json.dumps(plans, ensure_ascii=False, indent=2))


def cmd_dry_run_qdrant(args: argparse.Namespace) -> None:
    layers_dir = Path(args.layers_dir)
    text_rows = [
        json.loads(line)
        for line in (layers_dir / "ads_agent_yunting_faq_vector_api_d.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    media_rows = [
        json.loads(line)
        for line in (layers_dir / "ads_agent_yunting_media_vector_api_d.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    text_points = text_points_from_ads(text_rows, mock_dimension=args.mock_dimension)
    media_points = media_points_from_ads(media_rows, mock_dimension=args.mock_dimension)
    text_collection = args.collection or env("QDRANT_TEXT_COLLECTION", "yunting_service_text_v1_dev")
    media_collection = args.media_collection or env("QDRANT_MEDIA_COLLECTION", "yunting_service_media_v1_dev")
    adapter = QdrantAdapter(url=env("QDRANT_URL", "http://localhost:6333"), api_key=env("QDRANT_API_KEY"))
    text_unique_ids = sorted({point.payload["unique_id"] for point in text_points if point.payload.get("unique_id")})
    media_unique_ids = sorted({point.payload["unique_id"] for point in media_points if point.payload.get("unique_id")})
    plan = {
        "text": {
            "delete_old_points": [adapter.dry_run_delete_by_unique_id(text_collection, unique_id) for unique_id in text_unique_ids],
            "upsert_new_points": adapter.dry_run_upsert(text_collection, text_points),
        },
        "media": {
            "delete_old_points": [adapter.dry_run_delete_by_unique_id(media_collection, unique_id) for unique_id in media_unique_ids],
            "upsert_new_points": adapter.dry_run_upsert(media_collection, media_points),
        },
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Yunting service conversation local pipeline.")
    parser.set_defaults(func=None)
    sub = parser.add_subparsers(dest="command")

    pull = sub.add_parser("pull-latest-10", help="Pull latest Yunting service sessions into gitignored data/yunting.")
    pull.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    pull.add_argument("--run-id", default="")
    pull.add_argument("--base-url", default="")
    pull.add_argument("--project-id", default="")
    pull.add_argument("--start-time", default="")
    pull.add_argument("--end-time", default="")
    pull.add_argument("--days", type=int, default=14)
    pull.add_argument("--limit", type=int, default=10)
    pull.add_argument("--timeout-seconds", type=float, default=60.0)
    pull.set_defaults(func=cmd_pull_latest_10)

    pull_range = sub.add_parser("pull-range", help="Pull all Yunting service pages for a time window into gitignored data/yunting.")
    pull_range.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    pull_range.add_argument("--run-id", default="")
    pull_range.add_argument("--base-url", default="")
    pull_range.add_argument("--project-id", default="")
    pull_range.add_argument("--start-time", required=True)
    pull_range.add_argument("--end-time", required=True)
    pull_range.add_argument("--max-pages", type=int, default=0, help="0 means pull until hasMore is false.")
    pull_range.add_argument("--sleep-seconds", type=float, default=0.0)
    pull_range.add_argument("--preview-sessions", type=int, default=10)
    pull_range.add_argument("--timeout-seconds", type=float, default=60.0)
    pull_range.set_defaults(func=cmd_pull_range)

    layers = sub.add_parser("dry-run-layers", help="Build Doris/Qdrant layer JSONL from raw Yunting JSON.")
    layers.add_argument("--input-file", default="")
    layers.add_argument("--input-dir", default=str(DEFAULT_DATA_ROOT / "raw"))
    layers.add_argument("--output-dir", default=str(DEFAULT_DATA_ROOT / "layers"))
    layers.add_argument("--run-id", default="")
    layers.set_defaults(func=cmd_dry_run_layers)

    doris = sub.add_parser("dry-run-doris", help="Print Doris Stream Load plans for generated layer JSONL.")
    doris.add_argument("--layers-dir", required=True)
    doris.add_argument("--run-id", default="")
    doris.add_argument("--database", default="")
    doris.set_defaults(func=cmd_dry_run_doris)

    qdrant = sub.add_parser("dry-run-qdrant", help="Print Qdrant upsert plan from ADS vector rows.")
    qdrant.add_argument("--layers-dir", required=True)
    qdrant.add_argument("--collection", default="")
    qdrant.add_argument("--media-collection", default="")
    qdrant.add_argument("--mock-dimension", type=int, default=8)
    qdrant.set_defaults(func=cmd_dry_run_qdrant)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.func is None:
        parser.print_help()
        return
    args.func(args)
