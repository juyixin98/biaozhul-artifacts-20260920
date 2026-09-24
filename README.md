# 离线策略求值解释器（Offline Policy Evaluation Interpreter）

纯后端的**资源访问策略解释器**：策略以声明式 AST（JSON）描述，解释器在本地对
**主体属性 / 资源属性**做求值，支持集合包含、逻辑组合、显式拒绝优先
（deny-overrides）。缺失属性产生**未知（unknown）**三值结果并按拒绝处理。
**绝不执行策略中携带的任何代码**；生产模式下策略必须带 Ed25519 离线签名，
信任锚（公钥）通过带外目录预置，整个验签过程无网络、无 CA、无动态代码。

技术栈：Python 3.12 · FastAPI · Pydantic v2 · cryptography（Ed25519）· pytest。

---

## 1. 目录结构

```
.
├── app/                    # 应用代码
│   ├── errors.py           # 错误类型 -> HTTP 状态码/错误码
│   ├── logic.py            # Kleene 三值逻辑（true/false/unknown）
│   ├── interpreter.py      # AST 白名单解释器（无 eval/exec）
│   ├── model.py            # 规则/策略文档模型与静态校验
│   ├── evaluator.py        # deny-overrides 决策、最小相关规则集、顺序无关证明
│   ├── truth_table.py      # 组合枚举真值表
│   ├── signing.py          # Ed25519 规范化签名 / TrustStore 验签
│   ├── schemas.py          # 请求信封（Pydantic，禁止未知字段）
│   └── main.py             # FastAPI 路由、错误信封、请求体大小限制
├── tools/
│   ├── gen_keys.py         # 生成 Ed25519 密钥对
│   └── sign_policy.py      # 给策略文档签名，产出签名包
├── examples/
│   ├── policy.json             # 示例单策略（5 条规则）
│   ├── policy_set.json         # 示例多策略（RBAC + 地域）
│   ├── policy.signed.json      # 上述策略的签名包
│   ├── policy_set.signed.json
│   ├── request_*.json          # 4 个请求样例（主体/资源属性）
│   └── keys/
│       ├── demo.pem           # 演示私钥（仅演示，生产禁止入库/泄露）
│       └── demo.pub.pem       # 演示公钥（信任锚）
├── tests/                  # 75 个 pytest 用例
├── requirements.in         # 直接依赖（带版本区间）
├── requirements-dev.in     # 测试依赖
├── requirements.lock       # 锁定依赖（pip freeze 全量版本）
└── pytest.ini
```

---

## 2. 安装与启动

需要 Python ≥ 3.10（开发与验证使用 3.12.3）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock        # 或 pip install -r requirements.in

# 生成/复用签名密钥（examples/keys 已含一对演示密钥）
python -m tools.gen_keys --out examples/keys --name demo
# 对策略签名
python -m tools.sign_policy --key examples/keys/demo.pem \
    --document examples/policy.json --out examples/policy.signed.json

# 生产模式启动：只接受签名包，信任 examples/keys 下的 *.pub.pem
POLICY_ALLOW_UNSIGNED=0 POLICY_TRUST_DIR=examples/keys \
    uvicorn app.main:app --host 127.0.0.1 --port 8099

# 本地联调可显式允许内联未签名策略：
POLICY_ALLOW_UNSIGNED=1 uvicorn app.main:app --port 8099
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `POLICY_TRUST_DIR` | `examples/keys` | 信任公钥目录，仅加载其中的 `*.pub.pem` |
| `POLICY_ALLOW_UNSIGNED` | `0`（pytest 下为 `1`） | `1` 才允许请求体内联未签名策略 |

---

## 3. 策略语言（AST 参考）

条件是一棵 JSON 表达式树，**只能从固定白名单选算子**：

