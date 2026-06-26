import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_runtime.yunting.api import YuntingClient, default_time_window, message_tree_preview, write_raw_run
from agent_runtime.yunting.common import now_ts
from agent_runtime.yunting.doris import DorisStreamLoadAdapter
from agent_runtime.yunting.pipeline import build_yunting_layers, extract_sessions, load_raw_sessions, load_raw_sessions_from_dir, write_layers
from agent_runtime.yunting.qdrant import (
    OpenAICompatibleEmbeddingProvider,
    QdrantAdapter,
    media_points_from_ads,
    text_points_from_ads,
    text_points_from_vectors,
)


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
    sessions, pages = client.pull_service_sessions(
        project_id=project_id,
        start_time=start_time,
        end_time=end_time,
        limit=args.limit,
        max_pages=args.max_pages,
        max_empty_pages=args.max_empty_pages,
    )
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
        max_empty_pages=args.max_empty_pages,
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
    for path in _layer_jsonl_paths(layers_dir):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        plans.append(adapter.dry_run(path.stem, rows, run_id=args.run_id or layers_dir.name).__dict__)
    print(json.dumps(plans, ensure_ascii=False, indent=2))


def _build_doris_adapter(database: str = "") -> DorisStreamLoadAdapter:
    return DorisStreamLoadAdapter(
        hosts=[host for host in env("DORIS_STREAM_LOAD_HOSTS").split(",") if host],
        port=int(env("DORIS_STREAM_LOAD_PORT", "8040")),
        user=env("DORIS_USER"),
        password=env("DORIS_PASSWORD"),
        database=database or env("YUNTING_DORIS_DATABASE", "agent_runtime"),
    )


def cmd_stream_load_doris(args: argparse.Namespace) -> None:
    layers_dir = Path(args.layers_dir)
    adapter = _build_doris_adapter(args.database)
    results = []
    for batch_no, path in enumerate(_layer_jsonl_paths(layers_dir), start=1):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if args.skip_empty and not rows:
            continue
        result = adapter.stream_load(path.stem, rows, run_id=args.run_id or layers_dir.name, batch_no=batch_no)
        results.append(
            {
                "table": path.stem,
                "row_count": len(rows),
                "status": result.get("Status"),
                "label": result.get("Label") or f"yt_{path.stem}_{args.run_id or layers_dir.name}_{batch_no:04d}",
                "idempotent_success": bool(result.get("IdempotentSuccess")),
            }
        )
    print(json.dumps({"run_id": args.run_id or layers_dir.name, "tables": results}, ensure_ascii=False, indent=2))


def _layer_jsonl_paths(layers_dir: Path) -> list[Path]:
    paths_by_name = {path.stem: path for path in layers_dir.glob("*.jsonl")}
    from agent_runtime.yunting.tables import DORIS_TABLES, LAYER_ORDER

    layer_rank = {layer: index for index, layer in enumerate(LAYER_ORDER)}
    names = sorted(
        paths_by_name,
        key=lambda name: (layer_rank.get(DORIS_TABLES[name].layer, 999) if name in DORIS_TABLES else 999, name),
    )
    return [paths_by_name[name] for name in names]


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


