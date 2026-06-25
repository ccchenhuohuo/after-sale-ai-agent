# Tracing 仪表盘

终端 Agent 使用 OpenAI Agents SDK tracing。可在 OpenAI Platform Logs 仪表盘查看 trace：

https://platform.openai.com/logs?api=traces

也可以将 traces 额外发送到 ulanzicloud Tailscale 服务器 Phoenix。Phoenix 需要在服务器单独启动，本机继续运行 `VIJIMcopilot`。

Phoenix 地址：

```text
http://100.111.223.41:6006
```

## DeepSeek 模型与 Tracing

DeepSeek 可以通过 OpenAI 兼容接口作为 LLM provider 使用。生产默认不把 Agents SDK traces 导出到 OpenAI hosted traces；服务器优先使用 Phoenix / OpenTelemetry 作为运行链路视图。需要 OpenAI Platform traces 时，必须显式打开 hosted tracing。

配置：

```env
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
SUPPORT_AGENT_MODEL=deepseek-v4-flash
SUPPORT_AGENT_MODEL_FLASH=deepseek-v4-flash
SUPPORT_AGENT_MODEL_PRO=deepseek-v4-pro
SUPPORT_AGENT_BILLING_MODE=API Usage Billing
SUPPORT_AGENT_USE_CHAT_COMPLETIONS=true

OPENAI_TRACING_API_KEY=
OPENAI_PROJECT_ID=proj_xxx
SUPPORT_AGENT_OPENAI_HOSTED_TRACING_ENABLED=false
SUPPORT_AGENT_TRACING_DISABLED=false
SUPPORT_AGENT_TRACE_INCLUDE_SENSITIVE_DATA=true
SUPPORT_AGENT_TRACE_WORKFLOW_NAME=VIJIM-after-sale-copilot

PHOENIX_TRACING_ENABLED=true
PHOENIX_COLLECTOR_ENDPOINT=http://100.111.223.41:6006/v1/traces
PHOENIX_PROJECT_NAME=VIJIM-after-sale-copilot
```

运行行为：

- `LLM_API_KEY` 只用于模型调用。
- `SUPPORT_AGENT_OPENAI_HOSTED_TRACING_ENABLED=false` 是生产默认配置，避免服务器在 `api.openai.com` 不可达时反复丢弃 hosted trace batch。
- `OPENAI_TRACING_API_KEY` 只在 `SUPPORT_AGENT_OPENAI_HOSTED_TRACING_ENABLED=true` 时用于向 OpenAI Platform 导出 traces。
- 当前开发部署使用 `SUPPORT_AGENT_TRACE_INCLUDE_SENSITIVE_DATA=true`，trace 会记录用户原始输入、附件摘要、Agent prompt、模型 messages、内部结构化答案和最终飞书可见回复，便于在 Phoenix 完整复盘上下文传递。切到生产或共享环境前应改为 `false`；raw vector 和密钥仍不写入 trace。
- `PHOENIX_TRACING_ENABLED=true` 时，运行时会通过 OpenTelemetry/OpenInference 同步发送 traces 到 Tailscale 服务器 Phoenix；仅应在确认数据边界和访问控制后开启。
- 终端运行使用 `group_id=terminal-chat`。
- `/model` 会在 Flash 和 Pro 预设之间切换，影响后续 Agent 轮次。
- 一次用户消息只对应一个主 Runtime Trace；入口层在完整 turn 结束后调用一次 `flush_traces()`。
- 终端 `/compact` 会产生独立的上下文压缩 trace；它不是售后用户消息 turn，不参与“一条用户消息一个 Runtime Trace”的视觉验收。

## v2 Loop 业务 Span

当前运行时会先 intake，再收集结构化证据，最后运行结构化输出 Agent。Phoenix / OpenAI traces 中每个用户 turn 应只有一个主 Runtime Trace，内部应能看到以下业务 span：

