# 最小披露记录导出（Minimum-Disclosure Record Export, MDE）

纯后端的本地安全数据处理服务：按**用途（purpose）**对记录做字段级导出，
每个字段被**允许 / 拒绝 / 泛化**，并输出**逐字段、可离线核验的决策记录**。

> **本系统不是匿名化工具，也不提供任何匿名化或抗再识别保证。**
> 输出仍是个人数据，可能被关联再识别。每个导出包都内嵌这一声明
> （`disclaimers`）。假名化（HMAC-SHA256）只是目的绑定的稳定假名，
> 不是加密身份隐藏，也不抗跨数据集关联攻击。

---

## 1. 它解决什么问题

导出数据给某个用途（如 `analytics`、`support`、`fraud_review`）时，
需要可审计地回答："这条记录里的每个字段，为什么被导出、被丢弃、或被泛化？"

本服务提供：

1. **字段级策略引擎**：显式 `allow` / `deny` / `generalize`，用途默认拒绝。
2. **策略版本固定到任务**：策略发布即不可变（SHA-256 指纹 + Ed25519 签名链路），
   导出任务显式固定到某个指纹；策略更新不影响进行中或已完成的任务（无 TOCTOU 竞争）。
3. **嵌套结构不可旁路**：
   - 路径段对 `. [ ] %` 做百分号编码 —— 记录里的字面键 `"user.id"`
     编码为 `user%2eid`，**不可能**冒充嵌套路径 `user.id`；
   - 数组通配 `[]` 只匹配列表元素，把数组换成对象（或反之）会导致规则不命中、回落默认拒绝；
   - 同一用途内规则互不为前缀（发布时校验），不存在"父允许、子拒绝"的歧义组合；
   - 别名只能指向**已存在的精确规则字段**，不能借别名提权，别名决策全程留痕。
4. **可核验决策记录**：每个叶子字段一条决策（动作、命中规则、原因、
   输入/输出摘要）；容器有结构性决策。导出包自描述、带 Ed25519 签名，
   持公钥即可离线复验；持原始记录和本地密钥时可端到端重放，逐字节比对。
5. **成熟密码原语**：仅使用 `cryptography` 库的 Ed25519、HMAC-SHA256、HKDF；
   **不自创加密算法**。密钥全部本地生成（文件权限 0600），不接任何生产账号/KMS。

无前端、无网络鉴权、无数据库。

---

## 2. 目录结构

```
src/mde/
  paths.py       # 点分路径解析/匹配、百分号编码、数组通配
  transforms.py  # 泛化原语：email_domain/date_trunc/bucket_number/mask_tail/pseudonymize
  policy.py      # 策略校验/编译视图、不可变策略仓库（指纹、只增索引）
  engine.py      # 字段级策略引擎：递归求值 + 逐字段决策记录
  keys.py        # 本地测试密钥（Ed25519 + 32B transform secret，0600）
  signing.py     # canonical JSON、SHA-256 指纹、Ed25519 签名/验签
  exporter.py    # 导出任务、导出包组装、离线复验（签名/摘要/一致性/重放）
  api.py         # 127.0.0.1 本地 HTTP API（标准库）
  cli.py         # 命令行：publish / export / verify / serve
examples/
  policy.json          # 示例策略 v1（analytics/support/fraud_review + 别名）
  policy-v2.json       # 同一 policy_id 的修订版（演示版本固定）
  records.json         # 含别名注入、字面点号键、未知字段、嵌套/数组的攻击样例
  requests/            # HTTP 请求/响应样例（可直接查看）
  curl-demo.sh         # 一键端到端 curl 演示
tests/                 # 46 个自动化测试
RUNLOG.md              # 实际运行命令、结果与已知限制（如实记录）
```

---

## 3. 安装与运行

要求 Python ≥ 3.10。仅一个第三方依赖：

```bash
pip install -r requirements.txt          # cryptography（开发机已装 41.0.7）
# 或：pip install -e '.[test]'
```

所有数据默认落在 `--data-dir`（默认 `.mde-data`）；首次使用自动在
`<data-dir>/keys/test-keys.json` 生成 0600 权限的本地测试密钥。

### 3.1 命令行

```bash
# 1) 发布策略（返回不可变指纹）
PYTHONPATH=src python3 -m mde.cli publish --policy examples/policy.json

# 2) 按用途 + 固定指纹导出
PYTHONPATH=src python3 -m mde.cli export \
    --records examples/records.json \
    --purpose analytics \
    --policy-fingerprint <上一步的指纹> \
    -o /tmp/pkg.json

# 3) 公开复验（只需包；用包内嵌公钥时仅检测意外损坏，不能防伪造——报告里会警告）
PYTHONPATH=src python3 -m mde.cli verify --package /tmp/pkg.json

# 4) 端到端复验（本地持有原始记录 + transform secret：重算输入摘要并重放引擎）
PYTHONPATH=src python3 -m mde.cli verify \
    --package /tmp/pkg.json --records examples/records.json --local-keys
```

