# 离线策略求值解释器（Offline Policy Evaluation Interpreter）

纯后端的资源访问策略解释器：策略是一份**声明式 JSON**（条件 AST + 规则 + 策略），
服务端只对其做解释求值，**不执行任何用户提供的代码**。策略可用 Ed25519
离线签名，服务端只在验签通过后才求值，保证分发链路不被篡改。

- 语言/框架：Python 3.12 · FastAPI · Pydantic v2
- 密码学：`cryptography`（Ed25519，RFC 8032）
- 组合算法：`deny-overrides`（显式拒绝优先），集合语义，**与规则书写顺序无关**

---

## 1. 语义模型

### 1.1 三值逻辑（Kleene）

每个条件子表达式的结果是三值之一：

| 值 | 含义 |
|---|---|
| `true` | 可确定为真 |
| `false` | 可确定为假 |
| `unknown` | 属性缺失、路径中段非对象、叶子是对象/`null`、或类型不匹配 |

**缺失属性产生 unknown，unknown 按拒绝处理（fail-closed）**：
规则条件为 `unknown` 时规则不生效；没有任何 allow 生效时最终决定为 `DENY`。

真值表（`U` = unknown）：

```
AND : 任一 false => false；否则任一 U => U；否则 true
OR  : 任一 true  => true ；否则任一 U => U；否则 false
NOT : true<->false，U->U
eq/neq/集合算子 : 任一操作数为 U，或类型不符（不做隐式转换）=> U
```

`bool` 与 `int` 严格区分（`true != 1`），字符串不与数字比较。

### 1.2 算子白名单

| 类别 | 算子 |
|---|---|
| 逻辑 | `and`（1..n 参）、`or`（1..n 参）、`not`（1 参） |
| 比较 | `eq`、`neq`（两个标量） |
| 集合 | `in`（标量 ∈ 集合）、`contains`（集合含标量/子集）、`superset`、`subset`、`intersects` |

白名单之外的一切名字都只是数据，解析期直接拒绝。**没有 eval/exec、没有动态导入、
没有用户可控的函数调用**，策略永远不可能成为可执行代码。

### 1.3 决策组合（deny-overrides，顺序无关）

1. 任一条件为 `true` 的 **deny** 规则生效 → `DENY`（`reason=explicit_deny`），优先级高于所有 allow；
2. 否则，任一条件为 `true` 的 **allow** 规则生效 → `ALLOW`（`reason=explicit_allow`）；
3. 否则（全部 false/unknown）→ `DENY`（`reason=no_applicable_rule`，默认拒绝）。

求值按集合语义进行：所有规则先独立求值，再做集合组合。所有返回的规则 id
列表均按字典序排序，因此规则与策略的书写/传输顺序如何打乱，`decision` 与
各规则集合都完全相同（测试用 5! = 120 种全排列 + 3! 策略全排列 + 随机打乱验证）。

### 1.4 最小相关规则集合

- `minimal_relevant_rules`：**最小代表性集合**——
  - DENY：字典序最小的一条生效 deny（仅凭它就能推出拒绝）；
  - ALLOW：字典序最小的一条生效 allow；
  - 默认 DENY（无生效规则）：空列表，此时看 `counts.indeterminate` 区分
    “明确不匹配”与“因属性缺失而未知”。
- `matched_rules`：全部生效（条件为 true）的规则；
- `conflicting_allow_rules`：DENY 时同时生效但被压制的 allow，用于解释冲突。

> 说明：`minimal_relevant_rules` 取的是“足以单独支撑决定”的代表规则
> （XACML 风格的最小辩护在“默认即拒绝”系统里会退化为空集，不利于解释，
> 因此这里用代表性集合 + 完整 `matched_rules`/冲突集合共同给出解释信息）。

---

## 2. 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── conditions.py    # 条件 AST：白名单解析 + 三值解释器
│   ├── engine.py        # 策略校验 + deny-overrides 求值
│   ├── crypto.py        # Ed25519 签发/验签 + 规范化 JSON
│   ├── errors.py        # 错误类型与结构/体积上限
│   └── main.py          # FastAPI 路由、ASGI 体积限制、错误码
├── scripts/
│   ├── generate_keys.py # 生成 Ed25519 密钥对
│   └── sign_policy.py   # 为策略文件签发签名包
├── examples/
│   ├── policies.json             # 示例策略
│   ├── request_allow.json        # 放行样例
│   ├── request_deny_conflict.json# allow/deny 冲突样例
│   ├── request_unknown_deny.json # 缺属性未知 => 拒绝样例
│   └── signed_bundle.json        # 已签名策略包（由脚本生成）
├── tests/                # 65 个自动化测试
├── requirements.txt      # 直接依赖（固定版本）
├── requirements.lock.txt # 运行时完整传递依赖锁定（18 个包）
├── requirements-dev.txt  # 测试附加依赖
└── pytest.ini
```

---

## 3. 依赖与启动

需要 Python 3.10+（开发环境为 3.12.3）。

```bash
# 1) 建虚拟环境并安装锁定依赖（生产）
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt

# 测试需要额外安装
.venv/bin/pip install -r requirements-dev.txt

