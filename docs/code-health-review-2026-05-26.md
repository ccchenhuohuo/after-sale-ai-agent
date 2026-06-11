# 项目代码健康度深度审查报告

审查日期：2026-05-26

审查对象：`ulanzi-after-sell-copilot` 当前工作区状态

审查方式：只读审查为主，执行了本地测试与编译检查；未运行会写入飞书、云盘或外部业务系统的脚本。

## Executive Summary

当前项目已经完成终端优先 MVP 的主要收敛：运行入口只剩 `chatcopilot` / `agent_mvp.py`，Agent SDK 工具注册清楚，旧 Web Demo、HTTP API、webhook runtime 和本地规则式 demo 知识没有作为 Git 跟踪代码残留。`make test` 通过 10 个测试，`make compile` 无语法错误。

主要健康风险集中在治理与下一阶段扩展：

- P1：`docs/obsidian-master-note.md` 是断掉的软链接，但 README 和资料索引都把它作为关键参考。
- P1：tracing 示例和默认配置允许把模型/工具输入输出写入 OpenAI traces，和售后群隐私场景不匹配。
- P1：飞书采集脚本默认执行真实写入，且 Base/chat/table IDs 硬编码在脚本里，防误操作能力不足。
- P2：终端入口 `agent_mvp.py` 已成为 UI、session、Agent run、上下文压缩、模型切换的混合模块，当前可接受，但会阻碍后续接入多入口或自动化测试。
- P2：答案 contract 主要靠 prompt 约束，没有结构化输出或 output guardrail，测试也只验证 prompt 文本。
- P2：项目改名后 trace metadata 仍写 `agent-runtime-test`，会影响 Traces 排查。

优先建议：先修 P1，再补 contract/guardrail 测试和拆分 runtime 边界。不要先做大重构。

## Baseline

- 分支：`main`
- 当前 HEAD：`58952bb`
- 工作区状态：存在大量已有未提交修改，另有未跟踪 `docs/obsidian-master-note.md`。本报告未回退或覆盖这些改动。
- 依赖版本：
  - `openai-agents 0.17.3`
  - `openai 2.38.0`
  - `pydantic-settings 2.14.1`
  - `httpx 0.28.1`
  - `pytest 9.0.3`
- 验证结果：
  - `make test`：10 passed
  - `make compile`：通过
- 旧 demo 扫描结论：
  - 未发现 Git 跟踪的 FastAPI、Flask、Streamlit、Gradio、HTTP API、webhook runtime 入口。
  - `demo` / `webhook` / `本地确定性` 命中主要是文档中“已移除”的历史说明，以及 `rag.py` 的占位注释。

## Findings

### P1: 关键设计文档索引指向断掉的软链接

状态：已在当前工作区修复。`docs/obsidian-master-note.md` 现在指向实际存在的 Obsidian 笔记：`飞书客服群 AI 智能客服支持 MVP 方案汇总.md`。

证据：

- `README.md:95-97` 将 `docs/obsidian-master-note.md` 列为关键参考。
- `docs/source-index.md:7-8` 将同一文件列为 Obsidian 总笔记软链接。
- 文件系统检查显示 `docs/obsidian-master-note.md` 是 broken symbolic link，目标为本机 Obsidian vault 中不存在的路径。

影响：

- 资料索引不可追踪，后续工程师无法打开“动态总笔记”确认项目决策。
- `source-index.md` 当前承担资料总入口角色，该断链会削弱文档体系可信度。

建议动作：

- 修复软链接目标，或把该文件改为普通 Markdown stub，内部写明真实 Obsidian 路径、维护人、失效处理方式。
- 增加一个轻量检查：`test -e docs/obsidian-master-note.md`，避免关键索引再次断链。

### P1: tracing 默认/示例包含敏感数据，不适合售后群数据

证据：

- `.env.example:16-18` 设置 `SUPPORT_AGENT_TRACING_DISABLED=false` 且 `SUPPORT_AGENT_TRACE_INCLUDE_SENSITIVE_DATA=true`。
- `src/agent_runtime/settings.py:21-23` 默认 `support_agent_trace_include_sensitive_data=True`。
- `docs/tracing.md:22-26` 示例同样建议 include sensitive data 为 true。
- `src/agent_runtime/llm.py:80-85` 把该配置直接传给 `RunConfig(trace_include_sensitive_data=...)`。

