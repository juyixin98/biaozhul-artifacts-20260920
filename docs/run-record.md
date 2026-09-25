# 实际运行记录（run-record）

本文件如实记录本项目在交付环境中的实际命令与结果。所有输出文件保存在 `docs/` 下。

- 环境：Ubuntu（Linux 6.8.0-90-generic），Python **3.12.3**
- 依赖：系统环境 `cryptography 41.0.7`；另用全新 venv 安装最新 `cryptography 50.0.1` 复验
- 记录时间：2026-09-24

## 1. 自动化测试

命令：

```bash
./run_tests.sh
# 等价于 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

结果：**91 个测试全部通过（OK，0 失败 0 错误），约 1.9s**。完整输出见 `docs/test-output.txt`。

覆盖范围（5 个测试模块）：

| 测试文件 | 内容 |
|---|---|
| `tests/test_canonical.py` | 路径/查询串规范化：点段、`%64` 编码歧义、`%2f` 编码分隔符拒绝、双重编码 `%252f` 不折叠、控制字符、非 ASCII、空段折叠、查询键排序、重复键拒绝、`+` 字面量、fragment / absolute-form 拒绝 |
| `tests/test_signing.py` | 规范串 8 行格式（含空查询空行、空正文 sha256、方法大写）；正文/方法/路径/查询/时间戳/nonce/密钥任一篡改即失败；坏 hex 签名拒绝；等价路径共用同一签名材料 |
| `tests/test_nonce_store.py` | 内存与 SQLite 两实现：首次登记成功、重放失败、按 key_id 隔离、TTL 过期清理、**64 线程并发恰好 1 个赢家**、200 个不同 nonce 全部受理；SQLite 两连接指向同一库的跨进程去重 |
| `tests/test_verifier.py` | 完整流水线：happy path、重放、**64 并发恰好 1 通过**、注入时钟断言 **±300 边界等号成立 / ±301 拒绝（stale/future）**、正文篡改、未知 key、异钥签名、路径编码等价、`%2f` 在签名比较前拒绝、认证头格式（4 类）、头大小写不敏感 |
| `tests/test_server.py` | 真实 HTTP（`ThreadingHTTPServer`）端到端：health、正常、重放 409、**32 并发恰好 1 个 200**、正文篡改 401、时间窗口、等价路径（裸 socket 保留 raw target）、恶意 target 400、未知 key、缺头、签名 GET、**正文非法不烧 nonce 可重试（400→200→409）**、404、**非 ASCII nonce 返回 400（不是 500）**、重复 Content-Length 走私 400、超 1MiB 正文 413；SQLite 存储再跑一次并发；日志脱敏（密钥/签名/nonce 不出现）与 `RedactingFilter` 单测 |

> 精确 ±300 边界只在**注入时钟的单元测试**中断言；真实网络端到端（时间戳生成到服务端处理有 1~2 秒耗时）使用 ±299 / ±305 作为“窗口内/窗口外”判定，避免因传输耗时产生非确定性。

全新虚拟环境复验：

```bash
python3 -m venv /tmp/ar-venv
/tmp/ar-venv/bin/pip install -r requirements.txt   # cryptography 50.0.1
/tmp/ar-venv/bin/python -m unittest discover -s tests
# → Ran 91 tests ... OK
```

## 2. 端到端攻击场景演示

命令：

```bash
examples/run_demo.sh     # 自动生成密钥、随机端口起服务、跑完后停服
```

结果：**退出码 0，全部场景符合预期**。完整输出见 `docs/demo-output.txt`，服务端日志见 `docs/demo-server.log`。

实测关键结论（摘录）：

- 正常请求 → `200 accepted`
- 原样重放（相同方法/路径/正文/时间戳/nonce/签名）→ `409 replay_detected`
- **32 个完全相同的并发请求：恰好 1 个 `200`，31 个 `409`**
- 正文 `amount` 100→999 而签名不变 → `401 bad_signature`
- 时间戳 ±299 → `200`；±305 → `401 stale_timestamp` / `401 future_timestamp`
- `/api/data`、`/./api/data`、`//api//data`、`/api/%64ata`、`/api/../api/data`、`/api/data?b=2&a=1` 全部 `200`（规范化等价）
- `/api%2fdata`、`/%2e%2e/etc/passwd`、`?a=1&a=2` 全部 `400 malformed_request`（即使攻击者绕过合规客户端手工构造签名）
- 未知 key id → `401 unknown_key`；`GET /health` → `200`

额外的高并发压测（手工脚本，未纳入自动化套件以免拖慢日常测试）：

```
N=200 个线程同时发送同一签名请求 → 200=1，409=199，ERR=0，耗时 1.13s
```

## 3. 密钥权限保护

```
keystore 权限 0644 → 服务拒绝启动：
  failed to load keystore: keystore ... 权限过宽（0644）；请先执行 chmod 600 ...
chmod 600 后 → 正常启动，GET /health 返回 200
```