| 算子 | 形态 | 语义 |
|---|---|---|
| `lit` | `{"op":"lit","value":…}` | JSON 标量/标量列表常量 |
| `attr` | `{"op":"attr","bag":"subject"\|"resource","key":"…"}` | 取属性；**缺失 → unknown** |
| `exists` | 同 `attr` | 属性键是否存在（不取值，区分 `null` 与缺失） |
| `not` | `{"op":"not","args":[x]}` | 三值非 |
| `and` / `or` | `{"op":"and","args":[x,y,…]}` | Kleene 三值与/或 |
| `eq` / `ne` | `{"op":"eq","left":…,"right":…}` | 族感知相等：`1≠"1"`、`1≠true`、跨族为 false |
| `lt`/`lte`/`gt`/`gte` | 同上 | 仅数字/字符串可比较；跨族或非可比类型 → 422 |
| `in` | `item in 列表` | 集合包含；右侧必须是列表 |
| `contains` | `列表 contains item` | 左侧必须是列表 |
| `subset` | `列表 subset 列表` | 左侧每个元素都在右侧中 |

三值真值表（Kleene）：

| A | B | A and B | A or B |
|---|---|---|---|
| true | unknown | **unknown** | **true** |
| false | unknown | **false** | **unknown** |
| unknown | unknown | unknown | unknown |

`not unknown = unknown`。

规则与策略文档：

```json
{
  "id": "document-access",
  "combining_algorithm": "deny_overrides",
  "rules": [
    {"id": "deny-contractors", "effect": "deny",
     "when": {"op": "in",
              "left": {"op": "attr", "bag": "subject", "key": "type"},
              "right": {"op": "lit", "value": ["contractor", "external-auditor"]}}},
    {"id": "permit-owner", "effect": "permit",
     "when": {"op": "and", "args": [ … , … ]}}
  ]
}
```

`effect` 取 `permit`（别名 `allow`）或 `deny`。

### 决策语义（deny-overrides + 默认拒绝）

1. 任一规则条件为 **true 且 effect=deny** → **deny**（显式拒绝压倒一切）；
2. 否则任一 **true 且 effect=permit** → **permit**；
3. 全部 false/unknown，或没有规则 → **deny**（`default_deny_no_match`）。

**未知绝不授权**：条件引用了缺失属性、或对缺失集合做 `in`/`subset` 时，该规则
unknown、不触发；因此只有属性齐备且条件明确成立的 permit 才能放行。

### 最小相关规则集合（`relevant_rules`）

返回真正参与决定的触发规则，排序后输出，故**与规则书写顺序无关**：

- 显式拒绝：所有触发的 deny 规则 id（每条都是充分拒绝理由，全部列出）；
- 放行：所有触发的 permit 规则 id（unknown/false 的规则被排除）；
- 默认拒绝：`[]`。

多策略级别为 `"策略id:规则id"`。

---

## 4. HTTP 接口与请求样例

服务启动后可访问 `http://127.0.0.1:8099/docs` 查看 OpenAPI 交互文档。

### `GET /healthz` / `GET /v1/trust`

```bash
curl -s http://127.0.0.1:8099/healthz
# {"status":"ok","unsigned_allowed":false,"trusted_key_count":1}
curl -s http://127.0.0.1:8099/v1/trust
# {"trusted_kids":["3f21f800d4e92b50dbc09d9a83aed06d"]}
```

### `POST /v1/evaluate` — 单策略求值

请求体二选一：`"signed"`（签名包）或 `"policy"`（内联，需开启
`POLICY_ALLOW_UNSIGNED=1`），外加 `subject` / `resource` 属性对象。

```bash
# 用 jq 把签名包和请求样例拼起来
jq -n --slurpfile s examples/policy.signed.json --slurpfile r examples/request_deny_contractor.json \
  '{signed:$s[0], subject:$r[0].subject, resource:$r[0].resource}' \
  | curl -s -X POST http://127.0.0.1:8099/v1/evaluate \
    -H 'content-type: application/json' -d @-
```

响应（节录）：

```json
{
  "policy_id": "document-access",
  "decision": "deny",
  "reason": "explicit_deny_overrides",
  "matched_deny_rules": ["deny-contractors"],
  "matched_permit_rules": [],
  "relevant_rules": ["deny-contractors"],
  "traces": [ {"rule_id":"deny-contractors","effect":"deny",
               "condition":"true","matched": true}, … ],
  "order_check": {"order_invariant": true, "variants_tested": 121, …}
}
```