影响：

- 售后群问题、客户截图文字、内部处理建议、工具输入输出可能被写入 OpenAI-hosted traces。
- 项目文档明确要求“不必要地输出客户隐私信息”，但 tracing 配置会在调试侧扩大数据暴露面。

官方依据：

- OpenAI Agents SDK 配置文档说明 `trace_include_sensitive_data=False` 可以保留 spans 但不包含潜在敏感的 LLM/tool 输入输出。
- Agents SDK tracing 文档说明 `Runner.run`、LLM generation、function tool call 都会被自动 trace，因此该配置影响范围包括工具调用。

建议动作：

- 将 `.env.example` 和 `docs/tracing.md` 的 `SUPPORT_AGENT_TRACE_INCLUDE_SENSITIVE_DATA` 改为 `false`。
- 将 `Settings.support_agent_trace_include_sensitive_data` 默认值改为 `False`。
- 如确需调试敏感内容，只允许在本地临时 `.env` 中显式开启，并在文档中标注“仅限受控调试”。

### P1: 飞书采集脚本默认执行真实写入，缺少强制确认和环境隔离

状态：已在当前工作区修复。脚本默认 dry-run，所有 Base/Drive 写入需要显式 `--apply`，目标 Base/chat/table IDs 改为环境变量或 CLI 参数。

证据：

- `scripts/ingest_feishu_support_data.py:28-36` 硬编码 Base token、群聊 ID、P2P chat ID 和 table IDs。
- `scripts/ingest_feishu_support_data.py:650-666` 提供 `--dry-run`，但默认不是 dry-run。
- `scripts/ingest_feishu_support_data.py:713-843` 在未传 `--dry-run` 时会读取已有记录、批量创建 raw/event/action/media，并同步 Drive 图片。
- `scripts/ingest_feishu_support_data.py:674-682` 的 `--sync-drive-images` 分支同样会直接创建/上传/patch 外部资源。

影响：

- 开发者误运行脚本会写入正式飞书 Base 或云盘。
- 硬编码 token 和 table IDs 让 dev/test/prod 环境难以隔离，也不利于审计脚本作用域。

建议动作：

- 改为默认 dry-run，增加必填 `--apply` 或 `--write-base` 才允许写入。
- 将 Base token、table IDs、chat IDs 移到配置文件或环境变量，并在命令输出中打印目标环境摘要。
- 对 Drive sync 也加入显式确认参数，避免只想检查本地 run-dir 时误 patch 远端记录。

### P2: `agent_mvp.py` 职责过多，下一阶段会阻碍扩展

状态：已在当前工作区修复。`agent_mvp.py` 已收缩为 CLI 编排，终端 UI、session、runtime helper 已拆入 `src/agent_runtime/terminal/`。

证据：

- `agent_mvp.py:32-80` 处理 ANSI、宽字符和 box rendering。
- `agent_mvp.py:97-151` 处理模型预设、session、状态输出。
- `agent_mvp.py:190-282` 处理交互式 inline info menu。
- `agent_mvp.py:339-385` 构建并运行上下文压缩 Agent。
- `agent_mvp.py:388-406` 执行主 Agent turn。
- `agent_mvp.py:409-457` 处理主循环和命令分发。

影响：

- 当前 MVP 可接受，但 UI、session、Agent run、命令分发混在一个文件里，后续接入 Feishu bot、HTTP worker、自动化 CLI 测试时会重复或难以复用。
- 终端 UI 的 TTY 细节会污染核心 Agent runtime 逻辑。

建议动作：

- 不做大重构，先按边界拆 3 个内部模块：
  - terminal UI/commands：渲染、按键、prompt、status。
  - runtime service：`run_turn`、`compact_context`、session 创建。
  - CLI entry：`main` 和命令分发。
- 保持 public 命令和 `chatcopilot` 入口不变。

### P2: 答案 contract 依赖 prompt，缺少结构化输出或 output guardrail

状态：已在当前工作区部分修复。已新增本地 answer contract validator，并在终端输出后做非阻断式内部校验；尚未升级为 SDK output guardrail 或 JSON-first 输出。

证据：