## 4. 日志不泄密核查

对 `docs/demo-server.log` 检查：

- 完整密钥 hex：**0 次出现**
- 完整 nonce：**0 次出现**（仅记录 SHA-256 前 8 位 `nonce_fp`）
- 请求签名：**0 次出现**（代码从不打印签名）
- 日志字段仅含 `event / result / key_id / nonce_fp / peer` 等

测试 `test_audit_log_contains_no_signature_or_secret` 与 `test_redacting_filter_*`
会在自动化层面持续校验这一点；另有 `RedactingFilter` 对已知密钥字符串做纵深脱敏。

## 5. 独立代码审查发现并修复的问题（已修复）

交付前用独立代理对全部源码做了一轮代码审查，发现 3 个真实问题，均已修复并补回归测试：

1. **nonce 被过早烧掉（中）**：原实现在 HMAC/时间窗通过后立即登记 nonce，而 POST 正文 JSON 校验在其后。已签名但正文为 `not-json` 的请求得到 400 的同时 nonce 已被占用，客户端改正后无法在窗口内安全重试（再发即 409）。
   **修复**：把验证器拆成 `authenticate()`（认证，不碰 nonce 表）与 `commit_nonce()`（原子登记）；服务端改为「认证 → 正文/业务校验 → 最后一步原子登记 nonce → 生效」。实测 `400 invalid_json → 改正后同 nonce 200 → 再重放 409`。
2. **非 ASCII nonce 头导致 500（低/中）**：审计路径对攻击者可控的原始 `X-Auth-Nonce` 调 `.encode("ascii")`，含非 ASCII 字节（如 `0xE9`）时抛 `UnicodeEncodeError`，使本应的 400 变成 500 且每请求一条异常日志。**修复**：指纹改用容错编码，并实测确认返回 `HTTP/1.1 400 Bad Request`。
3. **过短密钥报错不友好（低）**：`keygen --bytes 8` 抛裸 `ValueError` 未被捕获，打印 traceback。**修复**：keystore 层统一抛 `KeystoreError`，实测输出干净的中文错误并以退出码 1 结束。

审查同时确认正确的部分：两存储的 nonce 保留/过期语义、SQLite `BEGIN IMMEDIATE` 跨进程裁决、规范化的 fail-closed、请求走私探针（重复 Content-Length 拒绝、chunked 得 411、未排空正文不造成 keep-alive 不同步）、HMAC 恒定时间比较、keystore 0600、日志脱敏。

## 6. 开发过程中发现并修复的问题（如实记录）

1. **监听 backlog 过小**：`ThreadingHTTPServer` 默认 `request_queue_size=5`，32 路突发并发时部分连接被内核 reset（与防重放逻辑无关）。已提升到 128，并在测试客户端对连接级异常重试（重放请求幂等，重试安全）。
2. **客户端库路径折叠**：Python `http.client` 会在客户端侧把 `/./api/data` 折叠成 `/api/data`，无法演示“原始 target 经服务端规范化”。示例客户端与 E2E 测试改为裸 TCP 发送以逐字节保留 request-target。
3. **路由与签名的规范化一致性**：服务端最初按 raw path 路由，导致 `/./api/data` 在规范化前被判 404。现服务端先统一规范化，再用同一个 `(canonical_path, canonical_query)` 做路由与签名验证，杜绝“路由看到一种路径、验签看到另一种”的 TOCTOU 式歧义。
4. **TTL 边界**：nonce 过期清理由 `< now` 改为 `<= now`，与“记录在 ts+ttl 失效”的 claim 语义对齐。
5. **真实时间测试抖动**：端到端 ±300 精确边界受时钟/网络耗时影响有非确定性；精确边界改由注入时钟的单元测试保证，端到端改用有余量的偏移。
6. **请求走私面**：Python 标准库会接受两个**值相同**的重复 `Content-Length`（合并处理），与严格按 RFC 9112 拒绝重复头的前置代理可能产生歧义。已在 `_read_body` 用 `get_all` 显式拒绝任何多值/逗号合并的 `Content-Length`，并加 `test_013` 覆盖。

## 7. 未通过项 / 已知限制

- 无未通过的验收项；测试与演示均为全绿。
- 已知限制（设计内，非缺陷）：
  - 内存 nonce 存储只在**单进程**内去重；多进程/多机部署应使用 `--store sqlite`
    （或外部共享存储），否则各实例各自去重。SQLite 方案适合单机多 worker，不适合多机。
  - 时钟同步依赖服务端与客户端 NTP；窗口默认 ±300 秒，可按部署环境调整。
  - 本项目仅实现请求防重放，不包含 TLS、body 完整性之外的机密性保护、密钥轮换接口等；
    密钥为本地 OS CSPRNG 生成的测试密钥，未接入任何生产账号。
  - 时间戳为 Unix 秒；在 ±1 秒粒度内，同一 key 下同一 nonce 仍不可复用（由 nonce 保证）。
