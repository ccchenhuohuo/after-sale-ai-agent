# 云听客服会话 Doris DDL 草案

数据库默认使用 `agent_runtime`。本文件只提交 DDL 草案，不在本地自动执行。

> 说明：JSON 字段先按 `STRING` 保存，确保 Doris 版本兼容。生产执行前可按服务器 Doris 版本把部分字段升级为 JSON 类型。

## ODS

```sql
CREATE TABLE IF NOT EXISTS agent_runtime.ods_api_yunting_service_page_log_d (
  run_id VARCHAR(255),
  page_no INT,
  dt VARCHAR(255),
  request_body_json STRING,
  response_code INT,
  response_msg STRING,
  trace_id STRING,
  has_more BOOLEAN,
  page_token_hash STRING,
  raw_file_path STRING,
  source_system STRING,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(run_id, page_no, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(run_id) BUCKETS 8
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.ods_api_yunting_service_session_raw_f_d (
  unique_id VARCHAR(255),
  dt VARCHAR(255),
  run_id VARCHAR(255),
  insert_timestamp DATETIME,
  source_name STRING,
  project_name STRING,
  category_id STRING,
  raw_json STRING,
  raw_json_hash STRING,
  raw_file_path STRING,
  source_system STRING,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(unique_id, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(unique_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");
```

## STD

```sql
CREATE TABLE IF NOT EXISTS agent_runtime.std_api_yunting_service_session_f_d (
  unique_id VARCHAR(255),
  dt VARCHAR(255),
  oid STRING,
  project_name STRING,
  source_name STRING,
  shop_name STRING,
  session_type STRING,
  session_start_time DATETIME,
  session_close_time DATETIME,
  user_id STRING,
  user_name STRING,
  server_name_list_json STRING,
  order_no_list_json STRING,
  escore DOUBLE,
  score DOUBLE,
  is_default STRING,
  category_id STRING,
  contents_json STRING,
  topic_configs_json STRING,
  tag_list_json STRING,
  raw_file_path STRING,
  source_system STRING,
  biz_create_time DATETIME,
  biz_update_time DATETIME,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(unique_id, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(unique_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.std_api_yunting_service_message_f_d (
  message_pk VARCHAR(255),
  dt VARCHAR(255),
  content_id STRING,
  unique_id VARCHAR(255),
  message_index INT,
  publish_time DATETIME,
  `role` STRING,
  message_type STRING,
  user_name STRING,
  content_text STRING,
  product_title_list_json STRING,
  raw_json STRING,
  source_system STRING,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(message_pk, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(message_pk) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.std_api_yunting_service_media_asset_f_d (
  asset_id VARCHAR(255),
  dt VARCHAR(255),
  unique_id VARCHAR(255),
  content_id STRING,
  message_type STRING,
  `role` STRING,
  source_url STRING,
  content_type STRING,
  file_sha256 STRING,
  file_size BIGINT,
  download_status STRING,
  media_object_key STRING,
  error_message STRING,
  source_system STRING,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(asset_id, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(asset_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");
```

## DWD

```sql
CREATE TABLE IF NOT EXISTS agent_runtime.dwd_api_yunting_service_session_f_d (
  unique_id VARCHAR(255),
  dt VARCHAR(255),
  oid STRING,
  project_name STRING,
  source_name STRING,
  shop_name STRING,
  session_type STRING,
  session_start_time DATETIME,
  session_close_time DATETIME,
  user_id STRING,
  user_name STRING,
  server_name_list_json STRING,
  order_no_list_json STRING,
  escore DOUBLE,
  score DOUBLE,
  is_default STRING,
  category_id STRING,
  contents_json STRING,
  topic_configs_json STRING,
  tag_list_json STRING,
  raw_file_path STRING,
  source_system STRING,
  biz_create_time DATETIME,
  biz_update_time DATETIME,
  create_time DATETIME,
  update_time DATETIME,
  source_type STRING
) UNIQUE KEY(unique_id, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(unique_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.dwd_api_yunting_service_message_f_d (
  message_pk VARCHAR(255),
  dt VARCHAR(255),
  content_id STRING,
  unique_id VARCHAR(255),
  message_index INT,
  publish_time DATETIME,
  `role` STRING,
  message_type STRING,
  user_name STRING,
  content_text STRING,
  product_title_list_json STRING,
  raw_json STRING,
  source_system STRING,
  create_time DATETIME,
  update_time DATETIME,
  source_type STRING
) UNIQUE KEY(message_pk, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(message_pk) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.dwd_api_yunting_service_media_asset_f_d (
  asset_id VARCHAR(255),
  dt VARCHAR(255),
  unique_id VARCHAR(255),
  content_id STRING,
  message_type STRING,
  `role` STRING,
  source_url STRING,
  content_type STRING,
  file_sha256 STRING,
  file_size BIGINT,
  download_status STRING,
  media_object_key STRING,
  error_message STRING,
  source_system STRING,
  create_time DATETIME,
  update_time DATETIME,
  source_type STRING
) UNIQUE KEY(asset_id, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(asset_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");
```

