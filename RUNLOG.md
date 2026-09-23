# RUNLOG — 实际运行记录

本文件如实记录在开发机上实际执行的命令与结果。时间：2026-09-24。
开发机：Linux 6.8.0（Ubuntu），系统 Python 3.12.3（受 PEP 668 保护），
因此所有运行均在仓库内虚拟环境 `.venv` 中进行。

## 1. 环境准备

```
$ python3 --version
Python 3.12.3

$ python3 -m venv .venv
$ .venv/bin/pip install -e . pytest
（安装 cryptography 50.0.1 / pytest 9.x；pyproject 约束 cryptography>=41, pytest>=7）
```

系统 Python 自带的 cryptography 41.0.7 也满足约束；venv 中实际解析到 50.0.1。
未使用任何生产账号、外部服务或网络数据源（pip 安装来自本机构件源）。

## 2. 自动化测试（最终结果：全部通过）

```
$ .venv/bin/python -m pytest
collected 89 items

tests/test_cli.py ...                                                    [  3%]
tests/test_compiler.py .............                                     [ 17%]
tests/test_engine.py ....................                                [ 40%]
tests/test_keys.py ......                                                [ 47%]
tests/test_logsafety.py ....                                             [ 51%]
tests/test_paths.py ....................                                 [ 74%]
tests/test_service.py ........                                           [ 83%]
tests/test_transforms.py ...............                                 [100%]

============================== 89 passed in 4.77s ==============================
```

无跳过、无失败、无预期外告警。

覆盖的验收点（测试名均可在 `tests/` 中检索）：

| 验收点 | 对应用例（节选） |
|---|---|
| 嵌套数组 | `test_nested_arrays_and_keep_last`、`test_recursive_wildcard_over_arrays`、`test_recursive_wildcard_from_root_covers_root_array` |
| Unicode（码点计数） | `test_mask_unicode_chars_count_as_one`、`test_unicode_masking_and_hashing`、`test_encrypt_roundtrip` |
| 缺失字段 | `test_missing_field_ignored_by_default`、`test_missing_field_error_when_strict`、`test_missing_inside_wildcard_with_on_missing_error` |
| 规则冲突 / 优先级合成 | `test_higher_priority_wins_on_same_field`、`test_wildcard_vs_specific_rule_composes_by_priority`、`test_equal_priority_dynamic_conflict_raises`、`test_equal_priority_same_path_is_static_conflict`、`test_redact_subtree_with_higher_priority_inner_carve_out`、`test_redact_replace_mode_replaces_subtree_entirely` |
| 原始值不进错误日志 | `test_logged_original_value_is_redacted_on_success`、`test_type_mismatch_error_does_not_contain_value`、`test_conflict_traceback_in_logs_is_scrubbed`、`test_redaction_filter_handles_non_string_args`、HTTP 层 `test_type_mismatch_returns_422_without_value`、`test_missing_field_error_body_contains_rule_and_path_only` |
| 失败关闭 | `test_scalar_transform_on_container_raises`、`test_scalar_transform_on_number_raises`、`test_bool_is_not_accepted_as_string`、`test_null_passes_through` |
| 默认拒绝未知规则/参数 | `test_unknown_transform_rejected`、`test_unknown_top_level_key_rejected`、`test_unknown_rule_key_rejected`、`test_hash_bad_algo`（md5 拒绝）等 |
| 密钥安全 | `test_keygen_creates_0600_file`、`test_group_or_world_readable_keyfile_rejected`、`test_bad_version_rejected`、`test_short_hmac_key_rejected` |
| CLI / HTTP 端到端 | `tests/test_cli.py` 3 项、`tests/test_service.py` 8 项 |

## 3. CLI 实际运行

### 3.1 生成本地密钥并核对权限

```
$ .venv/bin/maskcompiler keygen --out /tmp/mc-final/keys.json
wrote /tmp/mc-final/keys.json (mode 0600)
$ stat -c "%a %n" /tmp/mc-final/keys.json
600 /tmp/mc-final/keys.json
```

### 3.2 编译（校验）样例规则集

```
$ .venv/bin/maskcompiler compile -r examples/rules.json
{
  "ok": true,
  "rules": [
    {"id": "audit-required-field", "priority": 50},
    {"id": "employee-pseudonym", "priority": 80},
    {"id": "internal-keep-contact", "priority": 200},
    {"id": "internal-memo-redact", "priority": 10},
    {"id": "pan-mask", "priority": 100},
    {"id": "phone-mask", "priority": 90},
    {"id": "secret-note-encrypt", "priority": 70}
  ]
}
```

### 3.3 对样例文档执行脱敏（节选实际输出）

