# 云听客服会话清洗管道

## 定位

云听客服会话进入 RAG 的定位是“历史经验参考 FAQ”，与飞书售后群真实 FAQ 同类，但权威程度更低。正式知识库、飞书售后群 FAQ、云听历史 FAQ 不靠是否可引用区分，而靠 `source_type`、`reference_class`、`authority_level`、`authority_score` 在检索和回答阶段排序。

云听默认字段：

- `source_type='yunting_service_history_faq'`
- `reference_class='support_history_faq'`
- `authority_level='low'`
- `authority_score=0.45`
- `can_be_reference=true`

## 数据边界

真实云听数据允许在本机和服务器做联调、dry-run、人工验收，但不进入 Git。仓库只提交结构样本 fixture，真实产物写入 `data/yunting/`，该目录已加入 `.gitignore`。

本地与服务器目录保持一致：

- 本地：`data/yunting/service/`
- 服务器：`/opt/agent-runtime/data/yunting/service/`
- raw：`raw/<run_id>/api_pages/page_0001.json`、`raw/<run_id>/sessions/{unique}.json`
- layers：`layers/<run_id>/<table>.jsonl`
- rag：`rag/<run_id>/faq_cases.jsonl`、`faq_chunks.jsonl`、`media_chunks.jsonl`
- manifest：`layers/<run_id>/manifest.json`
- media：`media/sha256/...`，默认不下载全量，只记录 URL 和待处理状态

## 本地命令

拉取最新 10 条真实云听客服会话：

```bash
YUNTING_ACCESS_TOKEN=... YUNTING_PROJECT_ID=... \
python scripts/yunting_service_pipeline.py pull-latest-10
```

从 raw JSON 生成 Doris/Qdrant 分层 JSONL：

```bash
python scripts/yunting_service_pipeline.py dry-run-layers \
  --input-dir data/yunting/service/raw/<run_id>/sessions \
  --run-id <run_id>
```

生成 Doris Stream Load 计划，不写 Doris：

```bash
python scripts/yunting_service_pipeline.py dry-run-doris \
  --layers-dir data/yunting/service/layers/<run_id>
```

生成 Qdrant upsert 计划，不写 Qdrant：

```bash
python scripts/yunting_service_pipeline.py dry-run-qdrant \
  --layers-dir data/yunting/service/layers/<run_id>
```

## Doris 分层

数据按部门 ETL 规范单向流转：`ODS -> STD -> DWD -> DIM/DWS -> ADS -> DM`。Doris 是事实源和同步追踪层，Qdrant 命中后必须通过 `chunk_id` 或 `media_chunk_id` 回查 Doris。

- ODS：保存 API 页响应和 `result.data[]` 原始 session。
- STD：统一字段命名、时间、枚举、数组 JSON 字符串；不做脱敏。
- DWD：会话、消息、媒体资产三类原子事实。
- DIM：`topicConfigs`、`tagList`、枚举映射。
- DWS：FAQ case、FAQ chunk、媒体观察证据。
- ADS：Qdrant 待入库/已入库服务表、pipeline dashboard。
- DM：质量、产品标签、多媒体主题集市。

公共字段：

- 所有层保留 `create_time`、`update_time`。
- ODS/STD/DWD 保留 `source_system='yunting'`。
- DWS/ADS/DM 保留 `stat_date`、`stat_week`。
- JSON 数组/对象字段以字符串保存，便于 Doris 版本兼容。

## 多模态处理

云听 `contents[]` 中 `messageType` 为 `IMAGE` 或 `VIDEO` 时，当前本地管道会生成媒体资产事实和媒体观察行，保留：

- `unique_id`
- `content_id`
- `role`
- `message_type`
- `source_url`
- `download_status='not_downloaded'`
- `media_object_key` 预留路径

媒体下载、OCR、视频抽帧、视觉摘要属于服务器后续增强步骤。媒体处理失败不阻断文本 FAQ 入库。

## Qdrant 设计

服务器尚未安装 Qdrant，因此仓库先交付 mock/dry-run adapter 和 collection 设计。

文本 collection：

- dev：`yunting_service_text_v1_dev`
- prod：`yunting_service_text_v1`
- 来源：`ads_agent_yunting_faq_vector_api_d`
- 模型：`text-embedding-v4`
- point：`answer_unit`、`case_overview`、`conversation_window`、`conversation_timeline`

媒体 collection：

- dev：`yunting_service_media_v1_dev`
- prod：`yunting_service_media_v1`
- 来源：`ads_agent_yunting_media_vector_api_d`
- 模型：`qwen3-vl-embedding`
- point：`image_asset`、`video_asset`、`video_keyframe`、`media_summary`

重跑策略：

- 同一 `unique_id` 新版本入库前，先按 payload filter 删除旧 point。
- 新 point_id 由 collection 与 chunk/media id 稳定 hash 生成。
- ADS 表记录 `sync_status`、`last_synced_at`、`error_message`。

## Dagster 交接

服务器 Dagster 已单独维护，本仓库不再强行新增调度主体。交付内容是纯 Python 管道入口和可选 `dagster_defs` handoff 常量：

- Job 名建议：`yunting_service_weekly_ingest_job`
- Cron：`0 3 * * 1`
- Timezone：`Asia/Shanghai`
- 服务器 Dagster asset/op 可直接调用 `scripts/yunting_service_pipeline.py` 或 `agent_runtime.yunting.pipeline`。

## 验收

验收分四条线并行：

- ETL/DDL：表名、公共字段、分层依赖方向、DDL 文档。
- 数据质量：真实 10 条会话复原、TEXT/IMAGE/VIDEO 识别、欢迎语过滤。
- RAG/Qdrant：chunk 粒度、payload 权威字段、幂等 point、mock upsert。
- Repo/CI：无真实数据和密钥进入 Git，测试通过。