## DIM

```sql
CREATE TABLE IF NOT EXISTS agent_runtime.dim_yunting_topic_value (
  unique_id VARCHAR(255),
  topic_name VARCHAR(255),
  topic_value_hash VARCHAR(255),
  dt VARCHAR(255),
  topic_value STRING,
  source_system STRING,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(unique_id, topic_name, topic_value_hash, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(unique_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.dim_yunting_tag (
  unique_id VARCHAR(255),
  tag_name VARCHAR(255),
  dt VARCHAR(255),
  tag_escore DOUBLE,
  topic_configs_json STRING,
  source_system STRING,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(unique_id, tag_name, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(unique_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.dim_yunting_service_enum (
  enum_type VARCHAR(255),
  enum_code VARCHAR(255),
  dt VARCHAR(255),
  enum_name STRING,
  source_system STRING,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(enum_type, enum_code, dt)
AUTO PARTITION BY LIST (dt) ()
DISTRIBUTED BY HASH(enum_type) BUCKETS 4
PROPERTIES ("replication_num" = "1");
```

## DWS

```sql
CREATE TABLE IF NOT EXISTS agent_runtime.dws_yunting_service_faq_case_d (
  case_id VARCHAR(255),
  stat_date VARCHAR(255),
  unique_id VARCHAR(255),
  case_title STRING,
  customer_question_summary STRING,
  answer_summary STRING,
  symptom_summary STRING,
  resolution_summary STRING,
  product_summary STRING,
  brand_json STRING,
  sku_json STRING,
  spu_json STRING,
  product_name_json STRING,
  tags_json STRING,
  topic_values_json STRING,
  evidence_level STRING,
  source_type VARCHAR(255),
  reference_class STRING,
  authority_level STRING,
  authority_score DOUBLE,
  can_be_reference BOOLEAN,
  quality_status STRING,
  source_name STRING,
  shop_name STRING,
  session_type STRING,
  stat_week VARCHAR(255),
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(case_id, stat_date)
AUTO PARTITION BY LIST (stat_date) ()
DISTRIBUTED BY HASH(case_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.dws_yunting_service_faq_chunk_d (
  chunk_id VARCHAR(255),
  stat_date VARCHAR(255),
  case_id VARCHAR(255),
  unique_id VARCHAR(255),
  chunk_type STRING,
  chunk_text STRING,
  question STRING,
  answer STRING,
  source_content_ids_json STRING,
  linked_asset_ids_json STRING,
  embedding_text_hash STRING,
  quality_status STRING,
  source_type VARCHAR(255),
  reference_class STRING,
  authority_level STRING,
  authority_score DOUBLE,
  can_be_reference BOOLEAN,
  stat_week VARCHAR(255),
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(chunk_id, stat_date)
AUTO PARTITION BY LIST (stat_date) ()
DISTRIBUTED BY HASH(chunk_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.dws_yunting_service_media_observation_d (
  media_chunk_id VARCHAR(255),
  stat_date VARCHAR(255),
  asset_id VARCHAR(255),
  unique_id VARCHAR(255),
  content_id STRING,
  message_type STRING,
  ocr_text STRING,
  visual_summary STRING,
  video_summary STRING,
  keyframe_refs_json STRING,
  evidence_level STRING,
  source_type VARCHAR(255),
  reference_class STRING,
  authority_level STRING,
  authority_score DOUBLE,
  can_be_reference BOOLEAN,
  stat_week VARCHAR(255),
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(media_chunk_id, stat_date)
AUTO PARTITION BY LIST (stat_date) ()
DISTRIBUTED BY HASH(media_chunk_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");
```

