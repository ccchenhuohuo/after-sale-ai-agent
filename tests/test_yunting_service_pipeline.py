import json
import argparse
from pathlib import Path

from agent_runtime.yunting import dagster_defs
from agent_runtime.yunting import cli as yunting_cli
from agent_runtime.yunting.api import YuntingClient, fetch_access_token, write_raw_run
from agent_runtime.yunting.common import SOURCE_TYPE
from agent_runtime.yunting.doris import DorisStreamLoadAdapter
from agent_runtime.yunting.pipeline import build_yunting_layers, extract_sessions
from agent_runtime.yunting.qdrant import QdrantAdapter, media_points_from_ads, text_points_from_ads, text_points_from_vectors
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
    assert manifest.missing_unique_count == 0
    assert manifest.duplicate_page_token_count == 0

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


def test_pipeline_normalizes_json_strings_roles_and_filters_non_answers():
    session = {
        "unique": "quality-session-001",
        "isDefault": "否",
        "sessionStartTime": "2026-06-24 10:00:00.000",
        "topicConfigs": json.dumps([{"topicName": "SKU", "topicValue": ["SKU-JSON"]}], ensure_ascii=False),
        "tagList": json.dumps([{"tagName": "兜底过滤"}], ensure_ascii=False),
        "contents": [
            {
                "contentId": "answer-valid",
                "publishTime": "2026-06-24 10:03:00.000",
                "role": "客服",
                "messageType": "文本",
                "content": "可以先长按电源键复位，再重新插电充电观察指示灯状态。",
            },
            {
                "contentId": "answer-invalid",
                "publishTime": "2026-06-24 10:02:00.000",
                "role": "客服",
                "messageType": "TEXT",
                "content": "请提供订单号",
            },
            {
                "contentId": "question",
                "publishTime": "2026-06-24 10:01:00.000",
                "role": "客户",
                "messageType": "TEXT",
                "content": "这个设备不能开机怎么办？",
            },
        ],
    }

    layers, _ = build_yunting_layers([session], run_id="test_run", raw_file_path="fixture")

    messages = layers["std_api_yunting_service_message_f_d"]
    assert [row["content_id"] for row in messages] == ["question", "answer-invalid", "answer-valid"]
    assert {row["role"] for row in messages} == {"CUSTOMER", "SERVER"}
    assert {row["message_type"] for row in messages} == {"TEXT"}
    assert any(row["topic_value"] == "SKU-JSON" for row in layers["dim_yunting_topic_value"])
    assert any(row["tag_name"] == "兜底过滤" for row in layers["dim_yunting_tag"])

    answer_chunks = [row for row in layers["dws_yunting_service_faq_chunk_d"] if row["chunk_type"] == "answer_unit"]
    assert len(answer_chunks) == 1
    assert "请提供订单号" not in answer_chunks[0]["chunk_text"]
    payload = json.loads(layers["ads_agent_yunting_faq_vector_api_d"][0]["payload_json"])
    assert payload["source_content_ids"]


def test_statement_style_fault_customer_message_becomes_answer_unit():
    session = {
        "unique": "statement-fault-001",
        "isDefault": "否",
        "sessionStartTime": "2026-06-24 11:00:00.000",
        "contents": [
            {
                "contentId": "fault",
                "publishTime": "2026-06-24 11:01:00.000",
                "role": "CUSTOMER",
                "messageType": "TEXT",
                "content": "设备充不进电了",
            },
            {
                "contentId": "answer",
                "publishTime": "2026-06-24 11:02:00.000",
                "role": "SERVER",
                "messageType": "TEXT",
                "content": "可以先更换充电线和充电头测试，长按电源键复位后再观察指示灯是否亮起。",
            },
        ],
    }

    layers, manifest = build_yunting_layers([session], run_id="test_run", raw_file_path="fixture")

    assert manifest.faq_case_count == 1
    answer_chunks = [row for row in layers["dws_yunting_service_faq_chunk_d"] if row["chunk_type"] == "answer_unit"]
    assert len(answer_chunks) == 1
    assert answer_chunks[0]["question"] == "设备充不进电了"
    assert "更换充电线" in answer_chunks[0]["answer"]
    assert json.loads(answer_chunks[0]["source_content_ids_json"]) == ["fault", "answer"]


