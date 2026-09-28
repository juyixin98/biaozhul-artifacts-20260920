# 设计说明（design）

## 1. 目标与边界

服务回答三类问题：

1. **排序（sorted）**：给定 locale、strength、numeric、case_first，字符串的确定顺序；
2. **值范围（range）**：落在两个边界字符串之间的全部串；
3. **前缀检索（prefix）**：以某串为「校对前缀」或「文本前缀」的全部串。

排序事实的唯一来源是成熟库 **ICU**（PyICU 74.2 / Unicode 15.1）。系统不自己
发明任何顺序。所有数据来自本地合成夹具，无网络、无生产账号。

## 2. 模块与职责

```
corpus/spec.py      语料规范（校验：doc_id 唯一、text 必须是 str、name 合法）
corpus/loader.py    夹具加载（单文件 / bundle）
mining/miner.py     挖掘内核：NFC/NFD、码点清单、规范等价类（不并身份）、
                    重复原文组、分类探针（accent/numeric/case/turkish/canonical）
collation/options.py   不可变选项值对象（frozen dataclass）
collation/engine.py    PyICU 封装：排序键、比较、规则指纹、收缩探测、前缀谓词
collation/versioning.py  索引版本：schema 代际 + 规则指纹的内容哈希
index/schema.py     SQLite DDL（原文、NFC、完整排序键、primary 段、seq）
index/store.py      建/重建、版本门禁、排序键 BLOB 的范围/分页/seek/全扫原语
index/cursor.py     不透明、版本绑定、带模式标签的翻页游标
query/keys.py       排序键分段事实与 primary 上界
query/validator.py  独立 oracle（自建 ICU collator + 纯 Python 重算）
query/service.py    三类查询 + 版本门禁 + 游标 + 降级标注
api/                FastAPI 装配、错误分类、trace 渲染与日志
```

语料规范、挖掘内核、索引与模型、查询验证彼此独立；参考答案（oracle）与
golden 夹具都不经过被测的索引/SQL/服务代码。

## 3. 关键不变量

1. **排序键与原文同时保存**：`entries.text`（原始拼写，不改写）、`nfc_text`、
   `sort_key`（完整 ICU 键，BLOB）、`primary_key`（primary 段）、`seq`（输入序）。
2. **选项绑定版本**：strength / numeric / case_first / locale，连同 ICU 版本、
   Unicode 版本、实际 locale、tailoring 规则哈希，进入
   `IndexVersion = idx_v{schema}_{16 hex}`。任何一项变化 → 版本号变化。
3. **升级必须重建，旧游标不可混入**：
   - 打开库即比对存储版本与当前选项派生版本，不符抛 `INDEX_VERSION_CONFLICT`；
   - 游标内嵌版本号与查询模式，恢复时二次校验。
4. **范围边界用排序键**：`WHERE sort_key >= key(low) AND sort_key <= key(high)`，
   SQLite 对 BLOB 的字节序与 ICU 排序键序一致；绝不使用 UTF-8 字节序。
   `étude` 的 UTF-8 字节在 `f` 之后，但它在排序上属于 `[e, f]`——测试固化此差异。
5. **规范等价不丢身份**：NFC/NFD 排序键相同，但作为不同 `doc_id` 行返回；
   等键并列用 `seq` 稳定决胜。

## 4. 前缀检索：为什么需要「收窄 + 过滤」，以及何时降级

ICU 二进制排序键按 level 分段：
`<primary> 0x01 <secondary> 0x01 <tertiary> 0x00`。实测两个非平凡事实：

- 只有 **primary 段**是逐字符权重的追加；secondary/tertiary 用的是**按位置
  计数**的 common weight（`05,06,07…`），不能对这些段做字节 startswith。
- **primary 段区间**（`primary(prefix)` … `primary(prefix)+FFFF`）能在
  「非数字排序、非 IDENTICAL、且 locale 无收缩规则」时，覆盖所有前缀候选，
  这一点由数万次固定种子随机对照验证（`test_collation_properties.py`）。

因此前缀查询：

1. 用 primary 段在 `idx_entries_primary_key` 上做**索引收窄**；
2. 用**库级精确谓词**过滤：两侧先 NFC，取候选前 `len(NFC(prefix))` 个码点，
   `collator.compare(prefix, head) == 0`（collation 模式）或 NFC startswith
   （text 模式）；
3. 结果仍按完整排序键 + seq 排序返回。

当 primary 单键区间**无法保证召回**时，自动改走全表有序扫描，并在
`trace.uncertainties` 单列原因（结论仍由库谓词保证正确，仅非索引 seek）：

| 配置 | 原因 | 如何检测 |
|------|------|----------|
| numeric=on | 数字 run 合并为单个权重（`2`→`22` 落到区间外） | 选项 |
| strength=15 IDENTICAL | 组合记号/NFC 边界权重越过简单上界 | 选项 |
| da `aa`→å、cs `ch` 等收缩 | primary 权重不再逐字符追加 | 引擎构建期对 26×26 拉丁字母对经验探测 |

## 5. 独立验证（参考答案不是被测核心自产）

三层独立参照：

- `scripts/probe_golden.py`：**只 import icu**，录制逐字节排序键与语序；
- `tests/fixtures/golden.py`：冻结的字面期望；
- `query/validator.py::IndependentOracle`：在测试里**新建** ICU collator、
  用纯 Python 从显式 `(doc_id,text,seq)` 列表重算，绝不复用 store/引擎实例/SQL。

被测实现（存储、BLOB 序、seek 区间、过滤、分页）与 oracle 逐项比对。
测试断言**具体顺序、具体键字节、具体失败类别与原因**，而非「接口可调用」。

## 6. 稳定性与分页

- 并列键（如重复原文、NFC/NFD、primary 强度下 a/A）用建库输入序 `seq` 决胜，
  `ORDER BY sort_key, seq` 保证全序与稳定；
- 分页为 keyset：游标携带 `(last_sort_key, last_seq, mode, index_version)`，
  恢复条件 `(sort_key,seq) > (last_key,last_seq)`，无偏移、无重复、无缺漏
  （有跨 7 条页大小、覆盖全 35 行的无缺口测试）。

## 7. 可解释性

每个响应与每行日志共享：

- `request_id`：关联一次请求；
- `index_version`：所用规则版本；
- `steps[]`：有序步骤、处理位置（如 `query/service.range_between`）、关键入参
  （如 low/high 的键十六进制、seek/scan 策略、scanned/matched）；
- `failures[]`：分类化失败原因；
- `uncertainties[]`：降级等「确定但需说明」的结论，单独列出。

## 8. 升级流程

ICU/Unicode 升级或任一选项变更 → 规则指纹变化 → 版本号变化：

1. 旧库查询立即得到 `INDEX_VERSION_CONFLICT(409)`，不会静默混用键；
2. `POST /admin/index/build {replace:true}`（或 `scripts/reindex.py`）重建；
3. 旧游标在恢复时被拒（409），客户端以新版本重新发起查询。