## ADS

```sql
CREATE TABLE IF NOT EXISTS agent_runtime.ads_agent_yunting_faq_vector_api_d (
  point_id VARCHAR(255),
  stat_date VARCHAR(255),
  collection_name STRING,
  chunk_id VARCHAR(255),
  case_id VARCHAR(255),
  unique_id VARCHAR(255),
  vector_model STRING,
  vector_dimension INT,
  payload_json STRING,
  payload_hash STRING,
  embedding_text STRING,
  embedding_text_hash STRING,
  sync_status STRING,
  last_synced_at DATETIME NULL,
  error_message STRING,
  stat_week VARCHAR(255),
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(point_id, stat_date)
AUTO PARTITION BY LIST (stat_date) ()
DISTRIBUTED BY HASH(point_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.ads_agent_yunting_media_vector_api_d (
  point_id VARCHAR(255),
  stat_date VARCHAR(255),
  collection_name STRING,
  media_chunk_id VARCHAR(255),
  asset_id VARCHAR(255),
  unique_id VARCHAR(255),
  vector_model STRING,
  vector_dimension INT,
  payload_json STRING,
  media_object_key STRING,
  sync_status STRING,
  last_synced_at DATETIME NULL,
  error_message STRING,
  stat_week VARCHAR(255),
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(point_id, stat_date)
AUTO PARTITION BY LIST (stat_date) ()
DISTRIBUTED BY HASH(point_id) BUCKETS 16
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.ads_agent_yunting_pipeline_dashboard_d (
  stat_date VARCHAR(255),
  run_id VARCHAR(255),
  stat_week VARCHAR(255),
  api_page_count BIGINT,
  raw_session_count BIGINT,
  std_session_count BIGINT,
  valid_case_count BIGINT,
  faq_chunk_count BIGINT,
  media_asset_count BIGINT,
  download_success_count BIGINT,
  embedding_success_count BIGINT,
  qdrant_upsert_success_count BIGINT,
  failed_count BIGINT,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(stat_date, run_id)
AUTO PARTITION BY LIST (stat_date) ()
DISTRIBUTED BY HASH(stat_date) BUCKETS 8
PROPERTIES ("replication_num" = "1");
```

## DM

```sql
CREATE TABLE IF NOT EXISTS agent_runtime.dm_yunting_service_quality_d (
  stat_date VARCHAR(255),
  stat_week VARCHAR(255),
  source_type VARCHAR(255),
  session_count BIGINT,
  valid_case_count BIGINT,
  message_count BIGINT,
  customer_message_count BIGINT,
  server_message_count BIGINT,
  reference_class STRING,
  authority_level STRING,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(stat_date, stat_week, source_type)
AUTO PARTITION BY LIST (stat_date) ()
DISTRIBUTED BY HASH(stat_date) BUCKETS 8
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.dm_yunting_service_product_tag_d (
  stat_date VARCHAR(255),
  stat_week VARCHAR(255),
  source_type VARCHAR(255),
  topic_value_count BIGINT,
  tag_count BIGINT,
  faq_count BIGINT,
  media_evidence_count BIGINT,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(stat_date, stat_week, source_type)
AUTO PARTITION BY LIST (stat_date) ()
DISTRIBUTED BY HASH(stat_date) BUCKETS 8
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS agent_runtime.dm_yunting_service_media_d (
  stat_date VARCHAR(255),
  stat_week VARCHAR(255),
  source_type VARCHAR(255),
  image_count BIGINT,
  video_count BIGINT,
  download_success_count BIGINT,
  ocr_success_count BIGINT,
  visual_summary_success_count BIGINT,
  media_upsert_success_count BIGINT,
  create_time DATETIME,
  update_time DATETIME
) UNIQUE KEY(stat_date, stat_week, source_type)
AUTO PARTITION BY LIST (stat_date) ()
DISTRIBUTED BY HASH(stat_date) BUCKETS 8
PROPERTIES ("replication_num" = "1");
```
