# RUNLOG — 本机实际运行记录

- 日期：2026-09-24（UTC；部分命令输出时间戳显示 UTC 2026-09-23 晚间）
- 机器：Linux 6.8.0-90-generic，Python 3.12.3
- 依赖：cryptography 41.0.7（系统已装），pytest 9.1.1
- 原则：以下命令均在本机真实执行，输出为实际结果；无“模拟通过”项。

## 1. 环境检查

```
$ python3 --version
Python 3.12.3
$ python3 -c "import cryptography; print(cryptography.__version__)"
41.0.7
```

## 2. 自动化测试

命令：`python3 -m pytest -q`

结果：**86 passed in 3.92s**（0 失败、0 跳过）。

按文件分布：

| 文件 | 用例数 | 内容 |
|---|---|---|
| tests/test_canonical.py | 16 | 字段重排、重复键、BOM、NaN、尾随字节、非 ASCII、浮点/整数、孤立代理、往返稳定 |
| tests/test_safepaths.py | 25（含参数化） | 17 组词法穿越/合法路径、文件与目录软链逃逸、FIFO、枚举排序 |
| tests/test_keys.py | 9 | Ed25519 往返、签名/消息篡改、key_id 确定性、信任库各类异常 |
| tests/test_sign_verify.py | 17 | 全部验收攻击/故障场景的端到端验证 |
| tests/test_cli.py | 6 | CLI 全流程、退出码、run 在篡改时被阻断 |
| tests/test_service.py | 7 | HTTP 健康/签名/验证/篡改/穿越/坏 JSON/禁用签名 |
| 合计 | 86 | 全部通过 |

开发过程中出现并修复的真实失败（修复后全部通过）：

1. `sam/cli.py` 误删仍在使用的 `public_key_id` import → keygen 子命令 NameError（6 个 CLI 用例失败），已恢复 import；
2. 一个测试误把 `files` 数组反序当作“字段重排”期望验签通过；数组是有序的，
   反序理应使签名失效。已改为：对象键重排→通过；数组改序→失败（新增
   `test_file_array_reorder_breaks_signature`）；
3. 子进程输出用 `capsys` 捕获不到（fd 级输出），改用 `capfd`；
4. 两处测试夹具自身问题：未创建制品根目录、给 `sign` 传 `../escape` entrypoint
   （签名端本就应拒绝，改为在“已签名封套”上篡改后验证）；
5. 篡改场景中 `UNSAFE_PATH` 对同一路径重复报告两次 → 去重（结构阶段不再
   重复文件路径检查，统一由逐文件阶段报告）。

## 3. CLI 端到端（手工实际执行）

### 3.1 keygen / sign

```
$ python3 -m sam keygen --private-key demo/keys/test-key.pem --public-key demo/trust/test-key.pem
已生成 Ed25519 测试密钥
  私钥(0600): demo/keys/test-key.pem
  公钥      : demo/trust/test-key.pem
  key_id    : 1b705b898e76cd1611583343c620d7a1ff9b5c14a463171ba4b202abc9933fdc

$ python3 -m sam sign --artifact-root demo/artifact --private-key demo/keys/test-key.pem \
    --output demo/envelope.sam.json --entrypoint bin/app.py
已对 2 个文件签名 -> demo/envelope.sam.json
```

私钥文件权限实际为 `0600`（`ls -l` 与测试双重确认）。

### 3.2 verify（正常）→ exit 0

```
验证通过: 2 个文件, key_id=1b705b898e76cd1611583343c620d7a1ff9b5c14a463171ba4b202abc9933fdc
  entrypoint: bin/app.sh
exit=0
```

### 3.3 run（先验证后执行）→ exit 0，制品真实运行

```
$ python3 -m sam run ... --python -- hello-from-run
Hello from the signed artifact
args: ['hello-from-run']
python: 3.12.3
exit=0
```

### 3.4 验收攻击/故障场景（每个场景独立临时副本）