- `src/agent_runtime/copilot/prompts.py:23-65` 用 prompt 规定 11 个字段。
- `src/agent_runtime/copilot/support_copilot.py:9-15` 创建 `Agent` 时未设置 `output_type`、`output_guardrails` 或 hooks。
- `tests/test_agent_contract.py:22-43` 只断言 prompt 包含字段和约束文本，没有验证真实 Agent 输出。

影响：

- 模型仍可能漏字段、调换字段顺序、输出客户口吻、或在低置信度场景给出过度确定结论。
- 下一阶段接入真实 RAG 后，如果没有输出层校验，风险会从“低置信保守建议”升级为“错误引用或错误承诺”。

官方依据：

- OpenAI Agents SDK 的 Agent 支持 `output_type`、input/output guardrails、hooks 和 tool behavior。
- Guardrails 文档说明 output guardrails 可检查最终输出并在 tripwire 时中止运行。

建议动作：

- 短期添加纯 Python post-run validator：检查 11 个字段、顺序、禁止承诺词、正式依据/历史参考占位语。
- 中期评估用 Pydantic `output_type` 固化字段，或添加 SDK output guardrail。
- 给 validator 增加单元测试，先不改变用户可见格式。

### P2: 项目重命名后 trace metadata 仍使用旧 app 名

状态：已在当前工作区修复。`trace_metadata["app"]` 已改为 `ulanzi-after-sell-copilot`，并新增测试覆盖。

证据：

- `pyproject.toml:6-8` 项目名已是 `ulanzi-after-sell-copilot`。
- `README.md:1-5` 项目名已是 `ulanzi after-sell copilot`。
- `src/agent_runtime/llm.py:72-75` trace metadata 中仍写 `"app": "agent-runtime-test"`。

影响：

- OpenAI Traces 中 app metadata 与 workflow name 不一致，排查时容易过滤错项目。
- 这是小问题，但会影响“可排查性”。

建议动作：

- 将 trace metadata app 改为 `ulanzi-after-sell-copilot`。
- 增加测试覆盖 `build_run_config().trace_metadata["app"]`。

### P2: SKU catalog 默认路径写死到单日版本，缺少 latest/active 指针策略

证据：

- `src/agent_runtime/settings.py:32` 默认 `sku_catalog_path` 指向 `data/sku_catalog/processed/2026-05-26/sku_support_catalog-2026-05-26.csv`。
- `.env.example:29` 使用同一个日期路径。
- `README.md:115` 和 `docs/source-index.md:62` 记录当前生成 CSV。
- `scripts/evaluate_sku_catalog_hits.py:224-228` 默认输入/输出也绑定 2026-05-26。

影响：

- 每次 SKU 更新都需要同步改多个文档和默认参数。
- 如果数据目录中存在新版本，运行时仍可能继续加载旧 CSV。

建议动作：

- 保留历史日期产物，但增加稳定入口，例如 `data/sku_catalog/processed/current/sku_support_catalog.csv` 或 `data/sku_catalog/active_catalog.csv`。
- `Settings` 和 `.env.example` 默认指向稳定入口；文档中的日期文件作为审计产物记录。
- 构建脚本输出后可打印“请更新 active pointer”的提醒；是否自动更新由后续决定。

### P2: 工具返回值均为自由文本，后续 RAG/评估扩展成本偏高

证据：

- `src/agent_runtime/tools/sku_catalog.py:91-124` 返回纯文本列表。
- `src/agent_runtime/tools/rag.py:6-40` 返回纯文本占位结果。
- `src/agent_runtime/tools/ticket.py:4-23` 返回纯文本工单草稿。

影响：

- Agent 当前能读懂，但测试、评估、UI 渲染、后续 Feishu/Base 写入都难以可靠消费。
- 真实 RAG 接入后，文档名、章节、链接、score、source_type 等字段如果仍只拼文本，会增加解析脆弱性。

建议动作：

- 先保持 model-visible 文本不变，同时在工具内部定义结构化结果类型或 JSON shape。
- 对 RAG 工具预留字段：`source_type`、`title`、`section`、`url`、`score`、`verified`、`snippet`。
- 对工单草稿预留字段：`title`、`severity`、`missing_info`、`suggested_owner`、`next_action`。

