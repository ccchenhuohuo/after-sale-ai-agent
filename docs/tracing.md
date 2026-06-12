# Tracing 仪表盘

终端 Agent 使用 OpenAI Agents SDK tracing。可在 OpenAI Platform Logs 仪表盘查看 trace：

https://platform.openai.com/logs?api=traces

也可以将 traces 额外发送到 Tailscale 服务器 Phoenix。Phoenix 需要在广州服务器单独启动，本机继续运行 `chatcopilot`。

Phoenix 地址：

```text
http://opencloud.taild79054.ts.net:6006
```

## DeepSeek 模型与 OpenAI Tracing

DeepSeek 可以通过 OpenAI 兼容接口作为 LLM provider 使用，同时将 traces 单独导出到 OpenAI。

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
SUPPORT_AGENT_TRACING_DISABLED=false
SUPPORT_AGENT_TRACE_INCLUDE_SENSITIVE_DATA=false
SUPPORT_AGENT_TRACE_WORKFLOW_NAME=ulanzi after-sell copilot MVP

PHOENIX_TRACING_ENABLED=false
PHOENIX_COLLECTOR_ENDPOINT=http://opencloud.taild79054.ts.net:6006/v1/traces
PHOENIX_PROJECT_NAME=agent-runtime-test
```

运行行为：

- `LLM_API_KEY` 只用于模型调用。
- `OPENAI_TRACING_API_KEY` 只用于向 OpenAI 导出 traces。
- `SUPPORT_AGENT_TRACE_INCLUDE_SENSITIVE_DATA=false` 是默认安全配置，避免把完整用户问题、工具输入输出和历史话题内容写入 trace。
- `PHOENIX_TRACING_ENABLED=true` 时，运行时会通过 OpenTelemetry/OpenInference 同步发送 traces 到 Tailscale 服务器 Phoenix；仅应在确认数据边界和访问控制后开启。
- 终端运行使用 `group_id=terminal-chat`。
- `/model` 会在 Flash 和 Pro 预设之间切换，影响后续 Agent 轮次。
- 每次终端 Agents SDK 运行后都会调用 `flush_traces()`。

## v2 Loop 业务 Span

当前运行时会先收集结构化证据，再运行结构化输出 Agent。Phoenix / OpenAI traces 中应能看到以下业务 span：

- `support_turn`：单轮售后分析入口，包含 `entrypoint`、`loop_version=v2`、用户问题哈希和飞书线程哈希。
- `input_normalize`：输入归一化和 SKU token 计数，只记录哈希、长度和计数。
- `sku_resolve`：SKU 目录解析。
- `evidence_collect`：并发证据收集的父 span。
- `official_kb_search`：正式知识库检索；当前正式 KB 未接入时返回空证据。
- `history_search`：历史话题 RAG 检索。
- `media_search`：媒体观察证据检索。
- `evidence_pack`：证据包汇总，记录 SKU/正式依据/历史/媒体命中数和证据等级状态。
- `answer_contract_check`：最终中文答案 contract 校验。
- `feishu_visible_reply_check`：飞书可见回复校验，检查内部字段、Markdown 痕迹和售后承诺风险。

trace metadata 会包含 `entrypoint`、`loop_version`、`source`、`model_label`、`history_index_available`、`media_index_available` 等非敏感字段。默认不记录用户原文、工具原文或飞书消息内容。内部 evidence、contract 和 trace 信息只进入 runtime/tracing，不直接拼进飞书群回复。

## Feishu 端到端 Span

飞书 SDK 长连接入口会在同一个 trace group 下记录完整事件生命周期：

- `receive_event`：SDK WebSocket 收到事件，只记录 event type 和 `event_id_hash`。
- `parse_event`：将 SDK payload 解析为内部 `FeishuMessageEvent`。
- `feishu_event`：单个飞书消息处理的根业务 span。
- `admission_gate`：群白名单、@ 机器人、用户白名单、过期事件和 bot loop gate。
- `dedup`：SQLite runtime store claim 状态，区分首次处理、duplicate、agent/reply retry。
- `queue_wait`：同一话题串行队列等待耗时。
- `queue_processing`：进入 per-thread queue 后的处理阶段。
- `agent_run`：调用 support evidence collector 和 Agent SDK 生成答案。
- `reply_in_thread`：调用飞书 `im.v1.message.areply` 且 `reply_in_thread=true`。
- `reply_in_thread_result`：记录 reply status、reply message hash 和 reply latency。
- `feishu_event_status`：最终状态，例如 `replied`、`duplicate`、`reply_failed`、`ignored`。

Feishu 相关 metadata 和日志只记录 `chat_id_hash`、`thread_id_hash`、`message_id_hash`、`event_id_hash`、`queue_key_hash`、状态和耗时；不写入原始飞书 ID、消息正文或客户内容。

## 查看方式

1. 在广州服务器启动 Phoenix 服务。
2. 本机运行 `chatcopilot`。
3. 输入一个测试问题。
4. 打开 Phoenix 或 OpenAI Platform traces。
5. 按 workflow name 或 Phoenix project name 过滤：

```text
ulanzi after-sell copilot MVP
agent-runtime-test
```

## 说明

- 如果启用 tracing，且模型 provider 不是 OpenAI，同时没有配置 `OPENAI_TRACING_API_KEY`，启动时会给出明确错误。
- 如果 OpenAI 组织开启 Zero Data Retention，OpenAI 托管 tracing 可能不可用。
