# SKU 支持目录

本文定义完整 SKU 导出文件如何暂存、清洗和合并，以便售后场景做 SKU 匹配。

## 文件布局

原始完整导出放在：

```text
data/sku_catalog/raw/<date>/
```

派生文件放在：

```text
data/sku_catalog/processed/<date>/
data/sku_catalog/reports/<date>/
```

`data/sku_catalog/` 目录被 git 忽略，因为其中包含公司的完整 SKU 目录。

## 当前来源文件

2026-05-26 原始文件：

- `data/sku_catalog/raw/2026-05-26/产品信息表-2026-05-26.csv`
- `data/sku_catalog/raw/2026-05-26/ODS-ERP-PLM产品sku表-2026-05-26.csv`

构建命令：

```bash
PYTHONPATH=src .venv/bin/python scripts/build_sku_support_catalog.py
```

## 合并规则

```text
ODS-ERP-PLM产品sku表.产品表id = 产品信息表.主键ID
```

2026-05-26 导出可以完整合并：

- `产品信息表`：17995 行。
- `ODS-ERP-PLM产品sku表`：18883 行。
- 有产品元数据匹配的源行：18883 行。
- 过滤组合 SKU 后的 SKU 行：11733 行。
- 过滤无效表格错误 SKU 行：1 行。
- 有效 SKU 过滤后的 SKU 行：11732 行。
- 过滤源中重复 SKU：245 个 code / 525 行。
- 按最新状态去重移除源行：280 行。
- 源去重并合并后的输出行：11452 行。
- 被过滤的组合 SKU 行：7150 行。
- 未匹配 SKU 行：0 行。
- 最终输出中的重复 SKU code：0 个。

组合 SKU 过滤规则：

- 如果 SKU 包含 `+`、`/`、`，`、`、`、空白等组合分隔符，或包含 `*` 等数量标记，则删除该行。
- 删除示例：`2742+2746`、`3142+T00A4301+X025`、`2744+2742*2+2747+2753`。
- `#REF!` 等表格错误值会作为无效 SKU 删除。
- SPU 重复是预期情况，不视为问题。

SKU 源去重规则：

- 去重发生在 SKU 源表合并产品元数据之前。
- 分组键：标准化后的 `sku`，统一大写。
- 保留规则：最新 `修改时间`，再看最新 `创建时间`，再看最大 `主键id`。
- 该规则有意保留最新源状态，即使最新行是已删除或停用；旧的启用行不能覆盖更新状态。
- 重复诊断仍写入 `data/sku_catalog/reports/<date>/duplicate_sku_diff_groups-<date>.csv` 和 `duplicate_sku_diff_summary-<date>.json`，包括保留的 `主键id` 和被删除的旧记录 ID。

## 字段含义

`ODS-ERP-PLM产品sku表` 是 SKU 粒度表。可用字段：

- `sku`：客户/客服可见的 SKU 或套装码，是首要精确匹配键。
- `品名`：SKU 级中文名称。
- `产品表id`：指向产品/SPU 元数据的外键。
- `修改时间`、`创建时间`、`主键id`：仅用于同一 SKU 多行时选择最新行，不输出到最终表。

`产品信息表` 是产品/SPU 元数据表。可用字段：

- `主键ID`：产品主键，由 `产品表id` 关联。
- `spu`：产品/SPU code，可作为更宽的 fallback 匹配。
- `产品名`：产品级中文名称。
- `产品负责人`：人工升级和流转信号。

## 合并表结构

生成目录是 SKU 粒度，一行对应一个 SKU 源表记录：

```text
data/sku_catalog/processed/2026-05-26/sku_support_catalog-2026-05-26.csv
```

输出有意保持窄表，只包含 5 列。每个输出字段都继承自一个源字段。清洗仅限于空白归一、常见占位值转空，以及 `sku_code` 大写。

| 输出列 | 来源字段 | 含义 |
| --- | --- | --- |
| `sku_code` | `ODS-ERP-PLM产品sku表.sku` | SKU code，统一大写 |
| `spu` | `产品信息表.spu` | 产品/SPU code |
| `sku_name_cn` | `ODS-ERP-PLM产品sku表.品名` | SKU 级中文名称 |
| `product_name_cn` | `产品信息表.产品名` | 产品级中文名称 |
| `product_owner_name` | `产品信息表.产品负责人` | 产品负责人 |

已从输出中移除：

- 系统 ID：`sku_record_id`、`product_id`、owner IDs、team IDs。
- 低价值运营字段：单位、上市时间、量产时间、仓库位置。
- ERP 字段：金蝶 ID 和同步状态。
- 原始生命周期 code 和重复标签。
- 高度一致的字段，例如 `是否变更`。
- 高空值图片字段：`sku图片`、`产品示意图url`。
- 低优先级英文/描述字段：`品名（英文）`、`产品名称（英文）`、`产品功能描述`、`产品卖点`、`产品用途`、`主要材质`。
- SKU 级负责人字段：`产品负责人姓名`；保留产品级负责人作为流转字段。
- `sku_variant`：稀疏且对支持匹配价值不足。
- `sku_component_tokens`：组合 SKU 行已完全过滤，不再需要组件 token。
- 派生检索/排序字段：`support_match_priority`、`support_match_status`、`support_match_text`。

## 支持匹配建议

该合并表应用作 SKU 识别来源，不应用作故障排查知识库。

当前运行时接入：

- `SKU_CATALOG_PATH` 指向生成的 CSV。
- `VIJIM-after-sale-copilot` 暴露 `search_sku_catalog` 作为 Agents SDK 工具。
- 工具用于识别 SKU、SPU、SKU 名、产品名和产品负责人，以便流转/升级。
- 工具不得作为正式故障依据、政策依据、保修逻辑或维修指导。

推荐排序：

1. 精确匹配 `sku_code`。
2. fallback 匹配 `spu`。
3. 模糊匹配 `sku_name_cn`、`product_name_cn` 和英文名。

如果后续需要检索专用文本字段，应在搜索/索引层由继承字段构建，不要写回源目录。
