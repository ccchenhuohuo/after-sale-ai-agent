# 云听客服会话管道上线整改方案

## 范围

本文只覆盖多代理审计中除“陈述式故障 FAQ 漏召回”之外的 P1 上线阻塞与 P2 高优先级问题。FAQ 召回规则由独立分支处理，本文不再重复展开。

整改前状态：

- 云听真实数据已能小批量拉取并生成 Doris/Qdrant 分层 JSONL。
- 服务器 Qdrant 已通过 Docker 安装，dev collection 已写入样本点。
- Doris 分层表设计已覆盖 ODS/STD/DWD/DIM/DWS/ADS/DM，但 DDL 和真实表的分区、幂等、重跑策略需要重做。
- 服务器 Dagster 已存在部署，但当前生产 workspace 并没有接入本仓库的客服会话 FAQ 全链路。

## 上线判断

在以下条件全部满足前，云听客服会话管道只能作为 dev 验证链路，不应开启生产周调度，也不应接入正式 RAG 检索：

1. Qdrant 真实写入不再使用 mock vector。
2. Doris 表具备可补跑、可重跑、可审计的幂等策略。
3. Dagster 服务器 job 真正覆盖拉取、分层、Doris 写入、embedding、Qdrant upsert、校验。
4. API 分页、Stream Load、Qdrant upsert 均具备异常恢复和失败可见性。
5. 真实数据不进入 Git，环境指纹和密钥不出现在提交内容中。

## P1 整改

### 1. Qdrant mock vector 与真实 upsert 隔离

问题：

- 整改前 `upsert-qdrant` 会把 `mock_vector()` 生成的确定性本地向量写入真实 Qdrant collection。
- ADS 行标记的模型是 `text-embedding-v4` / `qwen3-vl-embedding`，但实际向量并非这些模型生成，容易污染检索库。

已实施方案：

- `upsert-qdrant` 已改为生产命令，只接受真实 embedding 结果。
- 新增 `mock-upsert-qdrant-dev`，只允许写入 collection 名以 `_dev` 结尾的集合。
- mock upsert 的 payload 必须写入：
  - `embedding_backend='mock'`
  - `embedding_model='mock-deterministic-vector'`
  - `is_semantic_vector=false`
- 生产 upsert 必须校验：
  - ADS `vector_model` 与运行配置一致。
  - ADS `vector_dimension` 与生成向量长度一致。
  - collection 名不以 `_dev` 结尾时禁止 mock backend。
  - Qdrant collection 的 `size`、`distance`、vector schema 全部匹配。

验收标准：

- 未配置真实 embedding provider 时，生产 `upsert-qdrant` 直接失败。
- mock 命令写非 `_dev` collection 直接失败。
- 测试覆盖 mock 禁止生产写入、dimension 不一致、distance 不一致、payload backend 标记。

### 2. 媒体 collection 不再伪装成语义检索

问题：

- 当前 media vector seed 只来自 `message_type/content_id/asset_id/media_object_key`。
- 媒体 observation 仍是“尚未下载解析”的占位摘要，不能支持图片/视频语义检索。

已实施方案：

- 第一阶段：媒体只进入 Doris DWD/DWS/ADS metadata，不写入生产 media vector collection。
- 第二阶段：图片下载成功后生成 OCR 和 visual summary，把 OCR/summary 作为文本 chunk 写入 text collection。
- 第三阶段：接入真实多模态 embedding 后，再启用 production `yunting_service_media_v1`。
- ADS media 表保留 `sync_status='pending_media_processing'` 或 `skipped_no_semantic_vector`，不要标记为已向量化成功。

验收标准：

- 无 OCR/visual summary/多模态 embedding 时，生产 media upsert 不执行。
- 媒体处理失败不阻断文本 FAQ 入库。
- media point payload 能通过 `media_chunk_id/asset_id/unique_id/content_id` 回查 Doris。

### 3. Doris 分区与幂等重构

问题：