### P2: `scripts/ingest_feishu_support_data.py` 有部分已废弃或生命周期不清的逻辑

证据：

- `scripts/ingest_feishu_support_data.py:403-427` 定义 `upload_attachment`，但当前主流程未调用。
- 文档 `docs/base-demo-reference.md` 和 `docs/knowledge-base.md` 说明 Base 原生附件字段因限制未使用，Drive 链接才是当前归档约定。
- `scripts/ingest_feishu_support_data.py:349-360` 仍把“换新 / 补发 / 退款退货”作为 action type 分类，虽然这是内部动作日志，但未来如果直接入 RAG 容易被误用。

影响：

- 未使用函数会让维护者误判 Base native attachment 仍是支持路径。
- 内部处理动作分类如果未经 case-card 审核进入检索层，会与“不得承诺换新/退款/补发”的安全边界冲突。

建议动作：

- 给 `upload_attachment` 加明确注释：暂存/废弃原因；或移除并在历史说明里保留背景。
- 在脚本顶部增加 lifecycle 注释：当前是 one-off/ops ingestion utility，不是 runtime tool。
- 在 action type 相关注释中标明“仅内部动作分类，不得作为客户承诺依据”。

### P3: `data/official_docs` 和 `data/historical_qa` 占位目录没有本地说明

证据：

- Git 跟踪 `data/official_docs/.gitkeep` 和 `data/historical_qa/.gitkeep`。
- `.gitignore:17-18` 只忽略 `data/feishu_ingest/` 和 `data/sku_catalog/`。
- 文档解释了未来官方 KB 和历史 QA，但没有说明这两个 tracked 占位目录的允许内容、敏感边界和生命周期。

影响：

- 后续开发者可能把真实文档、未脱敏 case cards 或 embedding 产物直接放进 Git 跟踪目录。

建议动作：

- 增加 `data/README.md` 或在 `docs/knowledge-base.md` 加数据目录说明。
- 明确哪些文件可以提交，哪些只能本地保存或进入受控存储。

### P3: 本地生成缓存和 egg-info 已存在，虽被忽略但会干扰扫描输出

证据：

- 本地存在 `.pytest_cache`、`__pycache__`、`src/agent_runtime/__pycache__`、`tests/__pycache__`。
- 本地存在 `src/ulanzi_after_sell_copilot.egg-info`，被 `.gitignore` 的 `*.egg-info/` 覆盖。
- `make compile` 会扫描 `src/ulanzi_after_sell_copilot.egg-info`。

影响：

- 不影响 Git 状态，但会让文件扫描和 compile output 噪声变大。

建议动作：

- 可选清理本地缓存与 egg-info。
- 或将 compile 命令限定到实际包路径：`agent_mvp.py src/agent_runtime tests`。

## Architecture Notes

当前架构清晰度总体合格：

- `README.md:7-28`、`docs/architecture.md:11-44`、`docs/knowledge-base.md:22-40` 对当前运行时描述一致。
- `src/agent_runtime/copilot/support_copilot.py:9-15` 明确注册 4 个工具。
- `src/agent_runtime/tools/rag.py:6-40` 明确返回空结果，避免本地 demo 知识伪装成正式依据。
- `docs/answer-contract.md:48-64` 和 prompt 的边界规则基本一致。

当前不建议恢复 Web Demo、HTTP API 或 webhook runtime。更合理的扩展顺序是：

1. 先固化输出校验和安全 guardrail。
2. 再接入正式 KB 的最小 RAG。
3. 再把历史 Feishu 数据转 reviewed case cards。
4. 最后再做 Feishu bot / Base 写入等外部入口。

## SDK Conformance

符合点：

- `src/agent_runtime/copilot/support_copilot.py:9-15` 使用 plain `Agent` + function tools，符合 Agents SDK 基本模型。
- `agent_mvp.py:388-403` 使用 `Runner.run(..., session=session, run_config=...)`，符合 SDK session 自动管理方式。
- `agent_mvp.py:115-119` 使用 `SQLiteSession` 和 `SessionSettings(limit=...)` 控制上下文长度，符合 Sessions 文档。
- `src/agent_runtime/llm.py:52-58` 使用 `set_default_openai_client` 和 `set_tracing_export_api_key` 分离 OpenAI-compatible LLM provider 与 OpenAI tracing key，方向正确。
- `agent_mvp.py:373` 和 `agent_mvp.py:403` 在每次 terminal run 后调用 `flush_traces()`，符合 tracing 文档对立即导出的建议。

