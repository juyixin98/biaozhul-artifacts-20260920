# 语料规范 (Corpus Specification)

本文件定义实体解析后端接受的数据形态、字段语义与校验规则。它与
`entity_resolution/models.py` 中的 pydantic 契约一一对应；后者是运行时的
唯一事实来源，本文件是其可读说明。

## 顶层结构 `CorpusIn`

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `records` | `RecordIn[]` | 是（≥1） | 待解析的机构名称记录 |
| `must_links` | `[id, id][]` | 否 | 强制同实体的硬约束 |
| `cannot_links` | `[id, id][]` | 否 | 强制不同实体的硬约束 |
| `aliases` | `{canonical: string[]}` | 否 | 规范名 → 别名/异拼列表 |

- 额外字段一律拒绝（`extra="forbid"`），避免静默吞掉拼错的键。
- `must_links`/`cannot_links` 中的 id 必须出现在 `records` 中；自指 `(x,x)`
  属输入错误；同对同时出现两种约束属状态冲突。

## 记录 `RecordIn`

| 字段 | 类型 | 约束 | 语义 |
|------|------|------|------|
| `id` | string | 非空，≤200，语料内唯一 | 主键 |
| `name` | string | 非空（去空白后），≤500 | 机构显示名称 |
| `language` | string | ≤16，可空 | 名称语言提示（如 `en`/`ru`） |
| `attributes` | `map<string,string>` | 可空 | 结构化身份/属性信号 |

### 名称 `name`

- 名称**只是文本证据**，不是身份。同名记录仍可能因硬属性冲突或
  `cannot-link` 而被拆为不同实体。
- 规范化（NFKC、大小写/标点/空白折叠、法律后缀整词剥离、西里尔转写、
  少量 CJK 异体映射）只用于比较，不改写原始 `name`。

### 属性 `attributes`

- 普通属性相等会作为加分证据，冲突会被记录在证据里。
- 被配置为**硬属性**的键（如 `reg_id`）在两侧都出现且取值不同时，对该对
  产生**否决**：分数归零、永不为候选，无论名称多相似。
- 仅一侧出现的硬属性不构成否决（避免缺失值误伤）。

## 别名 `aliases`

- 键是一个规范拼写，值是与其指向同一名称组的其他拼写
  （跨语言、缩写、全角/半角等）。
- 别名只用于在相似度层"搭桥"（提升证据），**不直接声明同实体**；最终
  是否合并仍由全局目标与硬约束决定。
- 同一别名被映射到两个不同规范名会被拒绝（输入错误）。

## 约束语义与冲突类别

约束在任何聚类之前整体校验（并查集求 must-link 闭包）：

| 情况 | 类别 | `details.reason` |
|------|------|------------------|
| 引用不存在的 id / 自指 | `INPUT_ERROR` | `unknown_record` / `self_link` |
| 同对既 must 又 cannot | `STATE_CONFLICT` | `direct_contradiction` |
| must 闭包与 cannot 冲突（A~B,B~C 但 A≄C） | `STATE_CONFLICT` | `must_link_closure` |
| cannot 落在同一锁定簇内 | `STATE_CONFLICT` | `inside_locked_cluster` |
| must 跨越两个已锁定簇 | `STATE_CONFLICT` | `cross_locked_must` |

## 示例

见 `fixtures/corpus.example.json`，其中同时包含：
相似链（Pioneer Corp / Corporation / Foods）、跨语言别名桥
（Gazprom / Газпром）、同名不同硬 id（Acme Bank，`reg_id` 冲突）以及
配套的 `cannot_links`。
