# 资料索引

本文只记录 MVP 设计中用到的重要上游笔记和飞书资源。实现代码仍应以 `docs/` 下的具体设计文档为准。

## 主要设计笔记

- Obsidian 总笔记：[obsidian-master-note.md](</Users/chenyu/Documents/workplace/agent_runtime(test)/docs/obsidian-master-note.md>)
- 作用：项目设计、架构和阶段决策的动态总笔记。该文件是软链接；如果 Obsidian 笔记改名或移动，只需要更新软链接目标，不要在文档中写死 Obsidian 绝对路径。

## 飞书资源

### 售后事件原型库

- 名称：`售后问题 AI 闭环库`
- URL：https://ulanzichina.feishu.cn/base/JDWwbG7rRaeoZksPe1TchVyWnif
- 本地说明：[base-demo-reference.md](</Users/chenyu/Documents/workplace/agent_runtime(test)/docs/base-demo-reference.md>)
- 作用：原始事件采集、原消息追溯、媒体归档和动作日志参考。
- 最近采集：2026-05-26 运行 `data/feishu_ingest/20260526-014329`，采集目标售后群和鲁工 P2P 近 30 天消息，并把去重后的原始消息、事件候选、媒体记录和动作日志写入该 Base。
- 图片归档文件夹：https://ulanzichina.feishu.cn/drive/folder/HRdOft9QHlXeCSdAh9hcSMXlnsc

### 飞书聊天来源

- 群聊：`产品质量问题 发错货 售后及其他产品反馈`
- 群聊 ID：`oc_3fcba1adffbef12147b6258a341b5328`
- P2P 来源：`鲁志强（鲁工 / Luz）`
- 鲁工 P2P 聊天 ID：`oc_19c413416db380bd40659181709b766d`
- 范围：售后支持、产品质量、发错货、处理动作和项目实现上下文。
- 边界：视频和普通文件保留资源 key 以便追溯；2026-05-26 采集已将图片同步为飞书云盘链接，因为当前表的 Base 原生附件上传受移动端限制。

### 品质跟进 Base

- 名称：`2026年品质团队跟进事项清单`
- URL：https://ulanzichina.feishu.cn/base/EI20b8wd4af3NEsY3YHc6dsXnOh
- 本地说明：[quality-followup-base.md](</Users/chenyu/Documents/workplace/agent_runtime(test)/docs/quality-followup-base.md>)
- 相关聊天说明：[luz-chat-reference.md](</Users/chenyu/Documents/workplace/agent_runtime(test)/docs/luz-chat-reference.md>)
- 作用：已审核品质跟进分类体系和长期记录目标表。

### 售前参考表

- 名称：`2026AI配置汇总表`
- URL：https://ulanzichina.feishu.cn/wiki/YESIwpMshilwlikWx3scWeNtnRf
- 本地说明：[presales-knowledge-reference.md](</Users/chenyu/Documents/workplace/agent_runtime(test)/docs/presales-knowledge-reference.md>)
- 作用：仅作为售前补充资料。必须与售后诊断和处理决策隔离。

### 会议纪要

- 妙记 URL：https://ulanzichina.feishu.cn/minutes/obcntp46iby5ok73q79djjag
- 状态：访问还需要飞书妙记权限/admin approval。
- 作用：待读取后补充最新项目决策。

## 调试资源

### OpenAI Traces

- 仪表盘：https://platform.openai.com/logs?api=traces
- 本地说明：[tracing.md](</Users/chenyu/Documents/workplace/agent_runtime(test)/docs/tracing.md>)
- 作用：查看 OpenAI Agents SDK trace、运行分组和工具调用。

## 本地 MVP 数据

- SKU 支持目录：[docs/sku-support-catalog.md](</Users/chenyu/Documents/workplace/agent_runtime(test)/docs/sku-support-catalog.md>)
- 当前生成的 SKU CSV：`data/sku_catalog/processed/2026-05-26/sku_support_catalog-2026-05-26.csv`
- SKU 目录作用：为 Agent 提供 SKU/SPU/产品负责人识别，用于售后流转；不是故障排查或政策知识库。
- 本地演示种子知识已经从运行时移除。正式知识库和历史案例必须来自经过审核的检索源，才能作为答案依据。

## 边界规则

- 售后正式文档、已审核品质记录和历史售后 QA 是核心知识来源。
- 售前资料必须放在独立检索命名空间：`presales_reference`。
- 售前资料可以支持 SKU、基础功能和发票上下文，但不得驱动故障诊断、质量归因、换新、维修、退款或赔偿决策。
