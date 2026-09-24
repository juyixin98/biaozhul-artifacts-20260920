# 运行记录（RUNLOG）

本文件如实记录交付时在本机实际执行的命令与结果。

- 日期：2026-09-24
- 系统：Linux 6.8.0-90-generic (Ubuntu)，Python 3.12.3
- 虚拟环境：`.venv/`（仓库内新建）

## 1. 依赖安装

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi 'uvicorn[standard]' cryptography pytest httpx python-multipart
```

实测版本（完整锁定见 `requirements.lock.txt`）：

```
fastapi 0.141.1
cryptography 50.0.1
pytest 9.1.1
httpx 0.28.1
python-multipart 0.0.32
uvicorn 0.53.0
```

## 2. 自动化测试

```bash
.venv/bin/python -m pytest tests/
```

结果：

```
tests/test_api.py .........                    [ 19%]
tests/test_crypto.py ..........                [ 41%]
tests/test_validator.py ........................... [100%]
46 passed, 1 warning in 0.84s
```

唯一警告来自第三方：`starlette.testclient` 提示未来将改用 `httpx2`，
不影响功能与测试结果。

46 个用例覆盖（名称为 pytest 实测收集到的用例）：

- `test_crypto`（10）：规范化 JSON 确定性/拒绝浮点、Ed25519 往返与篡改检测、
  keyid 计算、元数据类型/keyid 一致性/时区/sha256 绑定的结构校验。
- `test_validator`（27）：引导（合法/无签名/过期/重复/未引导更新）、
  合法 v1→v2→v3 链、下载出库再校验、
  回滚（版本倒退/同版本重放/首包非 v1）、
  冻结（过期 timestamp 拒绝 + 新合法版本恢复）、
  混搭（snapshot 哈希不符/版本交叉绑定矛盾/targets 字节哈希不符）、
  目标掉包、夹带未声明文件、签名损坏/缺失/篡改后不重签、
  root 轮换（合法 A→B/伪造缺旧签名/跳号/v2→v1 回滚）、
  **轮换中断原子性**（崩溃后状态字节不变、无 .tmp 残留、合法包可恢复）、
  状态文件损坏检测。
- `test_api`（9）：健康检查、完整 HTTP 升级链与下载、409 状态码、
  HTTP 层回滚/掉包错误码、root 轮换后旧密钥被拒/新密钥续升、reset、元数据端点。

## 3. 样例仓库生成

```bash
.venv/bin/python scripts/make_samples.py samples
```

实际输出：

```
样例仓库已生成: /home/admin/Downloads/biaozhul/text70/a/samples
  1-root                   accepted
  2-v1                     accepted
  3-v2                     accepted
  4-v3-rotated             accepted
  attack-rollback          rejected: error=rollback（B 合法签名的 v2 重放，版本低于当前 v3）
  attack-frozen            rejected: error=expired（B 合法签名的旧包，timestamp 已过期）
  attack-payload-swap      rejected: error=hash（元数据合法，目标文件 sha256 不符）
```

## 4. 真实 HTTP 服务端到端演示

启动：

```bash
UPDATER_DATA_DIR=data .venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8099
```

合法链（bootstrap → v1 → v2 → v3 同时完成 root 轮换）逐个返回 `200`，
`GET /api/state` 最终为：

```json
{"bootstrapped":true,
 "versions":{"root":2,"targets":3,"snapshot":3,"timestamp":3},
 "targets":["app.txt"]}
```

在该状态下依次提交三个攻击包，**全部被拒绝，状态未改变**：

```text
attack-rollback       HTTP 400
  {"accepted":false,"error":"rollback",
   "message":"timestamp: 试图回滚版本 3 -> 2"}

attack-frozen         HTTP 400
  {"accepted":false,"error":"expired",
   "message":"timestamp: 元数据已过期 (expires=2026-09-23T06:17:58Z)，拒绝使用"}

attack-payload-swap   HTTP 400
  {"accepted":false,"error":"hash",
   "message":"目标 app.txt: 长度 35 与声明的 36 不符"}
```

攻击后再次确认：状态仍为 root v2 / timestamp v3，
`GET /api/targets/app.txt` 仍下载到合法内容
`demo release v3 after root rotation`。

其它接口实测：

```text
GET /api/targets/nope.bin  -> 404 target_not_found
GET /api/metadata/timestamp -> 200，返回当前已接受的 timestamp 原始 JSON
POST /api/reset            -> {"status":"reset"}；随后 GET /api/state 为未引导
```

> 说明：三个攻击包的元数据均由“当前受信”的 B 密钥合法签名，
> 因此被拒原因确实是版本回退、过期与哈希绑定，而不是签名先失败
> （签名防线另有 8 个专门用例覆盖）。

## 5. 验收点对照

| 要求 | 落实位置 | 结论 |
|---|---|---|
| root/targets/snapshot/timestamp 各自签名 | `app/updater.py::_check_role_signatures`，root 阈值与 keyid 来自 root | 已完成，含阈值与轮换双签测试 |
| 检查版本（防回滚） | `validate_bundle` 步骤 1、5 | 已完成，含倒退/重放/跳号/首包版本测试 |
| 检查过期 | 步骤 4（先于版本检查） | 已完成，含冻结与恢复测试 |
| 哈希绑定 | 步骤 6、7 + 下载出库再校验 | 已完成，含跨角色混搭与文件掉包测试 |
| 本地信任状态原子提交 | `_atomic_write_text` + 内容寻址 + flock | 已完成，含提交前崩溃测试 |
| 混搭历史文件拒绝 | 跨角色 sha256/版本绑定 | 已完成 |
| 冻结旧时间戳拒绝 | expires 检查 | 已完成 |
| root 轮换中断拒绝回滚且合法下一版本可恢复 | root +1、双签、原子性、续升测试 | 已完成 |
| 源码、锁定依赖、自动化测试、README（启动命令与依赖） | 仓库根目录 | 已完成 |

## 6. 未完成项 / 未做的事

- 未实现 TUF 的 delegated targets（角色委派）与多镜像（mirrors）；
  snapshot 只绑定 `targets.json`。
- 未引入持久化的撤销列表（密钥更换通过 root 轮换解决）。
- 原子性/锁按 POSIX 设计（`os.replace`、`fcntl.flock`），未在 Windows 验证。
- 过期判断使用服务器 UTC 时钟；没有暴露请求级时间注入头
  （库接口支持注入时间，过期场景由单元测试覆盖）。
- `POST /api/reset` 无鉴权，仅用于演示/测试，未做生产级访问控制。
- 没有 UI（按要求纯后端），未做性能/压测。
- 未创建 git commit（按环境约定，未经要求不自动提交）。