def test_faq_water_filter_ignores_acknowledgement_without_support_need():
    session = {
        "unique": "water-only-001",
        "isDefault": "否",
        "sessionStartTime": "2026-06-24 11:10:00.000",
        "contents": [
            {
                "contentId": "thanks",
                "publishTime": "2026-06-24 11:11:00.000",
                "role": "CUSTOMER",
                "messageType": "TEXT",
                "content": "好的，谢谢",
            },
            {
                "contentId": "service",
                "publishTime": "2026-06-24 11:12:00.000",
                "role": "SERVER",
                "messageType": "TEXT",
                "content": "不客气，有问题随时联系我们处理。",
            },
        ],
    }

    layers, manifest = build_yunting_layers([session], run_id="test_run", raw_file_path="fixture")

    assert manifest.faq_case_count == 0
    assert not layers["dws_yunting_service_faq_chunk_d"]
    assert not layers["ads_agent_yunting_faq_vector_api_d"]


def test_answer_unit_uses_recent_customer_support_context():
    session = {
        "unique": "multi-context-001",
        "isDefault": "否",
        "sessionStartTime": "2026-06-24 11:20:00.000",
        "contents": [
            {
                "contentId": "fault-1",
                "publishTime": "2026-06-24 11:21:00.000",
                "role": "CUSTOMER",
                "messageType": "TEXT",
                "content": "收到就是坏的",
            },
            {
                "contentId": "fault-2",
                "publishTime": "2026-06-24 11:21:30.000",
                "role": "CUSTOMER",
                "messageType": "TEXT",
                "content": "指示灯不亮",
            },
            {
                "contentId": "answer",
                "publishTime": "2026-06-24 11:22:00.000",
                "role": "SERVER",
                "messageType": "TEXT",
                "content": "请先确认电池绝缘片是否取出，再长按电源键三秒；仍不亮的话可以安排换货处理。",
            },
        ],
    }

    layers, _ = build_yunting_layers([session], run_id="test_run", raw_file_path="fixture")

    answer_chunks = [row for row in layers["dws_yunting_service_faq_chunk_d"] if row["chunk_type"] == "answer_unit"]
    assert len(answer_chunks) == 1
    assert answer_chunks[0]["question"] == "收到就是坏的 / 指示灯不亮"
    assert json.loads(answer_chunks[0]["source_content_ids_json"]) == ["fault-1", "fault-2", "answer"]


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
    assert points[0].payload["embedding_backend"] == "mock"
    assert points[0].payload["is_semantic_vector"] is False
    assert points[0].payload["data_version"] == "test_run"

    semantic_points = text_points_from_vectors(
        ads_rows[:1],
        [[0.1] * 768],
        collection="yunting_service_text_v1_dev",
        vector_model="text-embedding-v4",
        vector_dimension=768,
        backend="test-provider",
    )
    assert semantic_points[0].payload["embedding_backend"] == "test-provider"
    assert semantic_points[0].payload["is_semantic_vector"] is True


def test_media_ads_preserves_pending_media_object_key():
    layers, _ = build_yunting_layers(load_fixture_sessions(), run_id="test_run", raw_file_path=str(FIXTURE))
    ads_rows = layers["ads_agent_yunting_media_vector_api_d"]

    assert ads_rows
    assert {row["collection_name"] for row in ads_rows} == {"yunting_service_media_v1_dev"}
    assert all(row["media_object_key"].startswith("media/sha256/pending/") for row in ads_rows)
    assert {row["sync_status"] for row in ads_rows} == {"skipped_no_semantic_vector"}


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


def test_stream_load_doris_cli_uses_layer_order(tmp_path, monkeypatch):
    layers, manifest = build_yunting_layers(load_fixture_sessions(), run_id="ordered_run", raw_file_path=str(FIXTURE))
    layers_dir = tmp_path / "layers" / "ordered_run"
    from agent_runtime.yunting.pipeline import write_layers

    write_layers(layers_dir, layers, manifest)
    calls = []

    class FakeAdapter:
        def stream_load(self, table_name, rows, *, run_id, batch_no=1):
            calls.append((table_name, batch_no, len(rows)))
            return {"Status": "Success", "NumberLoadedRows": len(rows), "NumberFilteredRows": 0, "Label": f"label-{batch_no}"}

    monkeypatch.setattr("agent_runtime.yunting.cli._build_doris_adapter", lambda database="": FakeAdapter())
    args = argparse.Namespace(layers_dir=str(layers_dir), run_id="ordered_run", database="", skip_empty=False)

    yunting_cli.cmd_stream_load_doris(args)

    assert calls[0][0].startswith("ods_")
    assert any(call[0].startswith("std_") for call in calls)
    assert calls[-1][0].startswith("dm_")


