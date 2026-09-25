# 运行记录（RUNLOG）

如实记录本项目在交付环境中的实际命令与结果。环境：

- OS：Linux 6.8.0-90-generic（Ubuntu 内核）
- Python：`Python 3.12.3`（`/usr/bin/python3`）
- 依赖：`cryptography 41.0.7`（系统已安装）、`pytest 9.1.1`（系统已安装）
- 未连接任何生产账号 / 云服务；所有密钥均为本地一次性测试密钥。

## 1. 自动化测试

命令：

```bash
python3 -m pytest -q
```

最终结果（在交付环境实际执行）：

```
75 passed in 4.42s
```

按文件分布：

| 测试文件 | 数量 | 覆盖验收点 |
| --- | --- | --- |
| test_paths.py | 6 | 路径语法、`[]` 规范化、非法路径拒绝、别名冲突、指纹绑定内容 |
| test_alias.py | 5 | **字段别名**：规范名/外号双向命中、嵌套与数组内别名、记录区分路径 |
| test_unknown_fields.py | 6 | **未知字段**默认拒绝、父容器放行不泄露子字段、数组新字段、默认 allow 下显式 deny |
| test_arrays.py | 7 | **数组**：`[]` 对所有元素一致、下标无法旁路、多维数组、子树拒绝、标量数组 fail closed |
| test_nesting.py | 8 | **嵌套不得旁路**：深层未知拒绝、allow 父不放开子、deny 父整树移除、递归规则优先级、用途规则优先级 |
| test_generalizers.py | 9 | 8 种泛化器确定性、类型校验、失败 fail closed、盐的任务隔离 |
| test_policy_concurrency.py | 6 | **策略更新竞争**：不可变版本、乐观锁 409、版本固定到任务、20 线程并发不重号、文件存储恢复 |
| test_decision_records.py | 12 | **可核验记录**：签名、摘要、全量重算、4 类篡改/伪造检测、加密包正确/错误密钥、非匿名化声明 |
| test_canonical_and_edges.py | 8 | 确定性 JSON、null/根数组/标量根/非法类型等边界 |
| test_api.py | 6 | HTTP API 真实套接字端到端（发布/导出/核验/409/版本固定/加密） |
| test_cli.py | 2 | CLI 子进程端到端（keygen 权限 0600、明文+加密导出、篡改退出码 3） |

## 2. 端到端演示

命令：

```bash
python3 scripts/demo.py
```

实际结果要点（完整输出已在开发时打印并人工核对）：

- 策略 `hr-export` v1 发布成功，12 条规则，带指纹；
- 导出统计：`allow=4 deny=5 generalize=8 passthrough=3`，
  其中 `denied_by_default=2`（数组元素里的 `internal_note` 与根级
  `debug_trace_id` 这两个未知字段被默认拒绝）；
- 别名生效：输入键 `mail` 按规范名 `email` 掩码为 `z***@example.com`，
  `ssn` 按规范名 `national_id` 被拒绝；
- `medical` 整个子树（含 `diagnosis`）被拒绝；`salary`、未知字段不在输出中；
- 核验 7 项全部 `PASS`：签名、策略快照、输出哈希、决策哈希、重算输出、
  重算决策、非匿名化声明；
- 篡改输出字段名后被 `output_hash` 检查拦截；
- 策略竞争：基于 v1 的更新成功产生 v2；另一个仍带 `expected_version=1` 的
  提交被拒绝：
  `policy 'hr-export' version conflict: expected 1, current 2`；
- 旧导出包用包内 v1 快照重算仍 `ok=True`；新导出按 v2 执行，邮箱字段消失；
- Fernet 加密包外层不含 `Zhang San` 等明文；正确密钥核验通过，错误密钥报
  `decryption failed: invalid key or tampered ciphertext`。

产物目录：`demo_out/`（测试签名私钥、策略 JSON、导出包）。

## 3. HTTP API 实测

以临时空闲端口启动真实服务（首次尝试的 18080 端口被机器上无关进程
`vccsim` 占用，报错 `OSError: [Errno 98] Address already in use`，
改用随机空闲端口后正常——这是环境端口冲突，非代码缺陷）：

```
GET  /health                                       -> 200 {"status":"ok", ...}
POST /policies/hr-export/publish                   -> 201 version=1, rules=12
POST /v1/exports                                   -> 201
POST /v1/exports/verify                            -> 200 ok=True（7 项检查）
POST /policies/hr-export/publish?expected_version=2 -> 201（产生 v3）
POST /policies/hr-export/publish?expected_version=1 -> 409
     {"error":"... version conflict: expected 1, current 3",
      "policy_id":"hr-export","current_version":3}
```

## 4. 开发过程中出现并已修复的问题（不掩饰）

下列问题均在开发自测中由自动化测试或冒烟发现，已修复并有回归测试：

1. `parse_path` 无法处理 `[].` 之后的段、错误拒绝合法的 `[][].v` 多维数组、
   未拒绝孤立 `]`；`render_path` 在 `[]` 后多加了点号。已修复并加
   往返/非法路径测试。
2. 引擎递归时**从未计算子节点的规范化路径**，导致别名规则和祖先递归规则在
   嵌套层级失效（核心缺陷）。已修复为遍历时同时传递 raw/canonical 两条路径。
3. 根容器被当成字段产生空路径决策；空容器（所有子字段均被拒绝）一度仍以
   `{}`/`[{}]` 出现在输出中，泄露结构信息。已修复：根不记录，非根空容器剪枝。
4. 重构统计/记录回调时曾引入一处自递归与语法错误，立即由测试运行发现并修复。
5. `FilePolicyStore` 的文件名校验写法错误，把含 `-` 的合法 policy_id
   （如 `hr-export`）误判为不安全。已修正为允许字母数字与 `-_.`。
6. 测试本身的若干错误预期（默认 `deny` 下未加 allow 规则却期望 `keep`/`ok`
   等字段保留；篡改测试改到了一条本就是 allow 的记录）。已修正测试表达。

## 5. 未通过项 / 未做的事

- 截至交付，`python3 -m pytest -q` 为 **75 passed, 0 failed**，无遗留未通过项。
- **未实现/未承诺**：前端；生产账号与云 KMS 集成；网络层 TLS/鉴权；
  匿名化、差分隐私或任何重识别风险量化。这些在 README“已知限制”中明确列出。
