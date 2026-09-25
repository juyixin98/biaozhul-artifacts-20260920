# 运行报告（如实记录）

日期：2026-09-24
机器：Linux 6.8.0-90-generic（Ubuntu），Python 3.12.3

## 环境

系统 Python 自带 `cryptography 41.0.7`、`pytest 9.1.1`；
另用干净虚拟环境复验，venv 内安装到 `cryptography 50.0.1`，两套环境测试结果一致。

## 一、安装验证

```bash
$ python3 -m venv .venv && . .venv/bin/activate
$ pip install -e . pytest
$ python -c "import dms, cryptography; print(dms.__version__, cryptography.__version__)"
0.1.0 50.0.1
```

结果：成功。

## 二、自动化测试

```bash
$ python -m pytest -q
....................................................................... [100%]
71 passed in 3.17s
```

**结果：71 个测试全部通过，无跳过、无失败。**

测试文件与覆盖点：

| 文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `tests/test_paths.py` | 14 | 点号/方括号/下标/通配/递归下降解析、嵌套数组匹配、递归任意深度、缺失字段、等价路径规范化、非法输入拒绝 |
| `tests/test_rules.py` | 14 | 默认值、未知 action / 顶层字段 / 规则字段 / option 拒绝、参数类型（含 bool 不冒充 int）、六种 action、重复 id、编译期同路径冲突 |
| `tests/test_engine.py` | 27 | mask 保留末尾字符、Unicode 码点（中文/日文/emoji）、redact、drop 删键与数组元素、入参不被修改、嵌套数组、递归进数组、缺失字段静默与 require_match、同优先级冲突、显式优先级刺穿与祖先覆盖、同字段多规则、encrypt/decrypt 往返、SHA-256 确定性、无 provider 报错、无效密文不回显、非字符串 mask 不回显值、数字标量、统计结构、NaN 拒绝 |
| `tests/test_crypto_and_logging.py` | 6 | 密钥文件 0600、版本字段、幂等 load-or-create、坏密钥文件不 dump 内容、敏感值 scrub、FileHandler 落盘日志不含秘密且含 REDACTION 标记 |
| `tests/test_server.py` | 6 | /health、compile、mask、mask-compiled、未知规则 422 不回显值、冲突 422 不回显值、非法 JSON 400、require_match 422 |
| 其余 | 4 | 路径解析边界（根 `$`、根通配、越界下标等） |

> 表内分项为手工归类，合计以 pytest 统计的 71 为准。

## 三、开发过程中出现并已修复的失败（如实记录）

首轮 `pytest` 结果为 **6 failed, 65 passed**，问题与修复：

1. **路径解析器状态机缺陷**：bare key 消费后未正确推进游标，导致
   `foo..`（递归下降后缺段）未被拒绝；重写为显式的 `expect_segment`
   状态机后修复。补充了 `""`、`"   "`、`foo..`、`foo.[`、`items[-1]`、
   `$..`、`.`、`foo.bar.` 等拒绝用例。
2. **容器上规则合成语义**：最初实现把容器上的 `redact` 整体替换成常量，
   导致高优先级后代规则即便胜出也随容器一起被替换；重构为统一“规则流”
   模型——容器上的动作沿子树流到标量叶节点、保留结构，高优先级后代
   “刺穿”，同优先级不同规则重叠 fail-closed。
3. **递归 drop 哨兵泄漏**：无规则分支的容器推导式没有过滤内部 `_DROP`
   哨兵，导致 `$..drop_me` 时出现 `<object object ...>`；抽统一
   `_rebuild` 助手过滤后修复，并新增断言防止回归。
4. 两个测试自身的字符计数笔误（中文码点数、11 字符 mask 星号数），
   随语义确定一并改正。

修复后连续多次运行均为 71 passed。

## 四、CLI 实跑

```bash
$ dms keygen
已生成测试主密钥：.secrets/dms-test-key.json（权限 0600，请勿用于生产）
$ stat -c '%a' .secrets/dms-test-key.json
600
$ dms mask --rules examples/rules.json --doc examples/document.json
# exit=0；脚本断言：
#   users[0].phone == '*******5678'
#   users[1].phone == '***********0132'
#   三处 note（含递归到 meta.note）== '【已脱敏】'
#   ssn 为 64 位 SHA-256 hex；internal_tags 已删除
#   中文/英文名、emoji 文档完好；matched_fields == 10
```

错误路径：

```bash
$ dms mask --rules <(未知动作规则) --doc <(含 MY-SECRET-zzz 的文档)
# exit=2，stderr 为 {"error":{"code":"unknown_rule", ...}}，不含 MY-SECRET-zzz
$ dms mask --rules <(同优先级通配冲突规则) --doc <(含 MY-SECRET-qqq 的文档)
# exit=2，code=rule_conflict，details 仅含规则 id 与 trail，不含字段值
```

## 五、HTTP 服务实跑（127.0.0.1:8089）

实际执行并核验：

- `GET /health` → 200；
- `POST /v1/mask`（`examples/request_mask.json`）→ 200，手机号/SSN 正确脱敏，
  `internal_tags` 删除；
- `POST /v1/compile`（`examples/request_compile.json`）→ 200，返回规范化规则；
- 未知 action → 422 `unknown_rule`，响应体中 grep 不到 `TOPSECRET-12345`；
- 同优先级重叠 → 422 `rule_conflict`，grep 不到 `TOPSECRET-67890`；
- 加显式优先级后 → 200，`{"name":"***REDACTED***","secret":"****ef"}`；
- `require_match` 缺失 → 422 `missing_field`；
- encrypt → decrypt 往返还原 `我的秘密token-🔐`（Unicode + emoji）；
- 非法 JSON 请求体 → 400 `invalid_payload`。

日志泄漏检查：

```bash
$ grep -E "TOPSECRET|13812345678|abcdef" /tmp/dms-server.log
未发现任何原始敏感值
```

（服务访问日志只记录方法与路径模板，从不记录请求体；
另有 `SecretScrubFilter` 对登记过的标量值做落盘前抹除。）

## 六、未通过项 / 已知限制

- 无失败测试项；验收列出的四类场景（嵌套数组、Unicode、缺失字段、规则冲突）
  与“原始敏感值不进入错误日志”均有用例覆盖并通过。
- 已知限制（非缺陷，记录备查）：
  1. 密钥文件是**本地测试设施**，不含 KMS/HSM 接入，按需求刻意不接生产账号；
  2. 规则在容器上默认“流到叶节点并保留结构”，整体删除需显式用 `drop`；
  3. 请求体上限 1 MiB，文档嵌套深度上限 1000；
  4. `mask` 仅作用于字符串（对数字/布尔报 transform_error），`hash/encrypt/
     decrypt` 接受标量，容器需先指定到叶字段。