# 2) 生成签名密钥（首次；生产请妥善保管私钥，勿沿用开发密钥）
.venv/bin/python scripts/generate_keys.py keys 2026-09-dev

# 3) 签发示例策略
.venv/bin/python scripts/sign_policy.py examples/policies.json \
    --private-key keys/dev_private.pem --kid 2026-09-dev \
    --out examples/signed_bundle.json

# 4) 启动（纯后端 HTTP 服务）
POLICY_TRUSTED_PUBLIC_KEY=keys/dev_public.pem \
    .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `POLICY_TRUSTED_PUBLIC_KEY` | `keys/dev_public.pem` | 受信 Ed25519 公钥路径；缺失时签名接口返回 503 |
| `POLICY_MAX_BODY_BYTES` | `1048576`（1 MiB） | 请求体硬上限（ASGI 层逐块计数，chunked 传输也拦截） |

自动生成的交互式文档：`http://127.0.0.1:8000/docs`（FastAPI Swagger UI）。

---

## 4. HTTP 接口与请求样例

### 4.1 `GET /healthz` / `GET /v1/trust`

```bash
curl -s http://127.0.0.1:8000/healthz
# {"status":"ok"}

curl -s http://127.0.0.1:8000/v1/trust
# {"configured":true,"path":"keys/dev_public.pem","alg":"Ed25519","sha256":"..."}
```

### 4.2 `POST /v1/evaluate` — 直接提交策略集求值

```bash
curl -s -X POST http://127.0.0.1:8000/v1/evaluate \
  -H 'content-type: application/json' \
  -d @examples/request_allow.json
```

请求体（`examples/request_allow.json` 内含完整 `policies`）：

```json
{
  "policies": [
    {
      "id": "p-access",
      "rules": [
        {
          "id": "r-allow-internal-read",
          "effect": "allow",
          "condition": {
            "op": "and",
            "args": [
              {"op": "eq", "args": [{"attr": "subject.department"}, {"literal": "engineering"}]},
              {"op": "in", "args": [{"literal": "read"}, {"attr": "subject.clearances"}]},
              {"op": "intersects", "args": [
                  {"attr": "subject.groups"}, {"literal": ["internal", "staff"]}]}
            ]
          }
        },
        {
          "id": "r-deny-prod-secrets",
          "effect": "deny",
          "condition": {"op": "and", "args": [
            {"op": "eq", "args": [{"attr": "resource.classification"}, {"literal": "secret"}]},
            {"op": "contains", "args": [{"attr": "resource.tags"}, {"literal": "prod"}]},
            {"op": "neq", "args": [{"attr": "subject.role"}, {"literal": "admin"}]}
          ]}
        }
      ]
    }
  ],
  "subject":  {"department": "engineering", "clearances": ["read", "export"],
               "groups": ["internal"], "employment_type": "employee", "role": "developer"},
  "resource": {"id": "repo/edge-gateway", "classification": "internal",
               "tags": ["repo", "source"]}
}
```

响应（节选）：

```json
{
  "decision": "ALLOW",
  "reason": "explicit_allow",
  "combining_algorithm": "deny-overrides",
  "minimal_relevant_rules": ["r-allow-clearance-superset"],
  "matched_rules": ["r-allow-clearance-superset", "r-allow-internal-read"],
  "conflicting_allow_rules": [],
  "counts": {"rules_total": 4, "fired_allow": 2, "fired_deny": 0,
             "not_applicable": 2, "indeterminate": 0},
  "rule_results": [ ... 每条规则的 true/false/unknown 与是否 fired ... ]
}
```

**冲突样例**（`examples/request_deny_conflict.json`：2 条 allow 命中，同时 1 条 deny 命中）：

```json
{
  "decision": "DENY",
  "reason": "explicit_deny",
  "minimal_relevant_rules": ["r-deny-prod-secrets"],
  "matched_rules": ["r-deny-prod-secrets"],
  "conflicting_allow_rules": ["r-allow-clearance-superset", "r-allow-internal-read"],
  "counts": {"fired_allow": 2, "fired_deny": 1, "not_applicable": 1, "indeterminate": 0}
}
```

**未知/缺属性样例**（`examples/request_unknown_deny.json`：主体没有 `clearances`）：

```json
{
  "decision": "DENY",
  "reason": "no_applicable_rule",
  "minimal_relevant_rules": [],
  "counts": {"fired_allow": 0, "fired_deny": 0, "not_applicable": 2, "indeterminate": 2}
}
```

### 4.3 `POST /v1/evaluate-signed` — 验签后求值（推荐）

请求体外层包一个签名包：

```bash
.venv/bin/python - <<'PY'
import json, urllib.request
bundle = json.load(open("examples/signed_bundle.json"))
payload = {"bundle": bundle,
           "subject":  {"department": "engineering", "clearances": ["read"],
                        "groups": ["internal"], "employment_type": "employee",
                        "role": "developer"},
           "resource": {"classification": "internal", "tags": ["docs"]}}
r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8000/v1/evaluate-signed",
    data=json.dumps(payload).encode(),
    headers={"content-type": "application/json"}))
print(r.read().decode())
PY
```