- DDL 缺少 `PARTITION BY`，不利于日分区回灌、TTL、失败批次隔离。
- 多张 DIM/ADS/DM 表使用 `DUPLICATE KEY`，相同输入重跑会重复累积。
- DWS/ADS chunk id 会随清洗逻辑变化产生新行，旧 chunk 不会清理。

已实施方案：

- 所有 `_d` 表增加分区：
  - ODS/STD/DWD 使用 `dt`。
  - DWS/ADS/DM 使用 `stat_date`。
  - 配置 Dynamic Partition 或 Auto Partition。
- 按业务语义调整 key：
  - ODS page log：`UNIQUE KEY(run_id, page_no)`。
  - DIM topic/tag：`UNIQUE KEY(unique_id, topic_name, topic_value_hash)`、`UNIQUE KEY(unique_id, tag_name)`。
  - DIM enum：`UNIQUE KEY(enum_type, enum_code)`。
  - ADS dashboard：`UNIQUE KEY(stat_date, run_id)`。
  - DM 聚合：按维度集合定义 `UNIQUE KEY`，避免重复统计。
- 对 DWS/ADS FAQ chunk 引入版本治理：
  - 每次重算同一 `unique_id` 前，先删除该 `unique_id` 的旧 DWS/ADS 派生行，或引入 `run_id`/`version` 只保留 latest。
  - Qdrant 清理必须与 Doris 派生行版本保持一致。
- 缺失 `DATETIME` 输出 `null`，不输出空字符串。

验收标准：

- 同一 run 重跑后 Doris 各表行数不增加。
- 同一业务窗口不同 run 补跑后，只保留最新有效派生行或能按 version 明确区分。
- 失败批次可按分区或 run_id 清理。
- Stream Load filtered rows 为 0。

### 4. 生产 Dagster 接入

问题：

- 本仓库的 `dagster_defs.py` 只是可选 handoff，不是生产调度定义。
- 服务器 Dagster workspace 指向 `/opt/ulanzi/kol-dashboard`。
- 当前服务器云听 schedule 是 stopped，且不是本客服 FAQ 全链路。

已实施方案：

- 仓库已提供 `stream-load-doris`、`upsert-qdrant`、`verify-counts` 三个服务器 Dagster 可直接调用的 CLI 入口；服务器 Dagster 项目仍需新增客服会话 job，名称建议 `yunting_service_weekly_ingest_job`。
- asset/op 顺序：
  1. `pull_yunting_service_api_pages`
  2. `build_yunting_layers`
  3. `stream_load_yunting_doris_layers`
  4. `build_or_load_embeddings`
  5. `upsert_yunting_qdrant`
  6. `verify_yunting_pipeline_counts`
  7. `emit_yunting_pipeline_manifest`
- schedule：
  - cron：`0 3 * * 1`
  - timezone：`Asia/Shanghai`
  - 处理上一自然周。
- 开启前必须手动 launch 一次，确认：
  - raw 文件存在。
  - layers JSONL 行数与 manifest 一致。
  - Doris 每层 row count 与 manifest 对齐。
  - Qdrant point count 与 ADS successful rows 对齐。
  - 重跑同一窗口不重复。

验收标准：

- Dagster UI 能看到客服 job、asset materialization、manifest 路径。
- schedule 处于 RUNNING。
- 最近一次手动 run 成功，且日志包含 Doris/Qdrant 校验摘要。
- daemon heartbeat warning 已确认不影响该 job，或完成治理。

## P2 整改

### 1. API 分页循环保护

整改方案：

- `pull_service_pages()` 和 `pull_service_sessions()` 增加：
  - `seen_page_tokens`
  - 默认最大页数硬上限
  - 连续空页上限
  - 重复 token 报错并写入 manifest
- `max_pages=0` 不再代表无限无保护，而是“使用配置默认上限”。

验收标准：

