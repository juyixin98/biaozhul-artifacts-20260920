# 最小披露记录导出（Minimal Disclosure Export, `mde`）

纯后端的**本地安全数据处理服务**：按“用途（purpose）”对导出记录执行**字段级**
允许 / 拒绝 / 泛化决策，策略**版本固定到导出任务**，并输出**可独立核验的字段决策记录**
（Ed25519 签名 + SHA-256 摘要 + 策略快照 + 全量重算）。

> **这不是匿名化工具。** 本系统实施的是字段级*披露策略*。每个导出包都显式声明：
> *This output is NOT anonymized and no anonymization guarantee is made.*
> 泛化、掩码、假名化哈希得到的值在与其他数据结合时仍可能识别个人。

## 明确的边界

- 密码原语全部来自 [`cryptography`](https://cryptography.io/)（Ed25519、Fernet/
  AES-128-CBC+HMAC、标准库 HMAC-SHA256）。**不自创任何加密/哈希算法**。
- 密钥全部**本地生成**、本地文件保存（私钥文件强制 `0600`）。**不连接任何生产
  账号 / KMS / 云服务**。
- 无前端。提供 Python 库、CLI 与仅监听 `127.0.0.1` 的 HTTP JSON API。
- 不保证对抗本地恶意进程、不提供网络鉴权/TLS（本地服务，由部署方负责）。

## 目录结构

```
src/mde/
  models.py        不可变策略版本、规则、别名、路径语法、策略指纹 SHA-256
  generalizers.py  redact/mask/email_mask/date_bucket/numeric_bucket/
                   hash(HMAC-SHA256)/category/replace，失败 fail closed
  engine.py        单次递归遍历：逐字段决策、别名规范化、数组 [] 处理、决策记录
  policy.py        版本化策略存储（内存 / JSON 文件），不可变版本 + 乐观锁
  crypto.py        Ed25519 签名、Fernet 信封加密、任务随机盐
  service.py       导出编排：版本快照、签名清单、加密包、三层核验
  api.py           127.0.0.1 HTTP JSON API（标准库 ThreadingHTTPServer）
  cli.py           keygen / policy / export / verify / serve
  canonical.py     确定性 JSON 编码（签名与摘要基于字节级确定序列）
examples/          策略、数据、请求样例与 curl 脚本
scripts/demo.py    进程内端到端演示
tests/             75 个自动化测试
```

## 安装

需要 Python ≥ 3.10。当前环境已带 `cryptography` 41.0.7；干净环境：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'        # 或 pip install -r requirements-dev.txt
```

不安装也可直接用：`export PYTHONPATH=src`。

## 快速开始（CLI）

```bash
# 1) 本地生成测试密钥（Ed25519 签名 + 可选 Fernet 加密）
PYTHONPATH=src python3 -m mde.cli keygen --dir keys --with-fernet

# 2) 发布不可变策略 v1
PYTHONPATH=src python3 -m mde.cli policy publish --store-dir ./state \
  --id hr-export --file examples/policy_hr.json

# 3) 按用途导出（策略版本固定；可显式 --version）
PYTHONPATH=src python3 -m mde.cli export --store-dir ./state \
  --data examples/record.json --policy-id hr-export \
  --purpose analytics --signing-key keys/signing_key.pem --out /tmp/bundle.json

# 4) 核验（提供原始数据时执行全量重算比对）
PYTHONPATH=src python3 -m mde.cli verify --store-dir ./state \
  --bundle /tmp/bundle.json --source-data examples/record.json
```

## 快速开始（HTTP API）

```bash
PYTHONPATH=src python3 -m mde.cli serve --store-dir ./state \
  --signing-key keys/signing_key.pem --host 127.0.0.1 --port 8080
# 然后：bash examples/curl_examples.sh
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 服务状态与验签公钥指纹 |
| GET  | `/policies` | 列出策略及全部版本号 |
| GET  | `/policies/<id>?version=N` | 读取**不可变**的指定版本 |
| POST | `/policies/<id>/publish?expected_version=N` | 发布新版本（乐观锁，冲突 409） |
| POST | `/v1/exports` | 导出；可固定 `version`、可 `encrypt` |
| POST | `/v1/exports/verify` | 签名/摘要/重算核验 |

请求样例见 `examples/request_*.json`。

## 策略与路径模型

策略文档示例（`examples/policy_hr.json`）：

```json
{
  "default_action": "deny",
  "aliases": [{"canonical": "email", "aliases": ["mail", "email_address"]}],
  "rules": [
    {"path": "full_name", "action": "allow"},
    {"path": "email", "action": "generalize", "generalizer": "email_mask"},
    {"path": "national_id", "action": "deny"},
    {"path": "contacts[].phone", "action": "generalize",
     "generalizer": "mask", "params": {"keep_suffix": 2}},
    {"path": "medical", "action": "deny"}
  ]
}
```

- **动作**：`allow` / `deny` / `generalize`（必须给出已注册的 `generalizer`）。
- **路径**：点分；数组一律写 `[]`（`contacts[].phone`、`matrix[][].v`、
  根数组 `[].id`）。规则里**不能**写具体下标——所有元素共享同一规则路径。
- **别名**：规则写规范名，数据里出现任意外号都会命中；决策记录同时记录
  `path`（规范名）与 `input_path`（原始键）。
- **默认动作**：默认 `deny`——任何未被规则覆盖的**标量字段**都不导出。
- **规则优先级**：用途专用规则 > 通配规则；精确（非递归）> 递归；路径更长优先。

### 嵌套结构不得旁路（fail closed）

- 容器无规则时是 `passthrough`：**逐子节点决策**，不存在“整体放行后内部免检”。
- `allow` 一个对象**不**等于允许其内部字段裸出（子节点仍独立评估）。
- `deny` 父节点则整个子树移除，子级任何 allow 都无法挽回。
- 未知字段默认拒绝；新增/拼错/注入的字段（如 `debug_trace_id`）不会泄露。
- 泛化器对类型不符等情况抛错时，该字段按 **deny** 处理，绝不回退原值。

### 策略版本固定与更新竞争

- 每次 `publish` 生成版本号 +1 的**不可变**版本，附 SHA-256 `fingerprint`；
  旧版本永不修改。
- 导出时把解析到的具体版本号、指纹与**策略全文快照**写入并签名进导出包。
  之后无论策略怎么更新，旧包都能用包内快照**原样复算**。
- 发布支持 `expected_version` 乐观并发控制：两人基于同一旧版本起草更新，
  后提交者得到 `409 PolicyConflict`（含 `current_version`），必须重读合并，
  不能静默覆盖。

## 可核验的字段决策记录

导出包（`mde-bundle/v1`）：

```
manifest        策略 id/版本/指纹、用途、输入/输出/决策记录 SHA-256、
                任务盐、统计、验签公钥、Ed25519 signature、非匿名化声明
policy_snapshot 本次任务实际使用的不可变策略版本全文
output          泛化后的记录
decisions[]     每个字段一条：path / input_path / decision / by / reason /
                matched_rule_path / output_value
```

`verify` 三层检查，任一失败即整体失败：

1. **manifest_signature**：Ed25519 验签（且公钥与清单记录一致）；
2. **output_hash / decisions_hash**：对包内容重算 SHA-256 与清单比对；
3. **recomputed_output / recomputed_decisions**：当提供原始数据时，用包内
   策略快照 + 用途 + 任务盐**重跑引擎**，要求输出与决策记录逐字节一致。

篡改输出、改写某条决策、换掉策略快照、用错误原始数据复算、用另一把密钥伪造
签名——均会被对应检查拦截（有自动化测试覆盖）。

加密导出（`mde-encrypted-bundle/v1`）把整个明文包封进 Fernet 信封（认证加密），
外层只留无敏感内容的头部；错误密钥或密文被篡改均解密失败。

## 泛化器

| 名称 | 作用 | 关键参数 |
| --- | --- | --- |
| `redact` | 值置 null | — |
| `mask` | 字符串遮罩 | `keep_prefix`/`keep_suffix`/`char` |
| `email_mask` | `a***@domain` | `keep_prefix` |
| `date_bucket` | 时间粗化 | `granularity`: year/month/day |
| `numeric_bucket` | 数值分桶标签 | `bins`（严格递增） |
| `hash` | **假名化** HMAC-SHA256（任务随机盐） | `length` |
| `category` | 按映射表归类 | `map`/`default` |
| `replace` | 固定常量 | `with` |

`hash` 是**假名化**而非匿名化：同一导出任务内可关联，跨任务（不同随机盐）不可
关联；盐随包保存以便复算，它不是秘密。

## 端到端演示与测试（本仓库实际执行结果）

```bash
python3 scripts/demo.py     # 覆盖别名/未知字段/数组/嵌套/竞争/核验/加密
python3 -m pytest -q        # 75 passed
```

本仓库开发时实际运行并记录的结果见 [`RUNLOG.md`](RUNLOG.md)，其中包含使用的
命令、通过情况与过程中出现并修复的问题，不掩饰任何未通过项。

## 已知限制（设计取舍，非缺陷）

- 规则按**路径前缀**匹配，不支持“任意深度同名键”通配（如拒绝所有叫 `secret`
  的字段）。需要这类策略时应显式列出路径。
- HTTP API 不内置鉴权且仅绑定回环地址；跨网络使用需部署方加 TLS/鉴权。
- API 的 `encrypt:true` 模式会把一次性 Fernet 密钥放在响应里返回，仅适用于
  本地受控通道；正式密钥分发不在本项目范围。
- 不评估数据的统计披露风险（例如小样本、差分攻击），这超出“字段披露策略”范畴，
  也是不做匿名化承诺的原因之一。
