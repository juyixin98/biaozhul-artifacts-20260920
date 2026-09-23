# 运行记录（RUNLOG）

本文件记录在开发机上**实际执行**的命令与结果。除一处环境端口占用（已换端口重试，
非项目缺陷）外，全部测试与验收场景均通过。无生产账号，全部密钥本地随机生成。

- 日期：2026-09-24
- 系统：Linux 6.8.0-90-generic (Ubuntu)，Python 3.12.3
- 依赖：cryptography 41.0.7（AES-256-GCM 由 OpenSSL 后端提供）、pytest 9.1.1
- 工作目录：`/home/admin/Downloads/biaozhul/opp91/a`

## 1. 自动化测试

命令：

```bash
python3 -m pytest -q
```

结果：

```
...................................................................                                      [100%]
67 passed in 2.86s
```

按文件分布（`python -m pytest -v` 计数）：

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_crypto.py` | GCM 往返、错误密钥/AAD 失败、同 key+nonce 确定性（固化 nonce 不可复用的认知）、块 nonce 格式与文件内唯一性、计数器越界、三类 AAD 上下文（wrap/header/chunk）字段绑定 |
| `tests/test_container.py` | 空文件、10 种尺寸分块边界（含 4095/4096/4097、65535/65536/65537）、块数/末块长度、文件往返且无临时文件残留、密文中不含明文、文件内/跨文件 nonce 唯一、翻转密文字节、跨文件换块、文件内换块、只换 nonce、头部 length/chunk_size/file_id 篡改、两文件互换 wrapped_dek、magic/版本、尾部多余字节、4 种截断、磁盘截断无明文产物、删除旧 MK 后不可解、异环 kid 缺失与同 kid 异密钥两种失败 |
| `tests/test_rotation.py` | 轮换只重包 DEK（file_id/长度/块数/nonce 前缀不变、**块区字节逐字节相同**）、新 MK 可解且旧容器仍可由 retired 旧 MK 解、幂等 skipped、连续两代轮换链、**临时文件写好后模拟崩溃**（原文件 kid 不变、.rot.tmp 清理、可解、重跑成功）、`rotate_many` 部分跳过且整体可重入、块篡改后轮换再解密必失败、头被篡改时拒绝轮换、旧 MK 已删除时拒绝轮换 |
| `tests/test_keyring.py` | 初始化 0600 权限、exist_ok、轮换后旧转 retired 仍可取用、kid 唯一、禁止删 active、删除 retired、缺失/损坏 JSON/坏结构/坏密钥长度检测 |
| `tests/test_cli.py` | 子进程真实跑 CLI：初始化→加密→inspect→主密钥轮换→文件轮换→解密比对→幂等跳过→keyring-ls；篡改文件 CLI 退出码 1 且无明文/无 .part |
| `tests/test_server.py` | HTTP 业务函数全链路（加解密、主密钥+文件两级轮换、幂等、篡改 400、坏 base64/缺字段、密钥列表）；真实 socket 集成：health、无/错 token 401、正常加解密、未知路由 404 |

## 2. CLI 真机演示（临时目录 `/tmp/envenc-demo`，100000 字节随机明文，4096 字节/块 → 25 块）

### 2.1 初始化 / 加密 / 头部

```text
$ python3 -m envelope_enc.cli keyring-init -k keys.json
已初始化密钥环 keys.json
active 主密钥：mk-0001-b4a02329eb（created_at=2026-09-23T17:10:39Z）

$ python3 -m envelope_enc.cli encrypt -k keys.json -i secret.bin -o secret.bin.enc -c 4096
已加密：secret.bin -> secret.bin.enc（kid=mk-0001-b4a02329eb）

$ python3 -m envelope_enc.cli inspect secret.bin.enc
v               1
file_id         f-dbe12c216dcc5079
kid             mk-0001-b4a02329eb
chunk_size      4096
plaintext_size  100000
chunk_count     25

$ stat -c '%a %n' keys.json secret.bin.enc
600 keys.json
600 secret.bin.enc
```

正常解密 `cmp secret.bin secret.dec.bin` → `明文一致 OK`。

### 2.2 验收：篡改块 / 截断 / 交换文件块 / 块换位

```text
# 翻转第 24 块附近一个密文字节
错误：第 24 块认证失败：块被篡改、块顺序被调换或来自其他文件
退出码=1
无任何明文产物 OK        （ls 确认无 bad1.out / bad1.out.part）

# 砍掉容器末尾 10 字节（截断）
错误：容器截断：读取第 24 块密文需要 1712 字节，实际只有 1702 字节
退出码=1

# 把另一个文件 other.bin.enc 的第 0 块（nonce+密文，4124B）覆盖本文件第 0 块
错误：第 0 块认证失败：块被篡改、块顺序被调换或来自其他文件
退出码=1

# 同一文件内交换第 0、1 块（20000 字节、4096/块）
错误：第 0 块认证失败：块被篡改、块顺序被调换或来自其他文件
退出码=1
```

（跨文件换块失败来自块 AAD 中的 `file_id`；文件内换块失败来自 `chunk_index`。）

### 2.3 验收：主密钥轮换只重包 DEK

```text
$ python3 -m envelope_enc.cli rotate-master -k keys.json
主密钥已轮换：mk-0001-b4a02329eb -> mk-0002-443f9a08fb
旧主密钥 mk-0001-... 已置为 retired（仍可解密/轮换旧文件）