- `support_runtime_turn`：覆盖完整用户 turn 的 OpenInference 语义 span，包含 `entrypoint`、`loop_version=v2`、request/session/chat/thread/message hash、hash 化 `session.id`、输入长度、asset 数量；完整 I/O 模式下只在这个 span 上写 `input.value`、`user.input` 和最终 `output.value`。
- `support_core_runtime`：Core Support Runtime 内部业务编排，包含 intake、retrieval、answer contract 等子流程。
- `intake_pipeline` / `intake_pipeline_result`：输入路由、ingestion 和统一上下文组装。
- `ocr_provider_call` / `visual_embedding_provider_call` / `visual_understanding_provider_call`：分别记录 OCR、图片向量化和千问 VL 结构化视觉理解调用。
- `ingestion_ocr` / `ingestion_image_embedding` / `ingestion_visual_understanding` / `ingestion_video_sampling`：分别记录各 ingestion 工具的状态、provider、模型、耗时和 artifact 摘要 hash。
- `retrieval_pipeline` / `retrieval_pipeline_result`：SKU、正式 KB、历史 FAQ、媒体证据的并发检索。
- `sku_resolve`：SKU 目录解析。
- `official_kb_search`：正式 KB/MRD/手册/政策检索。
- `history_search`：历史话题 RAG 检索。
- `media_search`：媒体观察证据检索。
- `evidence_pack`：证据包汇总，记录 SKU/正式依据/历史/媒体命中数和证据等级状态。
- `agent_answer` / `runner_run` / `agent_answer_result`：结构化答案生成和模型运行状态。完整 I/O 模式下 `runner.input.value` 是实际送入模型的完整 Agent prompt。
- `answer_contract_check`：最终中文答案 contract 校验。
- `answer_contract_result`：答案 contract 结果。完整 I/O 模式下 `internal_answer.value` 是内部结构化答案渲染文本。
- `visible_reply_render`：飞书可见回复渲染和校验。完整 I/O 模式下 `visible_reply.value` 是最终发到飞书 thread 的可见回复副本；Sessions 页不依赖这个子 span。
- `channel_reply` / `channel_reply_result`：通道回复构建和结果记录。Feishu legacy 记录真实发送结果；OpenClaw channel 记录 `payload_built`，表示已构建 thread reply payload，实际发送由 sidecar 负责。

trace metadata 会包含 `entrypoint`、`loop_version`、`source`、`model_label`、`request_id_hash`、`session_id_hash`、`chat_id_hash`、`thread_id_hash`、`message_id_hash` 和 hash 化 `session.id`。开发透明模式下，`input.value` / `output.value` 会写在 `support_runtime_turn` span 上；`runner_run` 会写 `runner.system_instructions.value` 和 `runner.input.value`；实际送入 chat completions 的完整 message list 在子级 `llm generation` span 的 `input.value` 和 `llm.input_messages.*` 中查看。raw vector 永远不写入 trace。

## Runtime Trace Span

真实用户消息进入 Runtime Trace 后，会在同一个主 trace 下记录以下生命周期：

- `feishu_event`：单个飞书消息处理的根业务 span。
- `admission_gate`：群白名单、@ 机器人、用户白名单、过期事件和 bot loop gate。
- `dedup`：SQLite runtime store claim 状态，区分首次处理、duplicate、agent/reply retry。
- `queue_wait`：同一话题串行队列等待耗时。
- `queue_processing`：进入 per-thread queue 后的处理阶段。
- `agent_run`：调用 support evidence collector 和 Agent SDK 生成答案。
- `visible_reply_render`：把内部结构化答案渲染为飞书群可见文本，并做可见回复校验。
- `channel_reply`：调用飞书 `im.v1.message.areply` 且 `reply_in_thread=true`。
- `channel_reply_result`：Feishu legacy 记录 `replied` / `reply_failed`、reply message hash 和 reply latency；OpenClaw 记录 `payload_built` / `payload_only`。
- `feishu_event_status`：最终状态，例如 `replied`、`duplicate`、`reply_failed`、`ignored`。

Feishu tracing metadata 默认只记录 `chat_id_hash`、`thread_id_hash`、`message_id_hash`、`session_id_hash`、hash 化 `session.id` 和输入长度。普通日志同样优先使用 hash。

## 本地事件阶段

以下阶段用于本地日志、调试或 future instrumentation；如果没有 active trace，它们的裸 `custom_span` 会被 Agents SDK 处理为 NoOp，不会出现在 OpenAI Traces / Phoenix 面板：

- `receive_event`：SDK WebSocket 收到事件。
- `parse_event`：将 SDK payload 解析为内部 `FeishuMessageEvent`。
- `backfill_poll`：短周期回扫目标群最近消息，兜底 SDK WebSocket 漏推。