def cmd_verify_counts(args: argparse.Namespace) -> None:
    layers_dir = Path(args.layers_dir)
    manifest_path = layers_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    table_row_counts = manifest.get("table_row_counts", {})
    mismatches: list[dict[str, Any]] = []
    for path in _layer_jsonl_paths(layers_dir):
        actual = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        expected = int(table_row_counts.get(path.stem, -1))
        if expected != actual:
            mismatches.append({"table": path.stem, "expected": expected, "actual": actual})
    result: dict[str, Any] = {
        "run_id": manifest.get("run_id", layers_dir.name),
        "layers_dir": str(layers_dir),
        "jsonl_table_count": len(table_row_counts),
        "jsonl_mismatches": mismatches,
    }
    if args.check_qdrant:
        text_rows = _load_jsonl(layers_dir / "ads_agent_yunting_faq_vector_api_d.jsonl")
        text_collection = args.collection or env("QDRANT_TEXT_COLLECTION", "yunting_service_text_v1_dev")
        data_version = args.data_version or str(manifest.get("run_id") or layers_dir.name)
        adapter = QdrantAdapter(url=env("QDRANT_URL", "http://localhost:6333"), api_key=env("QDRANT_API_KEY"))
        qdrant_count = adapter.count_by_data_version(text_collection, data_version)
        result["qdrant"] = {
            "collection": text_collection,
            "data_version": data_version,
            "expected_text_points": len(text_rows),
            "actual_text_points": qdrant_count,
            "matches": qdrant_count == len(text_rows),
        }
        if qdrant_count != len(text_rows):
            mismatches.append({"table": "qdrant_text_points", "expected": len(text_rows), "actual": qdrant_count})
    result["ok"] = not mismatches
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if mismatches:
        raise SystemExit(1)