def test_doris_table_specs_include_unique_keys_and_partitions():
    assert DORIS_TABLES["ods_api_yunting_service_page_log_d"].key_columns == ("run_id", "page_no", "dt")
    assert DORIS_TABLES["ods_api_yunting_service_page_log_d"].partition_column == "dt"
    assert DORIS_TABLES["dim_yunting_topic_value"].key_columns == ("unique_id", "topic_name", "topic_value_hash", "dt")
    assert DORIS_TABLES["ads_agent_yunting_pipeline_dashboard_d"].key_columns == ("stat_date", "run_id")
    assert DORIS_TABLES["dm_yunting_service_quality_d"].key_columns == ("stat_date", "stat_week", "source_type")
    assert DORIS_TABLES["dws_yunting_service_faq_chunk_d"].partition_column == "stat_date"


def test_doris_ddl_uses_unique_keys_and_partitions():
    ddl = (ROOT / "docs" / "yunting-service-doris-ddl.md").read_text(encoding="utf-8")

    assert "DUPLICATE KEY" not in ddl
    assert "AUTO PARTITION BY LIST (dt)" in ddl
    assert "AUTO PARTITION BY LIST (stat_date)" in ddl
    assert "UNIQUE KEY(unique_id, topic_name, topic_value_hash, dt)" in ddl


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


def test_doris_stream_load_rejects_filtered_rows(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"Status": "Success", "NumberLoadedRows": 0, "NumberFilteredRows": 1}

    monkeypatch.setattr("agent_runtime.yunting.doris.httpx.request", lambda *args, **kwargs: FakeResponse())
    adapter = DorisStreamLoadAdapter(hosts=["doris.example"], port=33060, database="agent_runtime")

    try:
        adapter.stream_load("std_api_yunting_service_message_f_d", [{"message_pk": "m1"}], run_id="test_run")
    except RuntimeError as exc:
        assert "filtered rows" in str(exc)
    else:
        raise AssertionError("expected filtered rows to fail")


def test_doris_stream_load_treats_finished_duplicate_label_as_success(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"Status": "Label Already Exists", "ExistingJobStatus": "FINISHED"}

    monkeypatch.setattr("agent_runtime.yunting.doris.httpx.request", lambda *args, **kwargs: FakeResponse())
    adapter = DorisStreamLoadAdapter(hosts=["doris.example"], port=8040, database="agent_runtime")

    result = adapter.stream_load("std_api_yunting_service_message_f_d", [{"message_pk": "m1"}], run_id="test_run")

    assert result["IdempotentSuccess"] is True


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


def test_media_points_from_ads_preserve_payload_and_object_key():
    layers, _ = build_yunting_layers(load_fixture_sessions(), run_id="test_run", raw_file_path=str(FIXTURE))
    points = media_points_from_ads(layers["ads_agent_yunting_media_vector_api_d"], mock_dimension=8)

    assert points
    assert len(points[0].vector) == 8
    assert points[0].payload["media_object_key"].startswith("media/sha256/pending/")
    assert points[0].payload["message_type"] in {"IMAGE", "VIDEO"}


def test_qdrant_adapter_real_methods_call_expected_endpoints(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {"result": {"status": "completed"}}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, headers, timeout):
        calls.append(("GET", url, None))
        return FakeResponse(status_code=404)

    def fake_put(url, **kwargs):
        calls.append(("PUT", url, kwargs.get("json")))
        return FakeResponse()

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs.get("json")))
        return FakeResponse()

    monkeypatch.setattr("agent_runtime.yunting.qdrant.httpx.get", fake_get)
    monkeypatch.setattr("agent_runtime.yunting.qdrant.httpx.put", fake_put)
    monkeypatch.setattr("agent_runtime.yunting.qdrant.httpx.post", fake_post)

    adapter = QdrantAdapter(url="http://localhost:6333", api_key="secret")
    adapter.ensure_collection("text_dev", vector_size=768)
    adapter.ensure_keyword_payload_index("text_dev", "unique_id")
    adapter.count_by_data_version("text_dev", "run-1")
    adapter.delete_by_unique_id("text_dev", "session-1")
    adapter.delete_stale_by_unique_id("text_dev", "session-1", "run-1")
    adapter.upsert("text_dev", [text_points_from_ads([{"point_id": "11111111-1111-1111-1111-111111111111", "payload_json": "{}", "embedding_text": "hello", "embedding_text_hash": "hash"}], mock_dimension=8)[0]])

    assert calls[0] == ("GET", "http://localhost:6333/collections/text_dev", None)
    assert calls[1][0] == "PUT"
    assert calls[1][2]["vectors"]["size"] == 768
    assert calls[2][0] == "PUT"
    assert calls[2][2]["field_name"] == "unique_id"
    assert calls[3][0] == "POST"
    assert calls[3][2]["filter"]["must"][0]["key"] == "data_version"
    assert calls[4][0] == "POST"
    assert calls[4][2]["filter"]["must"][0]["key"] == "unique_id"
    assert calls[5][0] == "POST"
    assert calls[5][2]["filter"]["must_not"][0]["key"] == "data_version"
    assert calls[6][0] == "PUT"
    assert calls[6][2]["points"][0]["id"] == "11111111-1111-1111-1111-111111111111"