签名包结构（`alg` 固定 `Ed25519`；签名 = 对 `canonicalize(policies)` 的 Ed25519 签名，
规范化时对象键排序、无空白、拒绝 NaN/非 JSON 类型，因此键重排不影响验签）：

```json
{
  "version": 1,
  "alg": "Ed25519",
  "kid": "2026-09-dev",
  "canonical": "https://openpolicy.local/canonical/v1",
  "policies": [ ... ],
  "signature": "<base64>"
}
```

错误码：

| HTTP | code | 触发条件 |
|---|---|---|
| 400 | `invalid_policy` | 策略/条件结构或语义非法（details 给出每个错误的 JSON 路径） |
| 400 | `invalid_bundle` | 签名包字段/版本/算法/base64 非法 |
| 403 | `invalid_signature` | 签名与内容不一致，或公钥不被信任 |
| 413 | `payload_too_large` | 请求体超限 |
| 503 | `trust_not_configured` | 服务端未配置受信公钥 |
| 422 | — | Pydantic 请求体外层结构校验失败 |

---

## 5. 安全模型（为什么不能执行任意代码）

1. **策略只是数据**：条件为固定算子的 AST，解释器用 if/分派执行内置算子，
   代码库中没有任何 `eval`/`exec`/`compile`/动态导入会接触策略内容。
2. **白名单**：未知算子名（即使形如 `__import__` 或 `().__class__...`）在解析期拒绝。
3. **属性沙箱**：属性路径正则限定 `subject.*`/`resource.*` 下的点分安全标识符，
   无法访问环境、文件系统或 Python 对象。
4. **类型封闭**：字面量禁止 `null`/对象/嵌套集合；比较不做隐式转换；类型不符即 unknown。
5. **资源上限**：策略数 ≤ 50、规则 ≤ 500、条件节点 ≤ 2000、嵌套深度 ≤ 20、
   字符串字面量 ≤ 1000、请求体 ≤ 1 MiB，防止畸形策略/请求造成资源耗尽。
6. **完整性与真实性**：Ed25519 签名 + 规范化 JSON，篡改 effect、增删规则、
   换用未受信密钥签名一律 403；验签在任何解析之前完成。

---

## 6. 自动化测试与本次实际运行结果

运行：

```bash
.venv/bin/python -m pytest
```

测试覆盖（65 个，**全部通过**）：

- `test_conditions.py`：枚举 AND/OR 的 3²=9 种、NOT 的 3 种三值组合完整真值表；
  德摩根律在三值逻辑下的 9 种组合；缺属性/类型不符的 unknown 传播；
  5 个集合算子；非法算子/路径逃逸/危险字面量/深度上限的拒绝。
- `test_engine.py`：allow/deny/默认拒绝；跨策略 deny-overrides；多策略冲突；
  最小相关规则集合；**规则 5!=120 种全排列、策略 3!=6 种全排列、50 次随机打乱
  结果逐一相等**；3 条规则外部状态 3³=27 种组合枚举；结构校验；最小规则的
  “单独支撑决定 / 翻转后决定改变”验证。
- `test_crypto.py`：规范化 JSON 键序无关与类型拒绝；签发/验签往返；
  篡改 effect、偷加规则、换密钥三种攻击被拒；签名包各字段校验。
- `test_api.py`：四个端点、错误码、缺省 503、公钥指纹、1 MiB 体积限制。

本次会话中的实际运行记录：

```
$ .venv/bin/python -m pytest
65 passed, 1 warning in 0.63s
```

（1 个 warning 来自 starlette TestClient 对 anyio 别名的弃用提示，与本项目代码无关。）

另对真实 `uvicorn` 服务（127.0.0.1:8137）做了端到端 curl/Python 请求：
ALLOW、显式 DENY 冲突（含被压制的 allow 列表）、缺属性 unknown 默认 DENY、
签名包正常放行、篡改后 403、未受信密钥 403、规则打乱 10 次结果一致、
非法算子与属性逃逸均 400——均符合预期。

---

## 7. 未完成项 / 已知边界

- **无持久化与多租户**：策略由请求体提交（离线解释器定位），没有策略库、
  版本管理或多 key id 轮换表；当前只信任一把公钥。
- **无 action/环境上下文命名空间**：属性根严格限定 `subject`/`resource`
  （按题目范围）。若需要动作（read/write）或环境（ip/time）条件，
  可在 `conditions._PATH_RE` 与求值入口加入 `action`/`environment` 根，
  三值逻辑与组合算法无需改动。
- **最小相关规则集合采用“代表性最小集”**（见 1.4 说明），完整解释信息
  通过 `matched_rules` 与 `conflicting_allow_rules` 给出；未输出完整的
  形式化最小辩护（prime implicant）。
- 示例 `keys/dev_*` 密钥已随生成步骤存在于本机 `keys/`（已在 `.gitignore`
  中，不入版本库）；`examples/signed_bundle.json` 是用该开发密钥签的样例，
  生产部署必须重新生成密钥并重签。
- 未做容器化/Dockerfile、未做限流与鉴权（按“纯后端解释器 + HTTP 接口”范围）。
