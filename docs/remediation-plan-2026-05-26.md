# 代码健康度修复计划

日期：2026-05-26

状态：已执行。本计划不恢复 Web Demo、HTTP API 或 webhook runtime。

## 已完成修复

- `docs/obsidian-master-note.md` 已从断链修复为可打开的 Obsidian 软链接。
- 当前目标文件：`/Users/chenyu/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian/WashMyBrain/飞书客服群 AI 智能客服支持 MVP 方案汇总.md`
- 验证方式：`test -e docs/obsidian-master-note.md` 通过，且可读取文档开头 metadata。
- 飞书采集脚本已改为默认 dry-run；所有 Base/Drive 写操作需要显式 `--apply`。
- 飞书采集目标已从脚本硬编码默认值改为环境变量或 CLI 参数。
- `agent_mvp.py` 已收缩为 CLI 编排，终端 UI、session、runtime helper 已拆到 `src/agent_runtime/terminal/`。
- 已新增答案 contract validator，并在终端 Agent 输出后做非阻断式内部校验提醒。
- trace metadata app 已从 `agent-runtime-test` 修正为 `ulanzi-after-sell-copilot`。
- 验证结果：`make test` 通过 19 个测试，`make compile` 通过。

## 参考约束

OpenAI Agents SDK 仍是运行时编排层，DeepSeek 作为 OpenAI-compatible LLM provider 使用。

DeepSeek 官方文档对后续架构有以下影响：

- 多轮对话：DeepSeek `/chat/completions` 是无状态 API，每轮请求需要由客户端传入完整上下文。当前 Agents SDK `SQLiteSession` 正好承担这个职责。
- JSON Output：DeepSeek 通过 `response_format={"type": "json_object"}` 保证合法 JSON，但 prompt 中必须包含 `json` 字样和目标 JSON 示例，且需要设置足够 `max_tokens` 防截断。
- Tool Calls：DeepSeek tool call 与 OpenAI 格式兼容；strict mode 需要 beta base URL，且 function schema 要满足 strict JSON Schema 约束。
- Thinking Mode：DeepSeek thinking 默认开启；涉及 tool call 的 thinking 轮次需要正确保留 `reasoning_content`。当前优先让 Agents SDK 管理消息，不自行拼接底层 tool-call history。
- 上下文硬盘缓存：DeepSeek 默认开启缓存；优化重点是保持 system prompt、稳定上下文摘要和工具说明的前缀稳定，便于缓存命中。

参考链接：

- https://api-docs.deepseek.com/zh-cn/guides/multi_round_chat/
- https://api-docs.deepseek.com/zh-cn/guides/json_mode/
- https://api-docs.deepseek.com/guides/tool_calls
- https://api-docs.deepseek.com/guides/thinking_mode
- https://api-docs.deepseek.com/zh-cn/guides/kv_cache/

## 计划 1：飞书采集脚本防误写与配置外置

目标：让 `scripts/ingest_feishu_support_data.py` 默认只读/干跑，只有显式确认才写入飞书 Base 或 Drive。

实施步骤：

1. 增加配置对象
   - 新增脚本内 `IngestConfig` dataclass，字段包括 `base_token`、`group_chat_id`、`luz_chat_id`、`table_events`、`table_raw_messages`、`table_media`、`table_actions`。
   - 默认从环境变量读取：`FEISHU_SUPPORT_BASE_TOKEN`、`FEISHU_SUPPORT_GROUP_CHAT_ID`、`FEISHU_SUPPORT_LUZ_CHAT_ID`、`FEISHU_SUPPORT_TABLE_*`。
   - 当前硬编码值仅作为 `.env.example` 或 docs 中的示例，不再作为脚本默认写入目标。

2. 改默认执行模式
   - `--dry-run` 改为默认行为。
   - 新增必填写入开关 `--apply`；没有 `--apply` 时只抓取、转换、写本地 audit 文件，不调用 Base batch create、Drive upload、record upsert。
   - `--sync-drive-images` 也必须同时传 `--apply`，否则只输出待同步队列摘要。

3. 增加目标摘要和确认
   - 执行开始打印：Base token 后 6 位、table IDs、chat IDs、时间窗口、是否 apply、是否 sync drive。
   - `--apply` 模式下要求配置完整，否则直接失败。
   - 不做交互式确认，避免自动化环境卡住；用显式 flag 作为确认。

4. 保持输出兼容
   - 保留当前 `data/feishu_ingest/<run_id>/` audit 输出结构。
   - dry-run 仍写 `messages.cleaned.json` 和转换 payload，但文件名加前缀或 metadata 标注 `dry_run=true`。
   - 写入模式继续保持 idempotent：读取已有 key 后只补新增记录。

5. 测试
   - 用 monkeypatch mock `run_json` / `run`，验证无 `--apply` 时不会调用 `record-batch-create`、`drive +upload`、`record-upsert`。
   - 验证缺配置 + `--apply` 会失败。
   - 验证配置从环境变量覆盖成功。

验收标准：

