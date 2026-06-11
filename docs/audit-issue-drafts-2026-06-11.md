# 审计问题 Issue 草稿（2026-06-11）

> 说明：当前容器没有配置 GitHub remote、没有 `gh` CLI，也没有可用的 GitHub token，因此无法直接在远端 issue tracker 中创建 issue。本文件把审计报告中的所有问题整理为可直接复制到 GitHub/GitLab issue 的提交草稿。

## Issue 1：修复飞书 mention id 字典解析逻辑

**Labels**: bug, feishu, admission, high-priority

### 背景

飞书事件中的 `mentions[].id` 可能是字符串，也可能是包含 `open_id` / `union_id` / `user_id` 的字典。当前 `_mention_ids()` 先读取 `item.get("id")`，再判断该值是否为 `None`，导致 `id` 为字典时不会进入字典解析分支，可能把整个 dict 转成字符串。

### 影响

当生产环境只配置 `FEISHU_BOT_OPEN_ID` 或依赖 open_id 精确识别 @ 机器人时，机器人可能无法识别真实 @，从而忽略本应处理的客服群消息。

### 建议修改

- 在 `src/agent_runtime/feishu/events.py` 中重写 `_mention_ids()` 的 `id` 解析顺序。
- 先判断 `raw_id = item.get("id")` 是否为 dict。
- 若为 dict，按 `open_id`、`union_id`、`user_id` 顺序提取。
- 若为非空标量，再转为字符串。
- 增加 dict id 与 string id 两类单元测试。

### 验收标准

- `mentions=[{"id": {"open_id": "ou_bot"}, "name": "飞书 CLI"}]` 解析为 `("ou_bot",)`。
- `mentions=[{"id": "ou_bot", "name": "飞书 CLI"}]` 解析为 `("ou_bot",)`。
- `should_handle_event()` 在只配置 bot open_id 时可以正确接受 @ 机器人事件。

## Issue 2：严格校验飞书 webhook verification token

**Labels**: security, feishu, webhook, high-priority

### 背景

`_verify_token()` 目前只在 payload 中存在 token 且 token 不匹配时拒绝请求。若配置了 `FEISHU_VERIFICATION_TOKEN`，但请求完全不带 token，则会被放行。

### 影响

如果 webhook 入口被部署到公网，攻击者可能构造缺少 token 的请求绕过 verification token 校验，增加伪造事件进入后续处理链路的风险。

### 建议修改

- 在 `src/agent_runtime/feishu/webhook.py` 中调整 `_verify_token()`：
  - 若未配置 `FEISHU_VERIFICATION_TOKEN`，保持现有兼容行为。
  - 若已配置，则要求根级 `token` 或 `header.token` 必须存在且匹配。
  - token 缺失或不匹配都返回 403。
- 补充 webhook token 校验测试。

### 验收标准

- 配置 token 且 payload token 匹配时通过。
- 配置 token 且 payload token 缺失时返回 403。
- 配置 token 且 payload token 错误时返回 403。
- 未配置 token 时不因 token 校验失败。

## Issue 3：区分 in-progress 与 replied 状态以支持失败重试

**Labels**: reliability, feishu, sqlite, retry, high-priority

### 背景

当前飞书事件在处理前写入 `seen_events`。如果后续 LLM 调用或 `reply_in_thread()` 失败，同一个 message_id 后续重投会被判定为 duplicate，无法自动恢复。

### 影响

临时网络故障、飞书 API 抖动、模型服务短暂不可用都可能造成永久失败，需要人工介入重新触发。

### 建议修改

- 在 `src/agent_runtime/feishu/runtime_store.py` 中引入更细粒度事件处理状态，例如 `processing`、`replied`、`reply_failed`、`agent_failed`。
- 对已经 `replied` 的消息保持幂等，不重复回复。
- 对 `reply_failed` 或 `agent_failed` 且仍在重试窗口内的消息允许重试。
- 在 `src/agent_runtime/feishu/bridge.py` 中同步处理状态流转。

### 验收标准

- 第一次 reply 失败后，第二次同 message_id 投递可以重新处理。
- 已成功 replied 的 message_id 再次投递不会重复回复。
- `reply_ledger` 能区分成功、处理中和失败状态。

## Issue 4：校验失败时阻断高风险 Agent 输出并发送安全兜底回复

**Labels**: safety, answer-contract, feishu, high-priority

### 背景

当前 `validate_answer_contract()` 发现问题后，飞书链路仍会发送原始模型输出，只是在末尾追加“内部校验提醒”。如果原输出已经包含售后承诺、错误正式依据或未审核证据误用，提醒无法消除正文风险。

### 影响

客服可能复制或采纳违规正文，造成错误承诺、错误依据或对客户误导。

### 建议修改

- 对 `ContractIssue` 增加风险等级或在调用方定义高风险 code。
- 将 `forbidden_commitment`、`official_evidence`、`history_evidence` 视为高风险。
- 飞书链路遇到高风险 issue 时，不发送原始模型正文，改为发送安全兜底消息。
- 终端链路可展示原文，但必须明显标记“不建议复制给客服”。

### 验收标准

- 模型输出含“可以退款”等违规承诺时，飞书最终回复不包含原违规句子。
- 模型输出含未经允许的正式依据时，飞书最终回复使用兜底文本。
- 终端输出能清楚提示契约校验失败。

## Issue 5：优化飞书长回复截断策略以保留安全字段

**Labels**: safety, feishu, formatting, medium-priority

### 背景