```
$ .venv/bin/maskcompiler apply -r examples/rules.json \
      -d examples/document.json --key-file /tmp/mc-final/keys.json

$.orders 嵌套数组卡号:   "************1234" / "************2345" / "************3456"
$..phone 递归手机号:     "138****5678", "139****4321", "010*****5678"
staff_id HMAC-SHA256:    "b1e51d36a1852f4c...6af4d3"（同密钥下稳定，见下）
note Fernet:             "gAAAAABqtBSm...（每次不同，可解密还原）"
internal 子树:           {"budget": null, "contact": "***张三"}
                         （contact 被 priority=200 的规则从 redact 中裁剪保留）
audit_id:                null

report: {"transformed_locations": 12,
         "matched_rules": {"internal-keep-contact": 1, "pan-mask": 3,
           "phone-mask": 3, "employee-pseudonym": 2, "secret-note-encrypt": 1,
           "audit-required-field": 1, "internal-memo-redact": 1}}
```

### 3.4 密码原语性质验证（实际执行）

```
hash deterministic: True bf55a51e15e7124f...     # 同一明文+同一密钥 -> 同一 HMAC
fernet roundtrip: True                            # 中文明文 Fernet 解密还原成功
```

## 4. HTTP 服务实际运行

开发机的 8080/18080/18099 等固定端口被其他进程占用，因此端到端脚本绑定了由内核
分配的空闲端口（`build_server("127.0.0.1", 0, ...)`，`tests/test_service.py`
采用同一方式）。实际请求结果：

```
GET  /health                                   -> 200 {"status": "ok"}
POST /v1/rulesets (name=demo)                  -> 201
POST /v1/rulesets/demo/apply (样例文档)         -> 200
     phone: 138****5678
     pan:   ************1234
     note:  gAAAAABqtB...
POST /v1/rulesets/demo/apply ({"document":{"x":1}})
     -> 422 MissingFieldError
        msg: rule 'audit-required-field' on path $.audit_id matched no field
POST /v1/rulesets (重复创建同名)                -> 400 RuleSyntaxError
```

服务访问日志实际只输出方法与路径，无请求体（无敏感值）：

```
GET /health -> handled
POST /v1/rulesets -> handled
POST /v1/rulesets/demo/apply -> handled
request failed: MissingFieldError
POST /v1/rulesets/demo/apply -> handled
```

`examples/curl-demo.sh` 在固定端口可用的机器上可直接复现
（`BASE=http://host:port ./examples/curl-demo.sh`）。

## 5. 失败路径实测（默认拒绝 / 失败关闭，错误信息无原始值）

| 场景 | 命令输入 | 实际结果 |
|---|---|---|
| 未知 transform | `transform: "scramble"` | `UnknownTransformError: ... (supported: encrypt, hash, mask, redact)`，exit=2 |
| 类型不匹配 | 对数字 `987654321` 用 mask | `TypeMismatchError: rule 'n' at path $.n expects a string, got number`，exit=2；**消息中无 `987654321`** |
| 缺失字段严格模式 | 文档无 `$.absent` | `MissingFieldError: rule 'r' on path $.absent matched no field`，exit=2 |
| 同优先级精确冲突 | 两条 priority=5 规则打 `$.x` | `RuleConflictError: rules 'a' and 'b' have equal priority on the same path $.x`，exit=2 |
| 密钥文件权限过宽 | `chmod 644 keys.json` 后使用 | `KeyManagerError: refusing to use key file accessible to group/others (mode 0644); chmod 600 ...` |

## 6. 未通过项 / 已知边界

- 最终测试套件 **89/89 全部通过，无未通过项**。
- 开发过程中曾出现过的失败均已修复并回归（递归下降路径定位漏祖先步、
  `..[*]` 解析、规则排序误把路径特异度置于优先级之前、redact 子树覆盖语义、
  异常逃逸出 secret scope 后的日志脱敏）；对应行为现在都有自动化用例锁定。
- 已知边界（非缺陷，按需扩展）：
  - HTTP 服务为单进程内存态，规则集重启后需重新下发；无鉴权/TLS，设计为绑定
    `127.0.0.1` 的本地服务，不应直接暴露到网络；
  - 路径支持的是 JSONPath **子集**（见 README 语法表），不支持过滤器表达式
    `[?(@.x>1)]`、脚本表达式、并集 `[1,2]` 等；
  - `hash` 是假名化而非不可逆匿名化（持有相同密钥者可做字典比对），这是密钥化
    HMAC 的固有性质；需要更高强度假名化时应换用专门的 tokenization 方案；
  - 日志脱敏基于“原始标量子串替换”，理论上若非数据域字符串与某个敏感值完全相同
    也会被替换（只影响日志可读性，不影响正确性）。