- 默认运行不会写入任何飞书 Base 或 Drive。
- 所有外部写操作都只能在 `--apply` 下发生。
- 脚本顶部明确说明这是 ops ingestion utility，不是 runtime Agent tool。

## 计划 2：拆分 `agent_mvp.py` 的终端入口职责

目标：保持 `chatcopilot`、`make chat` 和所有终端命令不变，把终端 UI 与 Agent runtime 逻辑分开，方便后续接 Feishu bot 或自动化测试。

实施步骤：

1. 新增内部包
   - `src/agent_runtime/terminal/ui.py`：颜色、宽字符、box rendering、inline menu、help/status/agents/tools 输出。
   - `src/agent_runtime/terminal/session.py`：`build_terminal_session`、`session_item_count`、`clear_context`、`session_items_to_text`。
   - `src/agent_runtime/terminal/runtime.py`：`build_compactor`、`compact_context`、`run_turn`。

2. 收缩 `agent_mvp.py`
   - 只保留 `cli()`、`main()`、命令分发、root path bootstrap。
   - 继续导出测试当前使用的函数名，或者同步更新测试 import 到新模块。
   - 不改变 `pyproject.toml` 的 `chatcopilot = "agent_mvp:cli"`。

3. 保持行为不变
   - `/model`、`/clear`、`/compact`、`/info`、`/status`、`/agents`、`/tools`、`/help`、`/bye` 输出保持等价。
   - `SQLiteSession("terminal-chat")` 和 `SessionSettings(limit=...)` 保持现状。
   - `flush_traces()` 调用时机保持在每次 Agent/compactor run 后。

4. 测试
   - 迁移现有 terminal utility 测试到新模块。
   - 新增 smoke test：`agent_mvp.cli` 存在，console script 入口仍可解析。

验收标准：

- `make test` 和 `make compile` 通过。
- `agent_mvp.py` 只承担 CLI orchestration，不再包含渲染和 runtime helper 的大段实现。

## 计划 3：答案 Contract 校验与 DeepSeek JSON 输出策略

目标：短期先增加非侵入式输出校验；中期为 DeepSeek JSON Output 和 OpenAI Agents SDK structured output 留出路径。

实施步骤：

1. 新增本地 validator
   - 新增 `src/agent_runtime/copilot/answer_contract.py`。
   - 定义固定字段列表：`问题类型`、`运行模式`、`置信度`、`用户问题摘要`、`SKU 命中`、`建议回复（供客服参考，可复制调整）`、`建议排查步骤`、`需要追问`、`正式依据`、`历史参考`、`工单草稿`。
   - 提供 `validate_answer_contract(text: str) -> list[ContractIssue]`。
   - 校验字段存在、顺序、禁止承诺词、未接入 RAG 时不得出现伪文档/伪案例链接。

2. 接入 terminal runtime
   - `run_turn` 打印模型输出前执行 validator。
   - 初期不阻断输出，只在终端结果后追加内部警告段，或写到 debug log；避免影响客服测试体验。
   - 后续稳定后再升级为 output guardrail 或重试机制。

3. DeepSeek JSON Output 预研路径
   - 先不直接把客服最终输出改成 JSON，避免破坏可复制文本。
   - 为后续添加双层输出做准备：模型先产出结构化 JSON，再由本地 renderer 渲染为当前 11 字段文本。
   - 如果启用 DeepSeek JSON Output，需要在 prompt 中明确包含 `json` 字样和完整 JSON 示例，并配置 `response_format={"type": "json_object"}`；同时保留 max_tokens 防截断策略。

4. Tool call strict mode 暂不启用
   - 当前 Agents SDK 通过 OpenAI-compatible endpoint 管理 tools，先不切换 DeepSeek beta `strict` mode。
   - 等工具返回结构化 schema 后，再评估 beta base URL 和 strict schema 兼容性。

5. 测试
   - 字段完整且顺序正确：无 issue。
   - 缺字段、错序、包含“可以退款/直接换新/承诺补发/维修时效”等：返回 issue。
   - RAG 未接入时出现伪文档链接或伪案例日期：返回 issue。

验收标准：

- 当前文本输出格式不变。
- 本地测试能捕获 contract 破坏。
- 后续切换 JSON-first 输出时不需要重写业务字段定义。

## 计划 4：trace metadata 项目名修正

目标：让 OpenAI Traces 中的 app metadata 与项目名一致。

实施步骤：

1. 修改 `src/agent_runtime/llm.py`
   - 将 `trace_metadata["app"]` 从 `agent-runtime-test` 改为 `ulanzi-after-sell-copilot`。

2. 增加测试
   - 新增测试覆盖 `build_run_config(Settings(...)).trace_metadata["app"]`。
   - 同时确认 `llm_base_url`、`llm_model`、外部 metadata merge 行为不变。

验收标准：

- `make test` 通过。
- 新 trace 的 metadata app 可用于过滤当前项目。

## 推荐执行顺序

1. 飞书采集脚本防误写。
2. trace metadata 项目名修正。
3. 答案 contract validator。
4. `agent_mvp.py` 轻量拆分。

原因：先降低外部写入和排障风险，再提升 Agent 输出安全性，最后做结构拆分。