`truncate_for_feishu()` 当前按字符数硬截断。由于答案格式中“正式依据”“历史参考”“工单草稿”等安全边界字段位于后半段，长回复可能截掉这些关键信息。

### 影响

客服可能只看到建议回复和排查步骤，却看不到“未查询到可信正式依据”“未审核历史参考”“需人工确认”等限制条件。

### 建议修改

- 调整 `src/agent_runtime/feishu/responder.py` 的截断策略。
- 截断后仍必须保留：
  - `正式依据`
  - `历史参考`
  - `工单草稿`
  - “未查询到可信正式依据”
  - “未审核历史参考 / 未审核媒体观察证据 / 需人工确认”
- 如飞书 API 支持，可考虑同线程分段回复。

### 验收标准

- 构造超过 `FEISHU_REPLY_MAX_CHARS` 的完整答案，截断后仍包含安全字段。
- 截断提示清楚说明内容被压缩或截断。
- 不破坏现有 SDK areply 的 `reply_in_thread=true` 行为。

## Issue 6：扩展售后承诺检测规则并补充同义表达测试

**Labels**: safety, answer-contract, tests, medium-priority

### 背景

当前 `FORBIDDEN_COMMITMENT_PATTERNS` 只覆盖固定短语，例如“可以退款”“直接换新”“可以补发”。模型可能输出“建议给客户换新”“安排补发配件”“同意退款处理”“可走赔付”等同义违规表达。

### 影响

答案契约校验可能漏掉实际售后承诺，无法有效防止错误承诺进入客服工作流。

### 建议修改

- 扩展 `src/agent_runtime/copilot/answer_contract.py` 的规则。
- 可引入正则，覆盖“建议/安排/同意/可走/给客户/为客户 + 退款/赔偿/赔付/换新/补发/维修时效”等表达。
- 保留否定上下文豁免，例如“不要承诺补发”“不能直接退款”。
- 增加表驱动测试，降低误报和漏报。

### 验收标准

- “建议给客户换新”触发 `forbidden_commitment`。
- “安排补发配件”触发 `forbidden_commitment`。
- “不要承诺补发”不触发误报。

## Issue 7：统一飞书运行日志中的敏感 ID 哈希化

**Labels**: privacy, observability, feishu, medium-priority

### 背景

飞书桥接日志中直接输出 `message_id`、`chat_id`、`thread_id`、`root_id`、`queue_key` 等原始标识；但 tracing metadata 已经使用哈希化字段，说明项目有减少敏感标识暴露的设计意图。

### 影响

线上日志采集、外部可观测平台或排障材料中可能暴露原始飞书会话标识，增加隐私与权限边界风险。

### 建议修改

- 在 `src/agent_runtime/feishu/bridge.py` 中新增统一 helper，例如 `_short_hash(value: str) -> str`。
- 日志改用 `chat_id_hash`、`thread_id_hash`、`message_id_hash`。
- 检查 `runtime_store.py`、`event_sources.py`、`long_connection.py` 中是否还有原始敏感 ID 日志。

### 验收标准

- 飞书运行日志不再打印原始 `chat_id`、`thread_id`、`message_id`。
- 空字符串与普通 ID 的哈希 helper 行为稳定。
- tracing metadata 保持兼容。

## Issue 8：让媒体 RAG 能稳定解析索引中的相对媒体文件路径

**Labels**: rag, media, reliability, medium-priority

### 背景

媒体索引构建脚本会写入 `media_file_path`。若构建时使用相对 `staging_dir`，运行时 `_chunk_media_file()` 直接按当前工作目录检查 `Path(path_text).exists()`，可能因 cwd 不同而找不到本地图片。

### 影响

已下载图片可能被误判为不存在，媒体 RAG 退化为文本 fallback，多模态 rerank 的召回和排序质量下降。

### 建议修改

- 在 `scripts/build_media_evidence_index.py` 中优先写入绝对路径，或同时写入相对索引根目录/`source_staging_dir` 的路径。
- 在 `src/agent_runtime/tools/media_rag.py` 中解析相对路径时依次尝试：
  - 项目根目录 `ROOT`
  - 媒体索引目录
  - manifest 中的 `source_staging_dir`
- 为 `_chunk_media_file()` 或 `search_media_rag()` 增加索引上下文参数。

### 验收标准

- JSONL 中保存相对路径时，从项目根目录以外 cwd 调用仍能找到图片。
- 找不到图片时仍走文本 fallback，不抛异常。
- 媒体 RAG 测试覆盖相对路径和绝对路径两种场景。

## Issue 9：清理 README 中的本机绝对路径链接

**Labels**: documentation, cleanup, low-priority

### 背景

`README.md` 的“关键参考”部分包含 `/Users/chenyu/Documents/...` 形式的本机绝对路径链接。

### 影响

仓库迁移到 Linux、CI、其他开发者机器或生产容器后，这些链接不可访问，也会让读者误以为需要本地私有路径。

### 建议修改

- 将 `docs/source-index.md`、`docs/obsidian-master-note.md` 链接改成仓库相对路径。
- 如果 `docs/obsidian-master-note.md` 是本地 Obsidian 软链接或非仓库文件，应明确说明部分环境可能不可用，并提供替代文档。
- 搜索 README 和 `docs/` 中的 `/Users/chenyu/`、`agent_runtime(test)` 等本机路径并统一替换。

### 验收标准

- README 不再包含本机绝对路径。
- 仓库内文档链接在仓库根目录下可点击或有明确不可用说明。
- 文档自检通过。