Admission/debug trace 独立于 Runtime Trace。默认配置下 duplicate / ignored 事件不会污染主 tracing 面板；只有在 `SUPPORT_TRACE_ADMISSION_MODE=full` 或采样命中时才会出现 `Feishu Bridge Admission`。

## Sessions 页

Phoenix 的 Sessions 页不是自动从 `group_id` 生成的，它依赖 OpenInference 语义字段：

- `session.id`：生产默认使用 hash 化 session ID。飞书会基于 `feishu:{chat_id}:thread:{thread_id}` 得到稳定 hash，OpenClaw 同理，终端基于 `terminal-chat`。
- `input.value`：在 `SUPPORT_AGENT_TRACE_INCLUDE_SENSITIVE_DATA=true` 时记录，只在 `support_runtime_turn` 上表示本轮用户原始消息和安全附件摘要。
- `output.value`：在 `SUPPORT_AGENT_TRACE_INCLUDE_SENSITIVE_DATA=true` 时记录，只在 `support_runtime_turn` 上表示最终可见输出，也就是实际准备发给 Feishu thread、OpenClaw payload 或 Terminal 的文本。

因此，同一客服在同一飞书话题里多次追问时，应在 Sessions 页聚合到同一个 hash session；当前调试视图应能直接看到完整业务输入输出。子 span 中的 `runner.input.value`、`internal_answer.value`、`visible_reply.value` 只是调试字段，不作为 Sessions 页 first input / last output 的依据。

## 视觉验收

清理后的 tracing 面板应满足：

- 发送一条真实 Feishu legacy 测试消息后，按时间窗口、`message_id_hash` 或 `thread_id_hash` 过滤，只看到一个主 trace：`Feishu Support Runtime Turn`。
- Feishu legacy 主 trace 展开后能看到完整链路：`feishu_event`、`admission_gate`、`dedup`、`queue_wait`、`queue_processing`、`agent_run`、`support_runtime_turn`、`support_core_runtime`、`intake_pipeline`、`retrieval_pipeline`、`agent_answer`、`runner_run`、`answer_contract_check`、`visible_reply_render`、`channel_reply`、`channel_reply_result`，其中 `channel_reply_result.reply_status` 应为 `replied` 或 `reply_failed`。
- 发送非截图产品损坏图时，主 trace 下应同时看到 OCR 跳过或识别、`ingestion_image_embedding`、`visual_embedding_provider_call`、`ingestion_visual_understanding`、`visual_understanding_provider_call`；`runner.input.value` 应包含产品/部件/损伤位置/损伤类型等视觉摘要。
- 发送聊天截图时，主 trace 下应以 OCR 为主，不应把截图误判成产品损坏图；`runner.input.value` 应包含 OCR 文本或归一化问题。
- OpenClaw live 请求只出现一个主 trace：`OpenClaw Support Runtime Turn`，其中 `channel_reply_result.reply_status=payload_built` 且 `reply_transport=payload_only`。
- Terminal 普通 turn 只出现一个主 trace：`Terminal Support Runtime Turn`；Terminal 不要求 `channel_reply` 或 `channel_reply_result`。
- `contractOnly=true` 和默认 smoke 不产生 Runtime Trace。
- `visible_reply_render`、`channel_reply`、`answer_contract_result` 不应作为单独的 `$0 / 0ms` trace 出现在主 trace 后面。
- 真实 token/cost 只体现在主 runtime trace 上；回复渲染和发送只作为主 trace 子 span。
- Sessions 页可以看到 hash 化 `session.id`，同一话题连续两轮消息应聚合在同一个 session 下，并显示来自 `support_runtime_turn` 的可读 first input / last output。
- 验收截图至少保留 trace 列表视图和主 trace 展开后的 span tree。

## 查看方式

1. 在广州服务器启动 Phoenix 服务。
2. 本机运行 `VIJIMcopilot`。
3. 输入一个测试问题。
4. 打开 Phoenix 或 OpenAI Platform traces。
5. 按 workflow name 或 Phoenix project name 过滤：

```text
VIJIM-after-sale-copilot
VIJIM-after-sale-copilot
```

## 说明

- 如果显式启用 OpenAI hosted tracing，且模型 provider 不是 OpenAI，同时没有配置 `OPENAI_TRACING_API_KEY`，启动时会给出明确错误。
- 如果 OpenAI 组织开启 Zero Data Retention，OpenAI 托管 tracing 可能不可用。
