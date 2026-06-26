import json
import argparse
from pathlib import Path

from agent_runtime.yunting import dagster_defs
from agent_runtime.yunting import cli as yunting_cli
from agent_runtime.yunting.api import YuntingClient, fetch_access_token
from agent_runtime.yunting.common import SOURCE_TYPE
from agent_runtime.yunting.doris import DorisStreamLoadAdapter
from agent_runtime.yunting.pipeline import build_yunting_layers, extract_sessions
from agent_runtime.yunting.qdrant import QdrantAdapter, text_points_from_ads
from agent_runtime.yunting.tables import DORIS_TABLES


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "yunting_service_structural_sample.json"


def load_fixture_sessions():
    return extract_sessions(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_yunting_layers_cover_required_tables_and_public_fields():
    layers, manifest = build_yunting_layers(load_fixture_sessions(), run_id="test_run", raw_file_path=str(FIXTURE))

    assert set(layers) == set(DORIS_TABLES)
    assert manifest.raw_session_count == 1
    assert manifest.std_message_count == 7
    assert manifest.media_asset_count == 2
    assert manifest.faq_case_count == 1
    assert manifest.faq_chunk_count >= 4

    for table_name, rows in layers.items():
        for row in rows:
            assert row.get("create_time")
            assert row.get("update_time")
            if table_name.startswith(("ods_", "std_", "dwd_")):
                assert row.get("source_system") == "yunting" or table_name.startswith("dwd_")
            if table_name.startswith(("dws_", "ads_", "dm_")):
                assert row.get("stat_date")
                assert row.get("stat_week")


def test_topic_tags_messages_and_media_are_normalized():
    layers, _ = build_yunting_layers(load_fixture_sessions(), run_id="test_run", raw_file_path=str(FIXTURE))

    topics = layers["dim_yunting_topic_value"]
    tags = layers["dim_yunting_tag"]
    media = layers["std_api_yunting_service_media_asset_f_d"]
    answer_chunks = [row for row in layers["dws_yunting_service_faq_chunk_d"] if row["chunk_type"] == "answer_unit"]

    assert any(row["topic_name"] == "SKU" and row["topic_value"] == "SAMPLE-SKU-01" for row in topics)
    assert any(row["tag_name"] == "充电咨询" for row in tags)
    assert {row["message_type"] for row in media} == {"IMAGE", "VIDEO"}
    assert all(row["source_url"].startswith("https://example.invalid/") for row in media)
    assert len(answer_chunks) == 2
    assert not any(row["answer"] == "您好" for row in answer_chunks)
    assert not any("您好" in row["chunk_text"] for row in layers["dws_yunting_service_faq_chunk_d"])
    assert all(json.loads(row["source_content_ids_json"]) for row in answer_chunks)


def test_authority_payload_is_preserved_for_qdrant_rows():
    layers, _ = build_yunting_layers(load_fixture_sessions(), run_id="test_run", raw_file_path=str(FIXTURE))
    ads_rows = layers["ads_agent_yunting_faq_vector_api_d"]
    payload = json.loads(ads_rows[0]["payload_json"])

    assert ads_rows[0]["collection_name"] == "yunting_service_text_v1_dev"
    assert payload["source_type"] == SOURCE_TYPE
    assert payload["reference_class"] == "support_history_faq"
    assert payload["authority_level"] == "low"
    assert payload["can_be_reference"] is True

    points = text_points_from_ads(ads_rows, mock_dimension=8)
    assert points[0].id == ads_rows[0]["point_id"]
    assert len(points[0].vector) == 8
    assert points[0].payload["authority_score"] == 0.45


def test_media_ads_preserves_pending_media_object_key():
    layers, _ = build_yunting_layers(load_fixture_sessions(), run_id="test_run", raw_file_path=str(FIXTURE))
    ads_rows = layers["ads_agent_yunting_media_vector_api_d"]

    assert ads_rows
    assert {row["collection_name"] for row in ads_rows} == {"yunting_service_media_v1_dev"}
    assert all(row["media_object_key"].startswith("media/sha256/pending/") for row in ads_rows)


def test_doris_stream_load_plan_is_dry_run_and_deterministic():
    layers, _ = build_yunting_layers(load_fixture_sessions(), run_id="test_run", raw_file_path=str(FIXTURE))
    adapter = DorisStreamLoadAdapter(hosts=[], database="agent_runtime")
    rows = layers["std_api_yunting_service_message_f_d"]

    plan = adapter.dry_run("std_api_yunting_service_message_f_d", rows, run_id="test_run", batch_no=2)

    assert plan.database == "agent_runtime"
    assert plan.table == "std_api_yunting_service_message_f_d"
    assert plan.label == "yt_std_api_yunting_service_message_f_d_test_run_0002"
    assert plan.row_count == 7
    assert "message_pk" in plan.columns


def test_doris_stream_load_uses_post_request(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"Status": "Success", "NumberLoadedRows": 1, "NumberFilteredRows": 0}

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr("agent_runtime.yunting.doris.httpx.request", fake_request)
    adapter = DorisStreamLoadAdapter(hosts=["doris.example"], port=33060, database="agent_runtime")

    result = adapter.stream_load("std_api_yunting_service_message_f_d", [{"message_pk": "m1"}], run_id="test_run")

    assert result["Status"] == "Success"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://doris.example:33060/api/agent_runtime/std_api_yunting_service_message_f_d/_stream_load"
    assert calls[0]["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert calls[0]["headers"]["Expect"] == "100-continue"
    assert calls[0]["headers"]["strip_outer_array"] == "true"


def test_qdrant_dry_run_delete_and_upsert_are_explicit():
    layers, _ = build_yunting_layers(load_fixture_sessions(), run_id="test_run", raw_file_path=str(FIXTURE))
    points = text_points_from_ads(layers["ads_agent_yunting_faq_vector_api_d"], mock_dimension=8)
    adapter = QdrantAdapter(url="http://localhost:6333")

    delete_plan = adapter.dry_run_delete_by_unique_id("yunting_service_text_v1_dev", "sample-session-001")
    upsert_plan = adapter.dry_run_upsert("yunting_service_text_v1_dev", points)

    assert delete_plan["filter"]["must"][0]["key"] == "unique_id"
    assert delete_plan["filter"]["must"][0]["match"]["value"] == "sample-session-001"
    assert upsert_plan["point_count"] == len(points)
    assert upsert_plan["dry_run"] is True


def test_dagster_handoff_module_imports_without_hard_dependency():
    assert dagster_defs.YUNTING_WEEKLY_CRON == "0 3 * * 1"
    assert dagster_defs.YUNTING_EXECUTION_TIMEZONE == "Asia/Shanghai"


def test_cli_dry_run_layers_loads_api_page_logs(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw_root = tmp_path / "raw" / "sample_run"
    api_dir = raw_root / "api_pages"
    sessions_dir = raw_root / "sessions"
    api_dir.mkdir(parents=True)
    sessions_dir.mkdir()
    (api_dir / "page_0001.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    args = argparse.Namespace(
        input_file="",
        input_dir=str(sessions_dir),
        output_dir=str(tmp_path / "layers"),
        run_id="sample_run",
    )

    yunting_cli.cmd_dry_run_layers(args)

    page_rows = [
        json.loads(line)
        for line in (tmp_path / "layers" / "sample_run" / "ods_api_yunting_service_page_log_d.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    dashboard_rows = [
        json.loads(line)
        for line in (tmp_path / "layers" / "sample_run" / "ads_agent_yunting_pipeline_dashboard_d.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(page_rows) == 1
    assert page_rows[0]["response_code"] == 20000
    assert page_rows[0]["trace_id"] == "trace-structural-sample"
    assert dashboard_rows[0]["api_page_count"] == 1


def test_yunting_client_rejects_business_error(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 40100, "msg": "bad token", "traceId": "trace-error"}

    def fake_post(url, headers, json, timeout):
        assert url == "https://opendata.yuntingai.com/api/comment/v1/service/pull"
        return Response()

    monkeypatch.setattr("agent_runtime.yunting.api.httpx.post", fake_post)
    client = YuntingClient(base_url="https://opendata.yuntingai.com", access_token="test-token")

    try:
        client.pull_service_page(project_id="project")
    except RuntimeError as exc:
        assert "code=40100" in str(exc)
        assert "trace-error" in str(exc)
    else:
        raise AssertionError("expected business-code failure")


def test_yunting_token_fetch_uses_source_and_third_party(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 20000, "result": {"access_token": "token-from-source"}}

    def fake_get(url, params, timeout):
        assert url == "https://opendata.yuntingai.com/oauth2/token"
        assert params == {"source": "source-id", "third_party_id": "third-party"}
        return Response()

    monkeypatch.setattr("agent_runtime.yunting.api.httpx.get", fake_get)

    token = fetch_access_token(
        base_url="https://opendata.yuntingai.com",
        source="source-id",
        third_party_id="third-party",
    )

    assert token == "token-from-source"
