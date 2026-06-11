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