`traces` 给出每条规则的 `true/false/unknown` 求值结果，便于审计；
`order_check` 对规则做**全排列**（≤5 条时，5!=120 个变体加原始共 121）或
正序/按 id 升序/降序抽样，证明换序后决策与触发规则集合不变。

### `POST /v1/evaluate-policy-set` — 多策略组合与冲突

请求体为 `"policies":[…]` 或包含 `{"policies":[…]}` 的签名包。
跨策略仍为 deny-overrides：**所有**策略都 permit 才 permit；任一策略 deny 即 deny；
同时存在 permit 策略和 deny 策略时 `conflict=true`，并给出
`permit_policies` / `deny_policies` / `explicit_deny_policies`。

```bash
jq -n --slurpfile s examples/policy_set.signed.json \
  '{signed:$s[0],
    subject:{role:"admin",region:"US",groups:["eng"]},
    resource:{group:"eng",action:"read",region:"EU"}}' \
  | curl -s -X POST http://127.0.0.1:8099/evaluate-policy-set \
    -H 'content-type: application/json' -d @-
# decision=deny, conflict=true, permit_policies=["rbac-policy"],
# deny_policies=["region-policy"],
# relevant_rules=["region-policy:deny-export-us"]
```

### `POST /v1/truth-table` — 枚举组合真值

对变量做笛卡尔积逐格求值，用于验收“枚举组合真值”：

```bash
curl -s -X POST http://127.0.0.1:8099/v1/truth-table \
  -H 'content-type: application/json' -d '{
    "signed": <把 examples/policy.signed.json 内容粘进来>,
    "mode": "full",
    "variables": [
      {"bag":"resource","key":"classification","values":["public","internal","secret"]},
      {"bag":"resource","key":"action","values":["read","write"]}
    ]
  }'
```

- `mode`：`binary`（每变量前两个值）、`ternary`（两值 + 一个“缺失”格）、
  `full`（全部取值；`include_missing=true` 时追加缺失格）；
- 上限 `MAX_ROWS=4096`（2^12），超出返回 400；
- 响应含 `row_count`、`decision_counts`、`distinct_decisions`、
  `conflicting_cells`（多策略时的 permit/deny 冲突格数）、每行的 decision 与
  relevant_rules，以及在确定性抽样格（最多 64 格）上做的顺序无关复核
  `order_invariant`。

### 错误信封与状态码

```json
{"error": "untrusted_policy", "message": "signature verification failed"}
```

| HTTP | error | 触发条件 |
|---|---|---|
| 400 | `invalid_request` | 缺 policy/signed、body 非 JSON、真值表参数非法 |
| 403 | `untrusted_policy` | 严格模式下未签名、kid 不被信任、签名/算法不符 |
| 413 | `payload_too_large` | 请求体超过 256 KiB |
| 422 | `invalid_policy` | AST/文档结构非法（算子不在白名单、字段缺失等） |
| 422 | `evaluation_error` | 求值期类型冲突（如数字与字符串比大小、`in` 右侧非列表） |

---

## 5. 安全设计：为什么策略无法执行任意代码

1. **无代码、只有数据**：条件是受限 AST，算子走固定白名单分派；
   `tests/test_security.py` 以 AST 扫描保证 `app/` 下不出现
   `eval/exec/compile/__import__`，也不引入 `pickle/subprocess/importlib` 等。
2. **无隐式真值**：数字/字符串不能当布尔用（`and` 操作数非布尔即 422），
   相等不做跨类型强转，避免 `1=="1"` 之类绕过。
3. **缺失即未知、未知即不放行**，从根上杜绝“属性没给所以条件糊里糊涂为真”。
4. **签名信任链**：生产模式只接受 `EdDSA`（Ed25519）签名包；验签对
   **规范化 JSON**（键按 UTF-8 排序、无空白）进行，重排键不影响签名；
   `kid` 是公钥 DER 的 SHA-256 前 16 字节，必须命中预置信任锚；
   信任目录只加载 `*.pub.pem`，私钥误放进去不会被读取。