| 场景 | 实际报告的问题码 | 退出码 |
|---|---|---|
| A. 文件内容追加篡改 | `SIZE_MISMATCH` + `DIGEST_MISMATCH` (data/message.txt) | 2 |
| B. 清单内文件被删 | `FILE_MISSING` (data/message.txt) | 2 |
| C. 目录混入未签名文件 evil.sh | `EXTRA_FILE` (evil.sh) | 2 |
| D. 信任库换成另一把密钥 | `UNKNOWN_KEY` | 2 |
| E. 篡改封套 path=`../../etc/passwd` | `UNSAFE_PATH`（另因签名失效报 `SIGNATURE_INVALID`，以及连带的 EXTRA_FILE / ENTRYPOINT_INVALID） | 2 |
| F. 制品内软链 → /etc/hostname，签名端枚举 | **签名端直接拒绝**：`路径 'escape-link' 解析后 (/etc/hostname) 逃逸出制品根` | 3 |
| G. 篡改后执行 run | `[SIZE_MISMATCH] ... (data/message.txt)`，**制品无任何输出，未执行** | 3 |
| H. 封套 JSON 制造重复键 | `ENVELOPE_MALFORMED: 对象包含重复的键: 'sam_version'` | 2 |
| I. 重排封套/清单对象键 + 改缩进 | 验证**通过**（键序/空白对规范字节不可见） | 0 |
| J. 调换 files 数组顺序 | `SIGNATURE_INVALID` | 2 |

场景 E 的完整输出（纵深防御：一次暴露多重问题）：

```
验证失败: 共 5 个问题
  - UNSAFE_PATH [../../etc/passwd]: 路径包含空段、'.' 或 '..' (目录逃逸): '../../etc/passwd'
  - SIGNATURE_INVALID: Ed25519 签名校验失败 (key_id=1b70...)
  - EXTRA_FILE [bin/app.py]: 文件存在于制品目录但不在已签名清单中
  - ENTRYPOINT_INVALID [bin/app.py]: entrypoint 不在已签名 files 清单中
```

## 4. 本地 HTTP 服务（手工实际执行）

启动：`python3 -m sam.service --host 127.0.0.1 --port 18088 --signing-key demo/keys/test-key.pem`

| 请求 | 实际结果 |
|---|---|
| `GET /healthz` | `{"ok": true, "signing_enabled": true}` |
| `POST /v1/sign`（2 个内联文件 + entrypoint） | 200，返回封套，signature base64 长度 88 |
| `POST /v1/verify`（原始内容） | 200，`verify.ok = true`，2 个文件 |
| `POST /v1/verify`（message.txt 内容改为 TAMPERED） | 200，`ok=false`，问题码 `SIZE_MISMATCH`、`DIGEST_MISMATCH` |
| `POST /v1/verify`（files 含 `../escape.txt`） | **400**，`路径包含空段、'.' 或 '..' (目录逃逸): '../escape.txt'` |
| `POST /v1/verify`（trusted_public_keys 为空数组） | **400**，`trusted_public_keys 必须是非空 PEM(base64) 数组` |
| 不配置 --signing-key 时 POST /v1/sign（单测） | **403**，`/v1/sign 已禁用` |

## 5. 样例脚本

`bash examples/cli-commands.sh` 实际跑通（临时目录中 keygen→sign→verify→
verify --json→run），最后一行真实输出：

```
hello ['demo-arg']
```

## 6. 未通过项 / 已知缺口（如实列出）

- **无失败测试、无跳过测试**：86/86 通过。
- 手工验证仅限 Linux/POSIX；Windows 行为（反斜杠策略、可执行位、软链 API）
  未在该平台实际运行，仅有代码层面的防御。
- 未做性能/超大制品压测；HTTP 请求体硬上限 32 MiB（针对“内联文件”的演示形态，
  大制品应走 CLI/Unix 域套接字等形态，当前未实现）。
- HTTP 服务无鉴权/无 TLS（设计上仅绑定回环）；没有速率限制。
- 未实现密钥轮换/撤销、时间戳服务（威胁模型中明确列为不覆盖）。

## 7. 复现步骤汇总

```bash
pip install -r requirements.txt
python3 -m pytest -q                      # 86 passed
bash examples/cli-commands.sh             # CLI 全流程演示
python3 -m sam.service --port 18088 \
  --signing-key demo/keys/test-key.pem    # 再配合 examples/http/curl.sh
```
