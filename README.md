# maskcompiler — 结构化 JSON 脱敏规则编译器（纯后端）

本地运行的安全数据处理服务：用声明式 JSON 规则描述“哪些字段、用什么原语脱敏”，
编译器做严格校验与优先级合成，执行引擎遍历任意嵌套的 JSON 文档输出脱敏副本。
**仅后端**：一个可复用的 Python 库 + 标准库 HTTP 服务 + CLI，无前端、无数据库、
无外部账号。

密码学原语全部来自成熟库 [`cryptography`](https://cryptography.io/)
（HMAC 基于 `cryptography.hazmat.primitives.hmac`，可逆加密使用 Fernet =
AES-128-CBC + HMAC-SHA256 的认证加密）。**不自创加密算法**；密钥全部本地生成，
不接入任何生产账号或 KMS。

---

## 1. 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e .            # 或 pip install -r requirements-dev.txt

# 1) 生成本地测试密钥（0600 权限）
.venv/bin/maskcompiler keygen --out local-test-keys.json

# 2) 校验/编译规则集
.venv/bin/maskcompiler compile -r examples/rules.json

# 3) 对文档执行脱敏
.venv/bin/maskcompiler apply -r examples/rules.json \
    -d examples/document.json --key-file local-test-keys.json

# 4) 运行本地 HTTP 服务
.venv/bin/maskcompiler serve --key-file local-test-keys.json --port 8080

# 运行测试
.venv/bin/python -m pytest
```

不用安装也可以直接 `PYTHONPATH=src python3 -m maskcompiler ...`。

---

## 2. 规则集格式

```json
{
  "version": 1,
  "rules": [
    {
      "id": "phone-mask",
      "path": "$.users[*].phone",
      "transform": "mask",
      "params": { "keep_first": 3, "keep_last": 4 },
      "priority": 100,
      "on_missing": "ignore"
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 是 | 固定 `1` |
| `rules` | 是 | 非空数组 |
| `rules[].id` | 是 | 规则唯一标识（非空字符串，不可重复） |
| `rules[].path` | 是 | JSONPath 子集路径，见下 |
| `rules[].transform` | 是 | `mask` / `redact` / `hash` / `encrypt` |
| `rules[].params` | 否 | 原语参数对象，默认 `{}` |
| `rules[].priority` | 是 | **非负整数，必须显式给出**（无隐式优先级） |
| `rules[].on_missing` | 否 | `ignore`（默认）或 `error` |

### 默认拒绝（default-deny）

以下情况一律在**编译期**报错，拒绝规则集：

- 顶层未知键、规则对象未知键；
- 未知 `transform`；
- 未知 transform 参数（每个原语都有参数白名单）；
- 非法路径、非整数/负的 `priority`、非法 `on_missing`、重复 `id`、缺失 `version`；
- 同一精确路径上两条**同优先级**规则（静态冲突）。

### 路径语法（JSONPath 子集）

| 表达式 | 含义 |
|---|---|
| `$` | 文档根 |
| `$.a.b` / `$['a'].b` | 对象子键；带特殊字符的键用引号（支持单/双引号，`''` 转义） |
| `$.users[*]` | 数组每个元素（`[*]` 作用于对象时取其所有值） |
| `$.items[0]` / `$.items[-1]` | 指定下标，负数从末尾计数，越界无匹配 |
| `$..phone` | 递归下降：任意深度下所有 `phone` 键 |
| `$..[*]` | 任意深度下所有数组元素 |

段只在结构匹配时生效：对象键段作用于非对象、下标段作用于非数组 → 无匹配
（路径是“选择器”，不是断言）。

### 转换原语

#### `mask` — 保留首尾字符的遮盖（按 Unicode 码点计数）

```json
{ "transform": "mask",
  "params": { "keep_first": 0, "keep_last": 4, "mask_char": "*" } }
```

- `keep_first` / `keep_last`：非负整数，默认 0；
- 当 `keep_first + keep_last >= 原长度`（前缀后缀相遇/重叠）时整体遮盖；
- `mask_char`：必须是**单个 Unicode 字符**（默认 `*`）；
- `mask_length`：可选，遮盖部分固定长度（省略时等于中间字符数）。

示例：`"13812345678"` + `keep_first=3, keep_last=4` → `"138****5678"`；
`"张三"` + `keep_last=1` → `"*三"`；`"孙悟空"` + `keep_first=1, keep_last=1`
→ `"张*空"`。

#### `redact` — 删除/替换

```json
{ "transform": "redact",
  "params": { "replacement": null, "container": "recurse" } }
```

- `replacement`：替换值，任意 JSON 值，默认 `null`；
- `container`：命中容器（对象/数组）时的模式：
  - `recurse`（默认）：保留容器结构，容器内未被更强规则命中的叶子全部替换，
    **允许更高优先级的子字段规则“裁剪”出保留字段**；
  - `replace`：整个命中片段（连同子树）直接替换为 `replacement`。

#### `hash` — 密钥化 HMAC 假名化（确定性）

```json
{ "transform": "hash",
  "params": { "algo": "sha256", "encoding": "hex", "prefix": "" } }
```

- `algo`：`sha256`（默认）/ `sha384` / `sha512`（不支持 MD5 等弱算法）；
- `encoding`：`hex`（默认）/ `base64`；`prefix`：输出前缀（默认无）；
- 使用密钥文件中的 HMAC 密钥；同一输入在同一密钥下输出稳定，换密钥则不同；
- 未提供密钥文件时直接报错（**不会**内置默认密钥）。

#### `encrypt` — 可逆认证加密（Fernet）

输出为 Fernet token 字符串（UTF-8），含随机 IV 与时间戳，因此同一明文每次输出
不同。解密需同一密钥文件：

```python
from maskcompiler.keys import load_keyfile
b = load_keyfile("local-test-keys.json")
b.fernet().decrypt(token.encode()).decode()
```

### 多规则合成（同一字段多条规则）

1. 全部规则先在**原始文档**上完成路径匹配（避免高优先级 redact 先抹掉低优先级
   规则要看到的位置）；
2. 每个位置取**最高优先级**的命中规则；
3. 同一位置最高优先级出现两条不同规则 → `RuleConflictError`：
   - 精确路径同优先级在编译期发现（409）；
   - 通配符导致的同优先级覆盖在执行期按数据发现（409）；
4. `redact`（`recurse` 模式）命中容器时，其覆盖流向后代：
   - 后代有**更高**优先级直接命中规则 → 该后代按自己的规则处理（裁剪保留）；
   - 后代有**同优先级的不同**规则 → 冲突错误；
   - 后代只有更低优先级规则或无规则 → 被 redact 覆盖（低优先级规则被吞掉）；
5. 失败关闭（fail closed）：标量原语（mask/hash/encrypt）遇到非字符串 JSON 值
   抛 `TypeMismatchError`（`null` 原样保留，布尔值不会被当作字符串）；
   输入文档不会被修改。

### 缺失字段

- `on_missing: ignore`（默认）：规则零匹配时安静跳过（报告里计数为 0）；
- `on_missing: error`：规则零匹配时抛 `MissingFieldError`（HTTP 422）。

---

## 3. HTTP 服务（仅标准库 `http.server`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/v1/rulesets` | 体：`{"name": ..., "ruleset": {...}}`，编译并保存 |
| GET | `/v1/rulesets/{name}` | 规则集摘要（不含任何数据） |
| DELETE | `/v1/rulesets/{name}` | 删除 |
| POST | `/v1/rulesets/{name}/apply` | 体：`{"document": <任意 JSON>}`，返回 `output` 与 `report` |

状态码：`200/201` 成功；`400` 规则/请求问题（含未知规则集）；`409` 规则冲突；
`422` 缺失字段 / 类型不匹配；`413` 请求体超过 10 MiB。

响应例：

```json
{
  "output": { "users": [ { "phone": "*******5678" } ] },
  "report": { "transformed_locations": 1, "matched_rules": { "phone": 1 } }
}
```

curl 演示见 `examples/curl-demo.sh`（默认打 `http://127.0.0.1:8080`）。

## 4. Python API

```python
import json

from maskcompiler import compile_ruleset, apply_ruleset, load_keyfile

with open("examples/rules.json", encoding="utf-8") as fh:
    compiled = compile_ruleset(json.load(fh))
compiled.bind_keys(load_keyfile("local-test-keys.json"))

result = apply_ruleset(compiled, {"users": [{"phone": "13812345678"}]})
print(result.output)               # {'users': [{'phone': '*******5678'}]}
print(result.report.matched_rules)
```

异常类型见 `src/maskcompiler/errors.py`：`RuleSyntaxError`、
`UnknownTransformError`、`RuleParameterError`、`PathSyntaxError`、
`RuleConflictError`、`MissingFieldError`、`TypeMismatchError`、`KeyManagerError`。

---

## 5. 安全说明

1. **不自创密码学**：只用 `cryptography` 的 HMAC 与 Fernet；不支持 MD5/SHA1。
2. **密钥本地管理**：`keygen` 用 `os.urandom` 生成 ≥256 位 HMAC 密钥与 Fernet
   密钥，落盘文件 `O_EXCL` 创建并强制 `chmod 0600`；加载时拒绝组/其他用户可读的
   密钥文件；无硬编码密钥、无默认密钥（不带密钥文件使用 hash/encrypt 直接失败，
   HTTP 服务每次进程生成一次性临时密钥并告警）。
3. **原始敏感值不进日志（核心验收项，多层防御）**：
   - 所有异常消息在设计上只携带规则 id、路径、参数名、类型名，**绝不拼接数据值**；
   - 每次脱敏执行开启 `secret_scope`，输入中的每个标量（字符串/数字/布尔）都会被
     登记；日志过滤器（`SecretRedactionFilter`）会把日志的消息模板、格式化参数与
     **异常 traceback** 中出现的已登记原始值整体替换成 `***`；
   - 执行失败时密钥集合随逃逸异常在线程局部保留（weak key，异常被回收即清除），
     在 `with secret_scope()` 之外的上层 `logger.exception(...)` 仍然被脱敏；
   - HTTP 访问日志只记录方法与路径，**不记录请求/响应体、请求头**；
   - 错误响应体同样不含输入值。
4. **默认拒绝**：未知规则、未知参数、未知 transform、非法优先级全部拒绝。
5. **无副作用**：脱敏在深拷贝语义的新树上完成，原始入参对象不被修改。

---

## 6. 项目结构

```
pyproject.toml                 # 打包与 console_scripts
requirements.txt / requirements-dev.txt
src/maskcompiler/
  errors.py                    # 异常层次（消息无数据）
  paths.py                     # JSONPath 子集解析器与匹配器
  transforms.py                # mask / redact / hash(HMAC) / encrypt(Fernet)
  keys.py                      # 本地密钥生成、0600 落盘、加载校验
  compiler.py                  # 严格校验 + 默认拒绝 + 静态冲突 + 优先级
  engine.py                    # 匹配、优先级合成、冲突/类型失败关闭
  logsafe.py                   # secret_scope + 日志脱敏过滤器
  service.py                   # stdlib HTTP 服务
  cli.py / __main__.py         # 命令行
examples/
  rules.json                   # 规则样例（覆盖全部 4 种原语与裁剪合成）
  document.json                # 请求样例（嵌套数组 + Unicode）
  curl-demo.sh                 # HTTP curl 演示
tests/                         # 89 个自动化测试
RUNLOG.md                      # 实际运行命令与结果的如实记录
```

## 7. 测试

`tests/` 共 89 个用例，覆盖（对应验收点）：

- **嵌套数组**：`$.orders[*].payments[*].number`、数组套数组、递归 `$..[*]`；
- **Unicode**：中文姓名按码点保留首尾、中文 HMAC/Fernet 往返；
- **缺失字段**：默认忽略 / `on_missing: error`，通配符下部分缺失；
- **规则冲突**：显式优先级覆盖、通配符 vs 精确规则、同优先级编译期/执行期冲突、
  redact 子树的高优先级裁剪；
- **原始值不进错误日志**：成功/失败路径下日志消息与 traceback 的脱敏断言、
  类型错误消息不含原值、HTTP 422 响应不含原值；
- 密钥文件权限/版本/长度校验、CLI 端到端、HTTP 端到端。

运行结果与命令见 [RUNLOG.md](RUNLOG.md)。
