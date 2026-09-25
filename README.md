# DMS — 脱敏规则编译器（纯后端）

本地运行的结构化 JSON 数据脱敏服务：把声明式脱敏规则编译成可执行计划，
对 JSON 文档执行字段级脱敏。仅后端，无前端、无外部账号、无自创密码算法。

## 安全边界

| 约束 | 做法 |
| --- | --- |
| 不自创加密算法 | 加密用 [`cryptography`](https://cryptography.io) 的 **Fernet**（AES-128-CBC + HMAC-SHA256 认证加密）；密钥推导用 **HKDF-SHA256**；哈希用 **SHA-256** |
| 不接生产账号 | 主密钥仅由 `os.urandom(32)` 在本地生成，落盘权限 `0600`，默认路径 `.secrets/`（已 gitignore），仅用于测试 |
| 默认拒绝未知规则 | 未知 action、未知规则字段、未知 option、错误参数类型、未知顶层字段，一律**编译期拒绝整个规则集** |
| 不泄露原始值 | 异常与 HTTP 错误响应只含规则 id/路径等 schema 信息；日志过滤器对文档中出现过的标量值做二次抹除（纵深防御） |

## 支持的脱敏动作

| action | 含义 | options（默认值） |
| --- | --- | --- |
| `mask` | 保留末尾 N 个字符，其余替换为掩码字符（按 **Unicode 码点**计数） | `keep_last=4`、`mask_char="*"`（必须单字符） |
| `redact` | 整体替换为固定占位串 | `replacement="***REDACTED***"`（非空字符串） |
| `drop` | 删除该字段（对象删键、数组删元素） | — |
| `hash` | SHA-256 十六进制摘要（确定性，字符串/数字/布尔标量） | — |
| `encrypt` | Fernet 认证加密，输出 ASCII token | — |
| `decrypt` | Fernet 解密；密文无效时报错且不回显输入 | — |

> 容器（对象/数组）上的 `redact`/`mask` 等规则会**沿子树流到标量叶节点**并保留
> 文档结构；`drop` 在容器上整体删除。标量类型不匹配时（如对数字做 `mask`）
> 报 `transform_error`，消息不含该值。

## 路径语法

`$` 为可选根前缀；段之间用 `.` 分隔。

| 写法 | 含义 |
| --- | --- |
| `$.users[*].phone` | 段通配：数组（或对象）所有元素的 `phone` |
| `$..note` | 递归下降：任意深度的 `note` |
| `items[2].id` | 数组下标 |
| `$["a.b"]` / `$['单 键']` | 方括号引号键（含点号、空格、非 ASCII） |
| `users[*].addresses[*].city` | 嵌套数组 |
| `$` | 文档根 |

非法路径（空、未闭合括号、负数下标、`..` 结尾等）在编译期报错。

## 多规则合成（显式优先级）

同一字段被多条规则命中时：

1. `priority`（整数，默认 `0`）**数值大者生效**；
2. 完全相同的路径模式上存在同优先级但动作不同的规则 → **编译期** `rule_compile_error`；
3. 通配/递归规则与后代规则在同优先级下作用域重叠 → **运行期** `rule_conflict`（fail-closed，绝不静默二选一）；
4. 高优先级后代规则可“刺穿”低优先级祖先规则；低优先级后代在高优先级祖先作用域内不生效；
5. 同一条递归/通配规则在祖先和后代位置重叠不视为冲突。

可选字段 `require_match: true` 表示该规则在文档中**零命中即报错**（`missing_field`）；
默认对缺失字段安静跳过。

## 规则集示例

见 [`examples/rules.json`](examples/rules.json)：

```json
{
  "version": 1,
  "rules": [
    {"id": "mask-user-phone", "action": "mask",
     "path": "$.users[*].phone", "priority": 20,
     "options": {"keep_last": 4, "mask_char": "*"}},
    {"id": "hash-ssn", "action": "hash", "path": "$.users[*].ssn"},
    {"id": "drop-internal-tags", "action": "drop",
     "path": "$.users[*].internal_tags"}
  ]
}
```

## 快速开始

```bash
# 无第三方框架；运行时仅需 cryptography（Python ≥ 3.10）
python3 -m venv .venv && . .venv/bin/activate
pip install -e .          # 或：pip install cryptography（测试靠 pytest）

# 1) 生成本地测试密钥（仅 encrypt/decrypt/hash 需要）
python -m dms keygen                       # -> .secrets/dms-test-key.json (0600)

# 2) 命令行一次性脱敏
python -m dms mask --rules examples/rules.json --doc examples/document.json

# 3) 启动本地 HTTP 服务（默认仅绑定 127.0.0.1）
python -m dms serve --port 8080
```

### HTTP 端点

| 方法与路径 | 请求 | 说明 |
| --- | --- | --- |
| `GET /health` | — | 健康检查 |
| `POST /v1/compile` | 规则集 | 仅编译校验，返回规范化规则 |
| `POST /v1/mask` | `{"rules": {...}, "document": ..., "key_file"?: "..."}` | 编译并脱敏 |
| `POST /v1/mask-compiled` | `{"compiled": {...}, "document": ...}` | 复用已编译规则（服务端仍重新校验，防止绕过默认拒绝） |

```bash
curl -s -X POST http://127.0.0.1:8080/v1/mask \
  -H 'Content-Type: application/json' \
  --data @examples/request_mask.json
```

请求体上限 1 MiB；错误形如：

```json
{"ok": false, "error": {"code": "unknown_rule",
  "message": "未知规则动作 'frobnicate'（默认拒绝）",
  "details": {"action": "frobnicate", "index": 0}}}
```

### Python API

```python
from dms.crypto import CryptoProvider
from dms.rules import compile_rules
from dms.engine import apply_rules

compiled = compile_rules(rules_spec)          # RuleCompileError / UnknownRuleError
result = apply_rules(compiled, document,
                     crypto=CryptoProvider.load(".secrets/dms-test-key.json"))
print(result.document, result.stats)
```

## 目录结构

```
src/dms/
  errors.py         # 异常层次（消息不含字段值）
  paths.py          # 路径解析与通配/递归匹配
  rules.py          # 规则编译器（默认拒绝 + 编译期冲突检查）
  engine.py         # 执行引擎（优先级合成、嵌套递归、统计）
  crypto.py         # Fernet/HKDF/SHA-256 封装与本地密钥管理
  logging_utils.py  # 敏感值登记 + 日志抹除过滤器
  server.py         # http.server 实现的本地 HTTP API
  cli.py            # keygen / serve / mask
examples/           # 规则、文档与请求样例
tests/              # pytest 自动化测试（71 个）
```

## 测试

```bash
python -m pytest -q
```

覆盖：路径解析与嵌套数组/递归匹配、Unicode（中日韩文 + emoji 码点计数）、
缺失字段（静默 / `require_match`）、规则冲突（编译期与运行期、优先级刺穿）、
加密往返与无效密文、密钥文件 0600、**日志与错误响应不出现原始敏感值**、
HTTP 端到端。

实际运行命令与结果见 [`RUN_REPORT.md`](RUN_REPORT.md)。