def _ensure_dev_collection_name(name: str) -> None:
    if not name.endswith("_dev"):
        raise SystemExit(f"Mock Qdrant upsert is only allowed for *_dev collections, got {name!r}.")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _batches(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _load_qdrant_rows(layers_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    text_rows = _load_jsonl(layers_dir / "ads_agent_yunting_faq_vector_api_d.jsonl")
    media_rows = _load_jsonl(layers_dir / "ads_agent_yunting_media_vector_api_d.jsonl")
    return text_rows, media_rows


def _qdrant_collections(args: argparse.Namespace) -> tuple[str, str]:
    return (
        args.collection or env("QDRANT_TEXT_COLLECTION", "yunting_service_text_v1_dev"),
        args.media_collection or env("QDRANT_MEDIA_COLLECTION", "yunting_service_media_v1_dev"),
    )


def _update_rows_for_sync(rows: list[dict[str, Any]], *, status: str, error_message: str = "") -> list[dict[str, Any]]:
    synced_at = now_ts() if status == "synced" else None
    return [
        {
            **row,
            "sync_status": status,
            "last_synced_at": synced_at,
            "error_message": error_message,
            "update_time": now_ts(),
        }
        for row in rows
    ]


def _writeback_ads_rows(table_name: str, rows: list[dict[str, Any]], *, run_id: str, batch_no: int) -> dict[str, Any] | None:
    if not env("DORIS_STREAM_LOAD_HOSTS"):
        return None
    adapter = _build_doris_adapter()
    return adapter.stream_load(table_name, rows, run_id=run_id, batch_no=batch_no)


def cmd_mock_upsert_qdrant_dev(args: argparse.Namespace) -> None:
    layers_dir = Path(args.layers_dir)
    text_rows, media_rows = _load_qdrant_rows(layers_dir)
    text_points = text_points_from_ads(text_rows, mock_dimension=args.text_dimension)
    media_points = media_points_from_ads(media_rows, mock_dimension=args.media_dimension)
    text_collection, media_collection = _qdrant_collections(args)
    _ensure_dev_collection_name(text_collection)
    _ensure_dev_collection_name(media_collection)
    adapter = QdrantAdapter(url=env("QDRANT_URL", "http://localhost:6333"), api_key=env("QDRANT_API_KEY"))

    summary: dict[str, Any] = {"text": {}, "media": {}}
    if text_points:
        summary["text"]["collection"] = adapter.ensure_collection(text_collection, vector_size=args.text_dimension)
        adapter.ensure_keyword_payload_index(text_collection, "unique_id")
        text_unique_ids = sorted({point.payload["unique_id"] for point in text_points if point.payload.get("unique_id")})
        for unique_id in text_unique_ids:
            adapter.delete_by_unique_id(text_collection, unique_id)
        upserted = 0
        for batch in _batches(text_points, args.batch_size):
            adapter.upsert(text_collection, batch)
            upserted += len(batch)
        summary["text"].update({"deleted_unique_ids": len(text_unique_ids), "upserted_points": upserted})
    if media_points:
        summary["media"]["collection"] = adapter.ensure_collection(media_collection, vector_size=args.media_dimension)
        adapter.ensure_keyword_payload_index(media_collection, "unique_id")
        media_unique_ids = sorted({point.payload["unique_id"] for point in media_points if point.payload.get("unique_id")})
        for unique_id in media_unique_ids:
            adapter.delete_by_unique_id(media_collection, unique_id)
        upserted = 0
        for batch in _batches(media_points, args.batch_size):
            adapter.upsert(media_collection, batch)
            upserted += len(batch)
        summary["media"].update({"deleted_unique_ids": len(media_unique_ids), "upserted_points": upserted})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_upsert_qdrant(args: argparse.Namespace) -> None:
    layers_dir = Path(args.layers_dir)
    text_rows, media_rows = _load_qdrant_rows(layers_dir)
    text_collection, media_collection = _qdrant_collections(args)
    provider = OpenAICompatibleEmbeddingProvider.from_env()
    if provider.model != args.text_model:
        raise SystemExit(f"Embedding provider model {provider.model} does not match --text-model {args.text_model}.")
    if provider.dimension != args.text_dimension:
        raise SystemExit(f"Embedding provider dimension {provider.dimension} does not match --text-dimension {args.text_dimension}.")
    if not args.skip_doris_writeback and not env("DORIS_STREAM_LOAD_HOSTS"):
        raise SystemExit("DORIS_STREAM_LOAD_HOSTS is required for production upsert writeback; use --skip-doris-writeback only for isolated validation.")

    adapter = QdrantAdapter(url=env("QDRANT_URL", "http://localhost:6333"), api_key=env("QDRANT_API_KEY"))
    summary: dict[str, Any] = {"text": {}, "media": {"skipped_points": len(media_rows), "reason": "media_semantic_embedding_not_configured"}}
    if text_rows:
        summary["text"]["collection"] = adapter.ensure_collection(text_collection, vector_size=args.text_dimension)
        adapter.ensure_keyword_payload_index(text_collection, "unique_id")
        upserted = 0
        embedding_batches = 0
        data_version_by_unique_id: dict[str, str] = {}
        for row_batch in _batches(text_rows, args.batch_size):
            vectors = provider.embed_texts([row["embedding_text"] for row in row_batch])
            text_points = text_points_from_vectors(
                row_batch,
                vectors,
                collection=text_collection,
                vector_model=args.text_model,
                vector_dimension=args.text_dimension,
                backend=provider.backend,
            )
            adapter.upsert(text_collection, text_points)
            upserted += len(text_points)
            embedding_batches += 1
            for point in text_points:
                unique_id = point.payload.get("unique_id")
                data_version = point.payload.get("data_version", "")
                if unique_id and data_version:
                    data_version_by_unique_id[unique_id] = data_version
            if embedding_batches == 1 or embedding_batches % 10 == 0 or upserted == len(text_rows):
                print(
                    f"upsert-qdrant progress: {upserted}/{len(text_rows)} text points across {embedding_batches} embedding batches",
                    file=sys.stderr,
                    flush=True,
                )
        for unique_id, data_version in sorted(data_version_by_unique_id.items()):
            if data_version:
                adapter.delete_stale_by_unique_id(text_collection, unique_id, data_version)
        writeback = None
        if not args.skip_doris_writeback:
            writeback = _writeback_ads_rows(
                "ads_agent_yunting_faq_vector_api_d",
                _update_rows_for_sync(text_rows, status="synced"),
                run_id=args.run_id or layers_dir.name,
                batch_no=9001,
            )
        summary["text"].update(
            {
                "upserted_points": upserted,
                "embedding_batches": embedding_batches,
                "stale_cleanup_unique_ids": len(data_version_by_unique_id),
                "doris_writeback": bool(writeback) if not args.skip_doris_writeback else "skipped",
            }
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


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
    pull.add_argument("--max-pages", type=int, default=100)
    pull.add_argument("--max-empty-pages", type=int, default=2)
    pull.add_argument("--timeout-seconds", type=float, default=60.0)
    pull.set_defaults(func=cmd_pull_latest_10)

    pull_range = sub.add_parser("pull-range", help="Pull all Yunting service pages for a time window into gitignored data/yunting.")
    pull_range.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    pull_range.add_argument("--run-id", default="")
    pull_range.add_argument("--base-url", default="")
    pull_range.add_argument("--project-id", default="")
    pull_range.add_argument("--start-time", required=True)
    pull_range.add_argument("--end-time", required=True)
    pull_range.add_argument("--max-pages", type=int, default=1000, help="Hard page safety limit.")
    pull_range.add_argument("--max-empty-pages", type=int, default=2)
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

    doris_load = sub.add_parser("stream-load-doris", help="Stream Load generated layer JSONL into Doris in ETL layer order.")
    doris_load.add_argument("--layers-dir", required=True)
    doris_load.add_argument("--run-id", default="")
    doris_load.add_argument("--database", default="")
    doris_load.add_argument("--skip-empty", action="store_true")
    doris_load.set_defaults(func=cmd_stream_load_doris)

    qdrant = sub.add_parser("dry-run-qdrant", help="Print Qdrant upsert plan from ADS vector rows.")
    qdrant.add_argument("--layers-dir", required=True)
    qdrant.add_argument("--collection", default="")
    qdrant.add_argument("--media-collection", default="")
    qdrant.add_argument("--mock-dimension", type=int, default=8)
    qdrant.set_defaults(func=cmd_dry_run_qdrant)

    verify = sub.add_parser("verify-counts", help="Verify layer JSONL counts against manifest and optionally Qdrant.")
    verify.add_argument("--layers-dir", required=True)
    verify.add_argument("--check-qdrant", action="store_true")
    verify.add_argument("--collection", default="")
    verify.add_argument("--data-version", default="")
    verify.set_defaults(func=cmd_verify_counts)

    qdrant_mock_upsert = sub.add_parser("mock-upsert-qdrant-dev", help="Upsert deterministic mock vectors into *_dev Qdrant collections only.")
    qdrant_mock_upsert.add_argument("--layers-dir", required=True)
    qdrant_mock_upsert.add_argument("--collection", default="")
    qdrant_mock_upsert.add_argument("--media-collection", default="")
    qdrant_mock_upsert.add_argument("--text-dimension", type=int, default=1024)
    qdrant_mock_upsert.add_argument("--media-dimension", type=int, default=1024)
    qdrant_mock_upsert.add_argument("--batch-size", type=int, default=256)
    qdrant_mock_upsert.set_defaults(func=cmd_mock_upsert_qdrant_dev)

    qdrant_upsert = sub.add_parser("upsert-qdrant", help="Upsert real semantic text vectors into Qdrant and write back ADS sync status.")
    qdrant_upsert.add_argument("--layers-dir", required=True)
    qdrant_upsert.add_argument("--collection", default="")
    qdrant_upsert.add_argument("--media-collection", default="")
    qdrant_upsert.add_argument("--run-id", default="")
    qdrant_upsert.add_argument("--text-model", default=env("YUNTING_TEXT_EMBEDDING_MODEL", "text-embedding-v4"))
    qdrant_upsert.add_argument("--text-dimension", type=int, default=int(env("YUNTING_TEXT_EMBEDDING_DIMENSION", "1024")))
    qdrant_upsert.add_argument("--batch-size", type=int, default=10)
    qdrant_upsert.add_argument("--skip-doris-writeback", action="store_true")
    qdrant_upsert.set_defaults(func=cmd_upsert_qdrant)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.func is None:
        parser.print_help()
        return
    args.func(args)