5. **其他护栏**：Pydantic 禁止未知顶层字段、请求体 256 KiB 上限、
   真值表行数上限、规则 id / 策略 id 唯一性校验。

---

## 6. 自动化测试

```bash
source .venv/bin/activate
python -m pytest -q
```

覆盖：三值逻辑全真值表、全部算子、缺失属性 unknown、类型安全、
静态 AST 校验、deny 覆盖 permit、默认拒绝、最小相关规则集、
**5 条规则全 120 排列顺序无关**、多策略冲突与排列无关、
真值表枚举与行数上限、Ed25519 验签/篡改/伪造/错误算法/RSA 拒绝、
HTTP 全路由（含 403/400/413/422）、以及“无任意代码执行”静态扫描。

---

## 7. 实际运行结果记录（2026-09-24，如实记录）

环境：Linux 6.8、Python 3.12.3、cryptography 41.0.7（系统预装），
虚拟环境内按 `requirements.lock` 安装。

- **测试**：`python -m pytest -q` → **75 passed**（1 条 Starlette 关于
  TestClient/httpx 的弃用告警，不影响结果）。
- **端到端（`POLICY_ALLOW_UNSIGNED=0`，真实 uvicorn 进程）**：
  - 4 个示例场景结果：
    - `request_permit` → `permit`，相关规则
      `[permit-owner, permit-verified-employee]`；
    - `request_deny_contractor` → `deny / explicit_deny_overrides`，
      相关规则 `[deny-contractors]`（即使其它 permit 同时成立）；
    - `request_deny_confidential` → `deny`，相关规则
      `[deny-confidential-resource]`；
    - `request_permit_public` → `permit`，相关规则 `[permit-public-read]`。
  - 缺属性场景：subject 仅 `{user}`、resource 缺 classification →
    `deny / default_deny_no_match`，相关规则 `[]`，多条规则 trace 为 `unknown`。
  - 多策略冲突（US admin 访问 EU 资源）：`deny`、`conflict=true`，
    `permit_policies=[rbac-policy]`、`deny_policies=[region-policy]`，
    相关规则 `[region-policy:deny-export-us]`，顺序复核 7 个变体全一致。
  - 真值表（classification×action，full）：6 行，
    `{permit:1, deny:5}`，仅 public+read 放行；`ternary` 模式正确出现缺失格。
  - 安全用例：严格模式内联策略 → 403；篡改文档 → 403 验签失败；
    陌生私钥重签 → 403（kid 不被信任）；`{"op":"eval",…}` → 422
    `unknown operator 'eval'`（代码未执行）；超大请求体 → 413；
    顺序无关证明对 5 条规则跑满 120 个排列 + 原始共 121 个变体，
    决策与相关规则集合完全一致。

### 未完成项 / 已知限制（如实说明）

- **组合算法只实现了 `deny_overrides`**（任务要求的显式拒绝优先）；
  XACML 的 permit-overrides / only-one-applicable 等未实现，文档传入会被 422 拒绝。
- 策略**不支持属性解析函数/远程 PIP**（题目限定离线）；所有属性必须随请求给出。
- 签名固定 **Ed25519/EdDSA**，未实现密钥轮换列表、吊销（CRL/OCSP）或多 kid 并行；
  轮换需更换信任目录并重新签名。
- 真值表枚举上限 4096 行；顺序无关在大表上采用 64 格确定性抽样而非逐格全排列
  （单策略/单次请求路径在规则 ≤5 时仍是全排列证明）。
- 未做速率限制、鉴态与多租户隔离；本服务定位为受信网络内的纯后端求值组件。
- `examples/keys/demo.pem` 是**演示私钥并随仓库提供**，仅限本地演示；
  生产部署必须用 `tools/gen_keys.py` 自行生成并妥善保管私钥。