def test_qdrant_collection_distance_mismatch_fails(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"result": {"config": {"params": {"vectors": {"size": 768, "distance": "Dot"}}}}}

    monkeypatch.setattr("agent_runtime.yunting.qdrant.httpx.get", lambda *args, **kwargs: FakeResponse())
    adapter = QdrantAdapter(url="http://localhost:6333")

    try:
        adapter.ensure_collection("text_dev", vector_size=768, distance="Cosine")
    except RuntimeError as exc:
        assert "distance Dot" in str(exc)
    else:
        raise AssertionError("expected distance mismatch to fail")


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

    verify_args = argparse.Namespace(
        layers_dir=str(tmp_path / "layers" / "sample_run"),
        check_qdrant=False,
        collection="",
        data_version="",
    )
    yunting_cli.cmd_verify_counts(verify_args)


def test_write_raw_run_preserves_multiple_missing_unique_sessions(tmp_path):
    sessions = [{"content": "a"}, {"content": "b"}]

    write_raw_run(tmp_path, "raw_test", sessions, pages=[])

    files = sorted((tmp_path / "raw" / "raw_test" / "sessions").glob("*.json"))
    assert len(files) == 2
    assert files[0].name != files[1].name


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


def test_yunting_client_pull_service_pages_follows_page_token(monkeypatch):
    calls = []
    client = YuntingClient(base_url="https://opendata.yuntingai.com", access_token="test-token")

    def fake_page(*, project_id, start_time="", end_time="", page_token=""):
        calls.append(page_token)
        if not page_token:
            return {
                "code": 20000,
                "result": {
                    "data": [{"unique": "s1"}],
                    "hasMore": True,
                    "pageToken": "next-token",
                },
            }
        return {
            "code": 20000,
            "result": {
                "data": [{"unique": "s2"}],
                "hasMore": False,
                "pageToken": "",
            },
        }

    monkeypatch.setattr(client, "pull_service_page", fake_page)

    sessions, pages = client.pull_service_pages(
        project_id="project",
        start_time="2025-06-06 00:00:00",
        end_time="2025-06-07 00:00:00",
    )

    assert calls == ["", "next-token"]
    assert [session["unique"] for session in sessions] == ["s1", "s2"]
    assert len(pages) == 2


def test_yunting_client_rejects_repeated_page_token(monkeypatch):
    client = YuntingClient(base_url="https://opendata.yuntingai.com", access_token="test-token")

    def fake_page(*, project_id, start_time="", end_time="", page_token=""):
        return {
            "code": 20000,
            "result": {
                "data": [{"unique": f"s-{page_token or 'first'}"}],
                "hasMore": True,
                "pageToken": "repeat-token",
            },
        }

    monkeypatch.setattr(client, "pull_service_page", fake_page)

    try:
        client.pull_service_pages(project_id="project", max_pages=5)
    except RuntimeError as exc:
        assert "repeated pageToken" in str(exc)
    else:
        raise AssertionError("expected repeated token guard to fail")


def test_yunting_client_rejects_empty_page_loop(monkeypatch):
    client = YuntingClient(base_url="https://opendata.yuntingai.com", access_token="test-token")
    counter = {"value": 0}

    def fake_page(*, project_id, start_time="", end_time="", page_token=""):
        counter["value"] += 1
        return {
            "code": 20000,
            "traceId": "trace-empty",
            "result": {
                "data": [],
                "hasMore": True,
                "pageToken": f"next-{counter['value']}",
            },
        }

    monkeypatch.setattr(client, "pull_service_page", fake_page)

    try:
        client.pull_service_pages(project_id="project", max_empty_pages=1)
    except RuntimeError as exc:
        assert "too many empty pages" in str(exc)
        assert "trace-empty" in str(exc)
    else:
        raise AssertionError("expected empty page guard to fail")


