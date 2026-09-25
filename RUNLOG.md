# 运行记录（RUNLOG）

本文件如实记录开发完成后的实际运行情况。运行机器：Linux 6.8.0-90-generic，
Python 3.12.3，cryptography 41.0.7，pytest 9.1.1。时间：2026-09-24（UTC+8）。

## 1. 自动化测试

命令：

```bash
python3 -m pytest tests/
```

结果：**46 passed in ~1.2s，0 失败、0 跳过。**

覆盖文件：

| 文件 | 用例数 | 内容 |
|---|---|---|
| tests/test_paths.py | 6 | 百分号编码、字面点号键与嵌套路径区分、`[]` 通配、实例下标 |
| tests/test_transforms.py | 7 | 5 个泛化原语、参数校验、目的/策略绑定、bool 拒绝 |
| tests/test_policy.py | 8 | 策略校验、前缀互斥、重复路径、别名规则、指纹稳定性 |
| tests/test_engine.py | 12 | 别名/未知字段/数组/形状混淆/深层嵌套/用途隔离/泛化失败等 |
| tests/test_exporter.py | 8 | 导出包、端到端重放、篡改、伪造签名、版本竞争、不可变性 |
| tests/test_keys_signing.py | 3 | 本地密钥 0600 往返、Ed25519 正负例、损坏密钥文件 |
| tests/test_api.py | 2 | HTTP 全流程（发布/版本列表/导出/复验/端到端）、非法输入 |

## 2. CLI 端到端实跑（全新临时目录 /tmp/mde-fresh）

以下为实际命令与结果（完整输出在开发会话中留存，下方为摘要，数值可复现）。

1. **发布策略 v1**
   `cli publish --policy examples/policy.json`
   → 首次运行自动生成 0600 本地测试密钥；
   `policy_fingerprint=26a22fe8e52c10c5d34fe0d6e42d9e812fef7ab73d9b74a00ac63b49cd908d17`
   （规范化 JSON 的 SHA-256，内容不变则指纹跨运行稳定）。

2. **按 analytics 导出** examples/records.json
   `cli export --purpose analytics --policy-fingerprint <fp> -o pkg.json`
   → 输出 2 条最小披露记录，53 条决策。验收字段实际表现：
   - `ssn`、`security` 子树、`password_hash`：**deny / exact_rule**
   - 字面键 `"user.id"`：路径编码为 `user%2eid`，**default_deny**，未匹配任何嵌套规则
   - 注入别名 `e_mail`：按其目标 `email` 处理为 `attacker.example`，
     决策记录 `via_alias=e_mail, rule_path=email`
   - 未知字段 `unknown_field`、`orders[0].internal_memo`：**default_deny**
   - `email` → 域名；`signup_date` → 截断到月；`orders[].placed_at` → 截断到日；
     `name` → 目的绑定 HMAC 假名；`tags[]` / `orders[].*` 按数组通配逐元素处理
   - 空数组：策略已知的 `tags: []`、`orders: []` 保留；未知容器整体拒绝
     （reason=`empty_unknown_container_denied`）

3. **公开复验**（仅包，不带外公钥）
   `cli verify --package pkg.json`
   → `overall_passed: true`，6 项检查全过；报告**带警告**：未提供验签公钥时
   使用包内嵌公钥只能检测意外损坏、不能识别伪造包（退出码 0）。

4. **端到端复验**（原始记录 + 本地密钥）
   `cli verify --package pkg.json --records examples/records.json --local-keys`
   → `overall_passed: true`，9 项全过，含 `records_digest`、
   `replay_output`、`replay_decisions`（用包内快照重放引擎，逐字节一致）。

5. **篡改检测**：向输出注入 `output[0].ssn="INJECTED"` 后复验
   → `overall_passed: false`，三项同时失败：
   `ed25519_signature`、`output_digest`、
   `decision_output_consistency`（"ssn: 决策为 deny，但该路径在输出中仍有值"）。
   CLI 退出码：**篡改包 1，正常包 0**（已单独确认，管道中的退出码是解析脚本的）。

6. **非法输入拒绝**
   - 同用途前缀重叠策略（`profile` + `profile.name`）：发布失败，退出码 2，
     错误信息明确指向"前缀包含关系"。
   - 不存在的用途 `nope`：导出失败，退出码 2，并列出可用用途。

7. **策略更新 / 版本固定**
   - 发布 `examples/policy-v2.json`（revision=2；tags 改拒绝、email 改假名、
     日期改按年）→ 新指纹 `4a3b2fe8…db04f`。
   - 同一批记录分别按 v1/v2 指纹导出：
     v1 邮箱=`example.com`、tags 保留；v2 邮箱=`hmac:…`、tags 不出现。
   - v1 已导出包的指纹、内嵌快照、签名、复验结果**均不因 v2 发布而改变**。

8. **HTTP 服务实跑**：`examples/curl-demo.sh`（启动 127.0.0.1:8390、
   发布、导出、复验）跑通，`/health`、`/policies`、`/exports`、`/verify`
   均返回预期结果；进程结束即停止，仅绑定回环地址。

## 3. 开发过程中出现并已修复的问题（如实记录）

这些是开发自测时发现、在最终代码中已修复的缺陷，最终测试套件中均有回归用例：

1. `join_tokens` 在数组下标后漏拼点号（`orders[2]items[0]sku`）→ 已修，
   有 `test_instance_rendering` 覆盖。
2. 百分号编码大小写不统一（`%2E` vs `%2e`）→ 统一小写并固定测试。
3. 非法日期在 `date_trunc` 中抛裸 `ValueError` 而非受控 TransformError
   → 已包装为 fail-closed 的泛化失败。
4. 实例路径（决策记录里的 `orders[0].id`）最初无法被复验器解析
   → `split_path(allow_indices=True)` 区分规则形状路径与实例路径。
5. generalize 成功的决策最初没写 `output_digest`，导致公开复验一致性检查失败
   → 已补齐，复验对 allow/generalize 都重算输出摘要。
6. 策略仓库内存缓存会短路磁盘读取，使得"篡改磁盘策略文件"检测失效
   → `get()` 改为文件为真相来源、每次读盘复核指纹（有不可变性测试）。
7. API 集成测试最初把"构造 server"误当成"启动 serve_forever"导致挂起
   → 测试夹具显式在后台线程启动服务循环；`build_server` 与 `serve` 职责分离。

## 4. 未做的事 / 已知限制（不夸大）

- **不声称匿名化**：没有唯一性/ k-匿名 / 差分隐私等度量；输出可能被再识别。
  每个导出包内嵌 4 条免责声明。
- 无鉴权、无 TLS、无速率限制；HTTP 仅绑定 127.0.0.1，定位为本地工具。
- 决策记录里的输入摘要是 SHA-256（用于一致性/追责），**不是**对原始值的保密措施。
- 假名密钥本地明文保存（0600）；未接 KMS/HSM，按要求不接任何生产账号。
- 同用途"规则互不为前缀"是有意的严格约束：需要子树统一处置时按子树整体授权/拒绝，
  策略作者不能写父子混合动作（避免语义歧义），代价是策略需要显式枚举。
- 未做并发导出的性能压测；策略仓库的线程安全用锁保证正确性（版本固定语义
  由不可变指纹 + 快照内嵌保证，不依赖锁）。