# 轮换前后分别计算"块区"（去掉 magic/版本/长度/头部 JSON 之后）sha256：
kid    : mk-0001-b4a02329eb            ->  mk-0002-443f9a08fb
块区 sha256: d06e5331...5c6e3bd        ->  d06e5331...5c6e3bd   （完全一致）

$ python3 -m envelope_enc.cli rotate -k keys.json secret.bin.enc other.bin.enc
已轮换：secret.bin.enc  mk-0001-b4a02329eb -> mk-0002-443f9a08fb
已轮换：other.bin.enc   mk-0001-b4a02329eb -> mk-0002-443f9a08fb

# 用新主密钥解密
轮换后明文一致 OK

# 再次执行（幂等）
跳过（已是 mk-0002-443f9a08fb）：secret.bin.enc
```

### 2.4 验收：轮换中断

先把主密钥再轮换一代（MK3 active，文件仍为 MK2，制造真实"待轮换"状态），
用测试钩子 `crash_after="tmp_written"` 在临时文件 fsync 后、`os.replace` 前模拟进程被杀：

```text
主密钥已轮换：mk-0002-443f9a08fb -> mk-0003-4467097573
模拟崩溃：临时文件已 fsync，os.replace 前被杀
kid: 崩溃前=mk-0002-443f9a08fb  崩溃后=mk-0002-443f9a08fb  (相同说明原文件未受影响)
.rot.tmp 已清理: True
已解密：secret.bin.enc -> crash2.dec
中断后原文件解密一致 OK
$ python3 -m envelope_enc.cli rotate -k keys.json secret.bin.enc
已轮换：secret.bin.enc  mk-0002-443f9a08fb -> mk-0003-4467097573
重跑轮换后解密一致 OK
```

结论：替换前崩溃 → 原文件保持旧 kid 完好、临时文件被清理；重跑即完成（操作可重入）。
`os.replace` 本身原子，不存在"半个轮换"的结果文件。

## 3. HTTP 接口真机演示（真实 socket，curl + Python urllib）

```text
# 无 Token
$ curl http://127.0.0.1:<port>/keys
{"error": "未授权：需要 Authorization: Bearer <token>"} [401]

$ curl http://127.0.0.1:<port>/health
{"status": "ok"}

加密: kid = mk-0003-4467097573 file_id = f-991e9dfe856de818
主密钥轮换: mk-0003-4467097573 -> mk-0004-286570cdb2
文件轮换: mk-0003-4467097573 -> mk-0004-286570cdb2 skipped = False
解密明文一致 = True
篡改密文 -> HTTP 400 第 0 块认证失败：块被篡改、块顺序被调换或来自其他文件
```

## 4. nonce 唯一性与明文临时文件（结论 + 对应测试）

- nonce：`tests/test_container.py::test_nonces_unique_within_file` 解析容器，
  断言 4 个块 nonce 的前缀全部等于文件头随机前缀、计数器恰为 0,1,2,3、互不重复，
  且块区恰好读完；`test_nonces_unique_across_files` 断言两个独立文件随机前缀与
  file_id 不同。叠加"每文件独立随机 DEK"，即使跨文件前缀碰撞也处于不同密钥作用域。
- 明文临时文件：`test_file_roundtrip_and_no_plaintext_temp_left` 断言加密后无
  `.enc.tmp`、解密后无 `.part`；`test_file_truncated_on_disk_fails_and_no_plaintext_output`
  对磁盘上的截断容器解密，断言抛出 `TruncatedContainer` 且目标明文与 `.part` 都不存在；
  `test_encrypted_file_has_no_plaintext_inside` 断言密文容器中不出现完整明文字节串。

## 5. 如实记录的问题

1. **首次 HTTP 演示时端口 18080 被本机其它进程占用**（`OSError: [Errno 98] Address
   already in use`），演示服务未能启动，curl 打到了占用该端口的无关进程（/keys 返 404）。
   这是环境问题，非本项目缺陷；改用由 OS 分配的空闲端口后，20–26 项全部符合预期。
2. 开发过程中自测发现并已修复的两个代码问题（修复后 67 项全绿）：
   - `Keyring.initialize` 误把 `(kid, entry)` 元组放进集合字面量导致
     `TypeError: unhashable type: 'dict'`；
   - CLI 的 `-k` 原先挂在主解析器上只能放在子命令前，改为各子命令参数后
     `cli encrypt -k ...` 这类自然写法可用。
3. 未通过项：**无**（最终 `67 passed`；上述均为修复前的历史问题，已不复现）。

## 6. 复现实验的最小命令集

```bash
pip install -r requirements.txt
python3 -m pytest -q

KR=$(mktemp -d)/keys.json
python3 -m envelope_enc.cli keyring-init -k "$KR"
head -c 100000 /dev/urandom > /tmp/plain.bin
python3 -m envelope_enc.cli encrypt -k "$KR" -i /tmp/plain.bin -o /tmp/plain.enc -c 4096
python3 -m envelope_enc.cli rotate-master -k "$KR"
python3 -m envelope_enc.cli rotate -k "$KR" /tmp/plain.enc
python3 -m envelope_enc.cli decrypt -k "$KR" -i /tmp/plain.enc -o /tmp/plain.out
cmp /tmp/plain.bin /tmp/plain.out && echo OK
```