def test_message_pk_is_stable_when_earlier_message_arrives():
    base_session = {
        "unique": "stable-message-session",
        "isDefault": "否",
        "contents": [
            {
                "contentId": "same-content",
                "publishTime": "2026-06-24 11:02:00.000",
                "role": "SERVER",
                "messageType": "TEXT",
                "content": "可以先复位设备。",
            },
        ],
    }
    late_session = {
        **base_session,
        "contents": [
            {
                "contentId": "new-earlier",
                "publishTime": "2026-06-24 11:01:00.000",
                "role": "CUSTOMER",
                "messageType": "TEXT",
                "content": "设备不能开机怎么办？",
            },
            *base_session["contents"],
        ],
    }

    base_layers, _ = build_yunting_layers([base_session], run_id="test_run", raw_file_path="fixture")
    late_layers, _ = build_yunting_layers([late_session], run_id="test_run", raw_file_path="fixture")

    base_message = base_layers["std_api_yunting_service_message_f_d"][0]
    late_message = next(row for row in late_layers["std_api_yunting_service_message_f_d"] if row["content_id"] == "same-content")
    assert base_message["message_pk"] == late_message["message_pk"]
    assert base_message["message_index"] == 1
    assert late_message["message_index"] == 2


def test_manifest_reports_missing_unique_and_duplicate_page_tokens():
    payloads = [
        {"result": {"pageToken": "dup", "data": []}},
        {"result": {"pageToken": "dup", "data": []}},
    ]

    _, manifest = build_yunting_layers([{"contents": []}], run_id="test_run", raw_file_path="fixture", page_payloads=payloads)

    assert manifest.missing_unique_count == 1
    assert manifest.duplicate_page_token_count == 1


def test_cli_parser_exposes_pull_range():
    parser = yunting_cli.build_parser()

    args = parser.parse_args(
        [
            "pull-range",
            "--start-time",
            "2025-06-06 00:00:00",
            "--end-time",
            "2025-06-07 00:00:00",
            "--max-pages",
            "2",
        ]
    )

    assert args.func == yunting_cli.cmd_pull_range
    assert args.max_pages == 2


def test_cli_parser_exposes_mock_and_real_upsert_qdrant():
    parser = yunting_cli.build_parser()

    mock_args = parser.parse_args(
        [
            "mock-upsert-qdrant-dev",
            "--layers-dir",
            "data/yunting/service/layers/run",
            "--batch-size",
            "100",
        ]
    )
    assert mock_args.func == yunting_cli.cmd_mock_upsert_qdrant_dev
    assert mock_args.media_dimension == 1024

    real_args = parser.parse_args(
        [
            "upsert-qdrant",
            "--layers-dir",
            "data/yunting/service/layers/run",
            "--batch-size",
            "100",
        ]
    )

    assert real_args.func == yunting_cli.cmd_upsert_qdrant
    assert real_args.text_dimension == 768
    assert real_args.batch_size == 100


def test_mock_upsert_rejects_non_dev_collection(tmp_path):
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    (layers_dir / "ads_agent_yunting_faq_vector_api_d.jsonl").write_text("", encoding="utf-8")
    (layers_dir / "ads_agent_yunting_media_vector_api_d.jsonl").write_text("", encoding="utf-8")
    args = argparse.Namespace(
        layers_dir=str(layers_dir),
        collection="yunting_service_text_v1",
        media_collection="yunting_service_media_v1_dev",
        text_dimension=768,
        media_dimension=1024,
        batch_size=100,
    )

    try:
        yunting_cli.cmd_mock_upsert_qdrant_dev(args)
    except SystemExit as exc:
        assert "only allowed" in str(exc)
    else:
        raise AssertionError("expected non-dev collection to be rejected")


def test_real_upsert_requires_embedding_provider(tmp_path, monkeypatch):
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    (layers_dir / "ads_agent_yunting_faq_vector_api_d.jsonl").write_text("", encoding="utf-8")
    (layers_dir / "ads_agent_yunting_media_vector_api_d.jsonl").write_text("", encoding="utf-8")
    for name in ("YUNTING_TEXT_EMBEDDING_API_KEY", "BAILIAN_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "YUNTING_TEXT_EMBEDDING_BASE_URL", "BAILIAN_EMBEDDING_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    args = argparse.Namespace(
        layers_dir=str(layers_dir),
        collection="yunting_service_text_v1",
        media_collection="yunting_service_media_v1",
        run_id="test_run",
        text_model="text-embedding-v4",
        text_dimension=768,
        batch_size=100,
        skip_doris_writeback=True,
    )

    try:
        yunting_cli.cmd_upsert_qdrant(args)
    except RuntimeError as exc:
        assert "YUNTING_TEXT_EMBEDDING_API_KEY" in str(exc)
    else:
        raise AssertionError("expected missing embedding provider to fail")


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