### 3.2 本地 HTTP 服务（仅回环）

```bash
PYTHONPATH=src python3 -m mde.cli serve --host 127.0.0.1 --port 8390
# 或一键演示：
bash examples/curl-demo.sh
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + key_id |
| POST | `/policies` | 发布策略（校验、落盘、返回指纹；幂等） |
| GET | `/policies/{policy_id}` | 列出该策略所有已发布 revision 与指纹 |
| GET | `/policies/fingerprint/{fp}` | 读取某个不可变策略快照 |
| POST | `/exports` | `{purpose, policy_fingerprint, records, ...}` → 签名导出包 |
| POST | `/verify` | `{package, records?, use_local_keys?}` → 结构化复验报告 |

请求/响应完整样例见 `examples/requests/`（`*.http` 为端点目录，
JSON 文件是真实跑出来的请求体/包）。

**服务不做鉴权、只绑定 127.0.0.1，请勿暴露到网络。**

---

## 4. 策略语言

```json
{
  "version": "mde/policy@v1",
  "policy_id": "customer-demo",
  "revision": 1,
  "purposes": {
    "analytics": {
      "default": "deny",
      "allow":      ["id", "orders[].id", "tags[]"],
      "deny":       ["ssn", "security"],
      "generalize": {
        "email": {"transform": "email_domain", "params": {}},
        "orders[].placed_at": {"transform": "date_trunc", "params": {"unit": "day"}}
      }
    }
  },
  "aliases": {"e_mail": "email"}
}
```

- **路径**：点分键；`[]` 数组通配（`orders[].amount`）。键里的 `.[]%` 按百分号
  编码（`.` → `%2e`），所以字面键与嵌套路径在语法上不可能冲突。
- **default**：`allow` | `deny`（建议始终用 `deny`）。未命中任何规则的字段按此处理。
- **规则互不为前缀**：`profile` 与 `profile.name` 不能同时出现在**同一用途**的
  任意动作列表中 —— 发布即拒绝。这样消除了继承歧义；要对子树统一处置时，
  策略作者必须显式列出子树（子树允许/拒绝会整体作用于后代叶子）。
- **别名**：`源路径 -> 已存在的精确规则路径`。
  - 别名源不得与真实规则字段冲突（不能遮蔽字段）；
  - 别名目标必须是某用途里已配置的规则（不能指向空气、不能形成链）；
  - 别名只是"输入里另一个名字的同一字段"：其值按目标规则处理，
    值保留在源路径，决策记录用 `via_alias` + `rule_path` 标明。
- **动作的类型约束**：`generalize` 只能作用于标量叶子。若泛化规则命中了对象/
  数组容器（形状不符），该容器被移除并产生带警告的拒绝决策；泛化函数运行失败
  （如邮箱不合法）同样移除并记录 `transform_failed`，绝不放行原值。

### 泛化原语（全部确定性、发布时校验参数）

| 原语 | 参数 | 行为 |
|---|---|---|
| `email_domain` | — | `a@Example.com` → `example.com`；非法邮箱拒绝该值 |
| `date_trunc` | `unit: year\|month\|day` | ISO 日期截断；非法日期拒绝 |
| `bucket_number` | `width>0`, `offset:int` | `37,w10,o0` → `[30,40)`；bool 拒绝 |
| `mask_tail` | `keep_prefix≥0`, `mask:单字符` | 固定 8 位掩码长度（不泄露原值长度） |
| `pseudonymize` | `scope: purpose\|global` | HMAC-SHA256 假名；密钥经 HKDF 按**用途 + 策略指纹**派生 |

假名密钥派生（`transforms.py`）固定 salt/info，可跨进程复算：

```
PRK = HMAC-SHA256("mde-purpose-bound-v1", transform_secret)
key = HMAC-SHA256(PRK, "mde/pseudonymize/v1/purpose=<p>/policy=<fingerprint>" || 0x01)
```

同用途+同策略下稳定（可复验）；换用途或换策略版本即得到不同假名。
**这是假名，不是匿名化：持有密钥方可还原映射，且不抗背景知识关联。**

---

## 5. 决策记录与导出包

导出包（`mde/export-package@v1`）结构：

```
task            # task_id、purpose、policy_id/revision/fingerprint、key_id、时间
manifest        # 记录数、决策数、records/output/decisions 三个 SHA-256 摘要
policy_snapshot # 当次任务实际使用的完整不可变策略（内嵌，离线可复验）
output[]        # 最小披露输出
decisions[]     # 逐字段决策记录
disclaimers[]   # 明确的"非匿名化"等限定声明
signature       # 对上述整个 body 的 Ed25519 签名（含公钥 SPKI 与 key_id）
```

每条决策（叶子）示例：

```json
{
  "schema": "mde/decision-record@v1",
  "record_index": 0,
  "path": "orders[0].placed_at",          // 实例路径（具体下标）
  "shape_path": "orders[].placed_at",     // 策略形状路径
  "structural": false,
  "value_type": "string",
  "action": "generalize",                 // 规则声明的动作
  "decision": "generalize",               // 实际裁决：allow/deny/generalize
  "reason": "exact_rule",
  "via_alias": null,
  "rule_path": "orders[].placed_at",
  "input_digest": "sha256(canonical(原值))",
  "output_digest": "sha256(canonical(泛化值))"
}
```

拒绝原因码（`reason`）包括：`exact_rule`、`denied_by_ancestor_rule`、
`default_deny/default_allow`、`transform_failed`、
`generalize_rule_requires_scalar`、`empty_unknown_container_denied`、
`container_dropped_no_surviving_fields` 等。

被祖先子树整体拒绝的容器只产生**一条结构性拒绝决策**（不逐叶子枚举，
避免决策记录本身泄露被拒子树的内部形状）；而默认拒绝的未知子树会逐叶记录。

### 复验项（`POST /verify`）

1. 包结构完整性
2. 内嵌策略快照重算指纹 == 任务声明指纹（防快照被替换）
3. Ed25519 签名（**应传入带外获得的验签公钥**；缺省时退回包内嵌公钥，
   只能检测意外损坏、不能识别伪造包，报告会显式警告）
4. `output` / `decisions` 摘要与清单一致
5. 决策↔输出逐字段一致：`allow`/`generalize` 的值必须在输出中且摘要可重算；
   `deny` 的精确路径在输出中必须不存在
6. （可选，提供原始记录时）记录摘要一致
7. （可选，提供 records + 本地密钥时）用包内快照重放引擎，
   输出与决策逐字节一致

---

## 6. 验收点与测试对应

| 验收要求 | 覆盖测试 |
|---|---|
| 别名构造不能提权、决策可审计 | `test_alias_decisions_are_auditable`、`test_alias_chain_and_unknown_target_rejected` |
| 未知字段默认拒绝（含深层嵌套） | `test_unknown_fields_denied_by_default`、`test_deeply_nested_unknown_subtree_fully_denied` |
| 数组与形状混淆（数组↔对象）不旁路 | `test_shape_confusion_does_not_match`、`test_empty_arrays_policy_known_vs_unknown` |
| 字面点号键不能冒充嵌套路径 | `test_literal_dotted_key_cannot_bypass`、`test_literal_dotted_key_is_distinct_from_nested_path` |
| 嵌套子树不能绕过（祖先拒绝/允许） | `test_every_leaf_has_a_decision`、`test_purpose_isolation` |
| 策略更新竞争 / 版本固定 | `test_policy_update_race_does_not_affect_pinned_task`、`test_pinned_fingerprint_revision_conflict_rejected`、`test_policies_on_disk_are_immutable` |
| 可核验字段决策记录 | `test_decision_records_cover_every_field_and_are_verifiable`、`test_end_to_end_replay_with_records` |
| 篡改/伪造被检出 | `test_tamper_with_output_is_detected`、`test_forged_signature_detected`、`test_sign_verify_positive_and_negative` |
| 不声称匿名化 | 包内 `disclaimers` 断言 `test_export_package_structure_and_pin`；本 README |
| 密码原语成熟、密钥本地生成 | `tests/test_transforms.py`、`tests/test_keys_signing.py`（仅 Ed25519/HMAC/HKDF） |

运行：

```bash
python3 -m pytest tests/          # 46 passed（见 RUNLOG.md）
```

---

## 7. 明确的安全边界（务必阅读）

- **不是匿名化**。这是字段级最小披露 + 审计；唯一性、准标识符组合、
  背景知识关联等风险未处理。
- **目的绑定是策略层属性，不是密码学强制**。拿到输出和 transform_secret 的人
  可跨用途重算假名；`purpose` 作用域降低意外关联，不构成访问控制。
- **决策记录本身是敏感数据**：包含字段存在性、类型与输入摘要，应与原始数据
  同等保护；原始记录**不随包分发**（包里只有其摘要）。
- **签名信任根在带外公钥**。包内嵌公钥仅为便利，不能作为真实性依据。
- 本地测试密钥明文存于 0600 文件，**禁止用于生产**；无速率限制、无鉴权、无 TLS。
- 泛化函数可能因输入不合法而拒绝字段（fail-closed），不会静默放行明文。