- API 返回重复 pageToken 时不会死循环。
- API 返回空页但 `hasMore=true` 时会在阈值后失败。
- manifest 记录失败 token、页号、traceId。

### 2. 消息主键稳定

整改方案：

- `message_pk` 优先使用 `unique_id + content_id`。
- `content_id` 缺失时使用 `unique_id + publish_time + role + message_type + content_hash`。
- `message_index` 只作为排序字段，不参与主键。

验收标准：

- 晚到消息插入到前面时，已有消息 `message_pk` 不变。
- 同一 contentId 的重复消息可覆盖或被识别为同一事实。

### 3. raw 落盘异常记录不覆盖

整改方案：

- 缺失 `unique` 的 session 文件名改为 `session_{index}_{raw_hash}.json`。
- manifest 记录 `missing_unique_count`。
- pipeline 可以继续跳过无 unique 记录，但 raw 不能丢。

验收标准：

- 多条缺失 unique 的 raw session 会生成多个文件。
- manifest 可定位这些异常样本。

### 4. Stream Load 幂等和顺序

整改方案：

- `Label Already Exists + FINISHED` 视作成功。
- `Publish Timeout` 后查询 label 状态，再决定是否重试。
- 真实导入按 `LAYER_ORDER`，不要按文件名字母序。
- 每张表 Stream Load 后记录：
  - label
  - table
  - loaded rows
  - filtered rows
  - error URL

验收标准：

- 同 label 重试不会误报失败。
- 任何 filtered rows > 0 都让 job 失败并输出 error URL。
- 导入顺序固定为 `ODS -> STD -> DWD -> DIM -> DWS -> ADS -> DM`。

### 5. 安全与仓库卫生

整改方案：

- `.env.example` 中的私网/Tailscale IP 改为占位值。
- 文档示例避免把真实凭据直接放在 shell 命令行，改为本地 `.env`。
- 增加 secret scan，覆盖：
  - `.env*`
  - `data/**/*.json*`
  - `*.jsonl`
  - `docs/**/*.md`
- 文档补充：覆盖 `--data-root` 或 `--output-dir` 时，必须确认目标路径被 `.gitignore` 覆盖。

验收标准：

- Git diff 中无真实 token、真实媒体 URL、客户正文、私网拓扑指纹。
- CI 或 pre-commit 能阻止常见密钥和 raw 数据误提交。

## 推荐拆分 PR

### PR 1：Qdrant 安全闸门

- mock/real upsert 命令隔离。
- 真实 upsert 要求真实 embedding provider。
- collection schema 校验。
- Qdrant payload 增加 `embedding_backend`。
- 禁止 media production upsert 伪语义向量。

### PR 2：Doris DDL 与幂等

- 更新 DDL 分区和 key 模型。
- 更新 TableSpec/文档。
- `DATETIME` null 化。
- Stream Load label 幂等处理。
- 导入顺序使用 `LAYER_ORDER`。

### PR 3：API 与事实主键稳定

- pageToken 循环保护。
- message_pk 稳定化。
- raw missing unique 文件名修复。
- manifest 增加异常计数。

### PR 4：服务器 Dagster 接入

- 在生产 Dagster 项目接入客服管道。
- 手动 run 验证。
- 开启 weekly schedule。
- 增加 Doris/Qdrant count verifier。

### PR 5：安全卫生

- `.env.example` 环境指纹清理。
- 文档凭据示例改写。
- secret scan / pre-commit。

## 最终验收清单

- 本地单测通过。
- 真实最新 10 条数据 dry-run 通过。
- 一个小时间窗真实数据写入 Doris dev/schema 后，manifest 与 Doris row count 一致。
- Qdrant dev collection 使用 mock 时明确标记 mock，生产 collection 只允许真实 embedding。
- 同一窗口重跑两次，Doris 和 Qdrant 不重复。
- Dagster 手动 run 成功。
- Dagster schedule 启用后下一次 tick 成功。
- Git 中无真实数据、密钥、媒体 URL、环境指纹。