需要改进：

- tracing sensitive data 默认应改为 false。
- 对输出 contract 应增加 SDK output guardrail 或本地 post-run validator。
- 后续工具增多时，可以评估 `RunConfig.tool_execution` 限制本地 function tool 并发，避免 RAG/外部 API 并发过高。
- 若未来有多入口或持久会话，应从内存 `SQLiteSession("terminal-chat")` 过渡到文件型 session 或入口隔离的 session ID 策略。

官方参考：

- Agents SDK overview: https://platform.openai.com/docs/guides/agents-sdk/
- Running agents: https://openai.github.io/openai-agents-python/running_agents/
- Sessions: https://openai.github.io/openai-agents-python/sessions/
- Configuration: https://openai.github.io/openai-agents-python/config/
- Tracing: https://openai.github.io/openai-agents-python/tracing/

## Docs / Data Hygiene

文档体系优点：

- `docs/source-index.md` 已经承担总索引角色，外部 Feishu 资源、本地说明、调试资源、数据路径都有基本说明。
- `docs/knowledge-base.md` 清楚地区分正式 KB 和历史案例库。
- `docs/presales-knowledge-reference.md` 明确售前资料不得驱动售后诊断。
- `docs/sku-support-catalog.md` 对 SKU join、过滤、字段来源、使用边界说明较完整。

文档治理风险：

- 断链软链接必须先修。
- README 和 source-index 使用绝对本地路径，适合当前单机环境，但跨机器协作会失效；建议保留相对路径为主，绝对路径只放在“本机备注”。
- `docs/source-index.md` 记录会议纪要“访问待授权”，后续应标注最后检查日期，避免长期悬空。
- `data/official_docs` 和 `data/historical_qa` 目录用途没有明确提交边界。

## Test Gaps

现有测试覆盖：

- Prompt 字段和关键安全文本：`tests/test_agent_contract.py:22-43`。
- 工具注册顺序：`tests/test_agent_contract.py:46-55`。
- 模型预设、compactor、session item 文本转换、terminal prompt、info box：`tests/test_agent_contract.py:58-110`。
- Feishu parser 基础 JSON text、prefix trigger、strip prefix：`tests/test_feishu_parser.py:4-14`。

建议新增测试：

- P1：`.env.example` 与 `Settings` 字段一致，并断言敏感 tracing 示例默认 false。
- P1：`configure_agents_runtime` 在非 OpenAI provider 且 tracing enabled 但无 tracing key 时抛出明确错误。
- P2：`build_run_config` metadata app 与项目名一致。
- P2：`search_sku_catalog` 覆盖文件不存在、空 CSV、精确 SKU、SPU fallback、无命中、limit 上限。
- P2：`hybrid_search_kb` 和 `search_issue_history` 必须返回未接入/不得编造提示。
- P2：输出 validator 覆盖字段缺失、字段错序、禁止承诺词、正式依据伪造。
- P2：Feishu parser 覆盖富文本 content、空内容、非 JSON 字符串、mention_names trigger。
- P3：关键文档链接存在性检查，至少覆盖 `docs/obsidian-master-note.md`。

## Recommended Fix Order

1. 修复断链 `docs/obsidian-master-note.md`，保证资料索引可打开。
2. 将 tracing sensitive data 默认改为 false，并同步 `.env.example` 与 `docs/tracing.md`。
3. 给飞书采集脚本加 `--apply` 写入确认和配置外置，防止误写 Base/Drive。
4. 修正 `build_run_config` 的 trace metadata app 名称。
5. 增加输出 contract validator 和对应测试。
6. 给 SKU catalog 引入 stable active path，减少日期路径散落。
7. 给 `data/official_docs` / `data/historical_qa` 增加数据治理说明。
8. 轻量拆分 `agent_mvp.py`，先拆 UI 和 runtime service，不改变 public CLI。
9. 清理或标注 `upload_attachment` 等生命周期不清的脚本逻辑。
10. 后续真实 RAG 接入前，把工具返回结果结构化，避免纯文本结果成为长期接口。
