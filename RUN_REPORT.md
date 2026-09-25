# 运行报告（RUN_REPORT）

记录开发完成后的**实际**执行命令与结果。环境：

- OS：Linux 6.8.0-90-generic（Ubuntu 24.04）
- Python：3.12.3
- 依赖：cryptography 41.0.7（系统已安装，未联网安装）
- 日期：2026-09-24
- 全程离线、本地密钥，无任何生产账号/外部服务。

## 1. 自动化测试

命令：

```bash
python3 -m py_compile sig_manifest/*.py tests/*.py   # 编译检查
python3 -m unittest discover -s tests               # 全量测试
```

结果：**全部通过，0 失败，0 错误**。

```
Ran 87 tests in 6.330s

OK
```

分布：

| 测试文件 | 用例数 | 内容 |
|---|---|---|
| tests/test_canonjson.py | 21 | 字段重排规范化、重复键、浮点/NaN/BOM、NFC、键排序、转义、数组有序性 |
| tests/test_paths.py | 15 | `..`/绝对路径/反斜杠/盘符/`//`/`.`/控制字符、符号链接逃逸、悬空链接、枚举 |
| tests/test_keys.py | 10 | 密钥 ID、加密 PEM 与 0600 权限、错误口令、信任库 ID 重算/重复键/冲突 |
| tests/test_manifest_integration.py | 28 | 端到端：重排通过、重复键、未知密钥、坏签名、篡改、缺文件、穿越、执行门禁 |
| tests/test_server.py | 8 | 真实 loopback HTTP：healthz/sign/verify/run、422/400、穿越、重复键 |
| tests/test_cli.py | 5 | 真实子进程：keygen→sign→verify→run、篡改/未知密钥/链接逃逸下拒绝执行 |
| 合计 | **87** | |

## 2. 手工端到端演示（CLI）

### 2.1 正常流程：keygen → sign → verify → run

```text
$ python3 -m sig_manifest keygen --private-key demo/work/testkey.pem \
    --trust-store demo/work/trust.json --no-password
警告: 私钥未加密，请仅在本地测试环境使用。
已生成 Ed25519 私钥: demo/work/testkey.pem
公钥已加入信任库 : demo/work/trust.json
密钥 ID          : ed25519-sha256:542b22ca3aabf63421768ab2eb8b110189d2ae356af76476cda7ecdd36bd1abc
$ stat -c '%a' demo/work/testkey.pem
600

$ python3 -m sig_manifest sign --artifact-root demo/work/artifact --name demo-app \
    --private-key demo/work/testkey.pem --no-password \
    --entrypoint bin/hello.sh from-manifest --output demo/work/manifest.json
已生成清单: demo/work/manifest.json（文件 3 个，签名密钥 ed25519-sha256:542b…1abc）

$ python3 -m sig_manifest verify --artifact-root demo/work/artifact \
    --manifest demo/work/manifest.json --trust-store demo/work/trust.json
验证通过
  受信签名密钥: ed25519-sha256:542b22ca3aabf63421768ab2eb8b110189d2ae356af76476cda7ecdd36bd1abc
exit=0

$ python3 -m sig_manifest run ... --no-password cli-extra-arg
hello from signed artifact, args=from-manifest cli-extra-arg
exit=0
```

### 2.2 字段重排（验收点）

把 `signed` 与每个文件对象的键随机打乱、改成 5 空格缩进后：

```text
验证通过
  受信签名密钥: ed25519-sha256:542b…1abc
exit=0
```

### 2.3 五个攻击 / 异常场景（全部正确拒绝）

| 场景 | 命令/构造 | 实际结果 |
|---|---|---|
| 重复 JSON 键 | 插入 `"nonce":"ATTACK-DUP","nonce":…` | `[canonical_json_error] JSON 对象包含重复键: 'nonce'`，exit=1 |
| 路径穿越（持受信密钥签 `../../../../etc/passwd`） | 重签后 verify | `[unsafe_path] 路径不能包含 '..' 组件`，exit=1 |
| 文件缺失 | 删 `data.txt` 后 verify / run | `[file_error] 清单记录的文件缺失: data.txt`；run 在 spawn 前退出，exit=9 |
| 未知密钥 | 用未登记密钥签名后 verify / run | `[unknown_key] 签名密钥不在信任库中: …`；run exit=6，无制品输出 |
| 内容篡改 | 覆写 `data.txt` 后 verify / run | `[digest_mismatch] …`；run exit=8，无 `hello` 输出（未执行） |
| 符号链接逃逸 | 签名后把 `data.txt` 换成指向根外文件的软链 | `[unsafe_path] 路径逃逸出制品根目录 … resolved=…/outside.txt`，exit=1 |
| 签名值损坏 | 翻转签名首字节 | `[bad_signature] 签名值与规范化后的 signed 内容不匹配`，exit=1 |

一键脚本（可重复）：

```bash
bash examples/demo_local_flow.sh
# 末尾输出：verify exit=1（期望 1）；run exit=8（期望非 0，且没有 hello 输出）
```

## 3. HTTP 服务实测（curl）

先在端口 18080 启动时遇到 `OSError: [Errno 98] Address already in use`——
该端口被本机另一个服务占用（其响应为 Go 风格 `404 page not found`，并非本服务）。
换端口 18923 后全部正常：

```text
GET  /healthz                                  → 200 {"ok": true, "service": "sig-manifest"}
POST /verify   （合法清单）                     → 200 {"ok": true, "verified_key_ids": ["ed25519-sha256:542b…"]}
POST /run      （dry_run=true）                 → 200 {"ok": true, "dry_run": true,
                                                    "argv": ["bin/hello.sh","http-arg","x"], …}
POST /verify   （未知密钥签名的清单）            → 422 {"ok": false,
                                                    "errors":[{"code":"unknown_key", …}]}
```

（端口冲突属环境问题而非程序缺陷；程序已按原样如实记录报错。）

## 4. 开发过程中发现并修复的问题（如实记录）

1. `canonjson.encode` 输出数组结尾处一处引号笔误导致 `SyntaxError` —— 已修复并由测试覆盖。
2. 路径词法校验最初基于 `PurePosixPath.parts`，而它会折叠 `a//b`、`a/./b`，
   使空组件/`.` 漏网 —— 改为对原始字符串 `split('/')` 逐组件校验，并新增对应用例。
3. schema 最初把可选的 `entrypoint` 误设为必填（无入口清单无法验证）—— 已拆分为
   allowed/required 字段集。
4. 信任库加载时严格 JSON 抛出的 `CanonicalJSONError` 未在信任库边界转换为
   `KeyStoreError` —— 已包装，保持模块错误类型边界清晰。
5. HTTP `/verify` 最初对验证失败也返回 200（仅体体内 `ok:false`）—— 已改为
   验证失败返回 422，成功返回 200。
6. 规范化 JSON 的"字段重排"测试一度把 files **数组**也反序；数组顺序是签名载荷的
   一部分，反序理应产生不同字节 —— 修正测试预期（只重排对象键），并新增
   `test_array_order_is_significant` 固化该语义。

## 5. 未通过项 / 已知限制

- 自动化测试：**无未通过项**（87/87 通过）。
- 手工验收的五类场景（字段重排、重复键、路径穿越、文件缺失、未知密钥）
  以及"签名失败不得执行制品"均已实测符合预期。
- 已知非目标（见 README §12）：不含信任根带外分发、密钥撤销、时间戳权威、
  执行沙箱、TLS/鉴权与任何前端；HTTP 服务仅绑定 loopback。
