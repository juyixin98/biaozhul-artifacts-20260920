# 运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic，Python 3.12.3，无第三方依赖（仅标准库）。
日期：2026-09-24。以下命令均在项目根目录实际执行。

## 1. 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

最终结果：**50 个测试全部通过**，`Ran 50 tests in 5.4s — OK`。

测试分布：

| 文件 | 内容 |
|---|---|
| `tests/test_signing.py` (15) | 路径/查询规范化、点段归一化、编码大小写收敛、编码斜杠不当分隔符、非法 `%` 转义与控制字符拒绝、正文字段顺序、逐字段篡改破坏签名、空正文 |
| `tests/test_nonce_store.py` (7) | 单次登记、kid 隔离、窗口边界后可复用、过期清理、32 线程并发唯一胜出、文件库持久化、**两个真实 OS 进程**抢同一 nonce（`LOSE/WIN` 各一） |
| `tests/test_verifier.py` (12) | 合法/重放/正文篡改/方法篡改、路径编码歧义收敛、查询串重排、**±300s 时间边界含等号**、nonce 过期复用、缺头坏头、未知密钥、伪签名、Authorization 中 kid 不一致 |
| `tests/test_server.py` (16) | 真实 HTTP 端到端：healthz、签名通过、正文篡改、重放对、**40 并发同签名请求恰有 1 个 200 / 39 个 403**、路径变体、越界时间戳、缺头、404、日志脱敏（密钥与 64 位签名不出现在日志） |

## 2. 真实服务验收（live）

```bash
python3 -m scripts.gen_keys                       # 生成 2 个 256-bit 本地密钥，data/keys.json 权限 0600
python3 -m replay_protection.server --port 8080 &
echo '{"op":"transfer","amount":42}' | python3 -m scripts.sign_request \
    --url http://127.0.0.1:8080/v1/verify --kid test-key-1 --send
PYTHONPATH=. python3 examples/acceptance_live.py
```

实际结果（**11/11 通过**）：

```
[PASS] GET /healthz open: HTTP 200
[PASS] valid signed POST: HTTP 200
[PASS] identical replay: HTTP 403
[PASS] tampered body: HTTP 401
[PASS] canonicalized unknown route is 404 not bypass: HTTP 404
[PASS] timestamp delta -299: HTTP 200
[PASS] timestamp delta +299: HTTP 200
[PASS] timestamp delta -301: HTTP 401
[PASS] timestamp delta +301: HTTP 401
[PASS] unsigned POST: HTTP 401
[PASS] 40 concurrent duplicates: 1x200 39x403 (expected 1x200 39x403) other=[]
TOTAL: 11/11 checks passed
```

时间边界（±300s 含端点）的精确判定由注入时钟的单测确定性覆盖；HTTP 现场脚本
故意取 ±299s / ±301s，避免请求耗时恰好越过秒边界造成假阴性。

路径编码歧义现场演示：

```
signed=/v1/verify    wire=/v1/verify       -> 200
signed=/v1/verify    wire=/v1/./verify     -> 200
signed=/v1/verify    wire=/v0/../v1/verify -> 200
signed=/v1/verify    wire=/v1/%76erify     -> 200
signed=/v1/verify    wire=/v1%2Fverify     -> 404   # 编码斜杠保留在段内，不还原为分隔符
signed=/v1%2Fverify  wire=/v1/verify       -> 401   # 真斜杠路径的签名不能用于编码斜杠路径
```

日志脱敏检查（对运行中进程的日志全文 grep）：

```bash
SECRET=$(python3 -c "import json;print(json.load(open('data/keys.json'))['keys'][0]['secret_hex'])")
grep -c "$SECRET" /tmp/server.log            # 0
grep -oE '\b[0-9a-f]{64}\b' /tmp/server.log  # 无输出（无签名泄露）
grep -ci authorization /tmp/server.log       # 0
```

日志只含机器可读错误码（`replay_detected`、`bad_signature`、`stale_timestamp` 等），
不带头内容；响应里 nonce 也只回显前 4 字符。

## 3. 过程中发现并修复的真实缺陷（如实记录）

首版测试并非一次通过，`Ran 50 tests — FAILED (failures=5, errors=5)`，发现并修复：

1. **错误处理路径二次崩溃（真实代码缺陷）**：`VerificationError` 构造时未保存
   `self.message`，拒绝请求时 `_reject()` 访问 `err.message` 抛 `AttributeError`，
   导致重放请求的 403 响应发不出去、连接被重置。并发测试因此只有 1 个客户端收到
   响应。已补上 `self.message = message`，修复后 39 个并发客户端稳定收到 403。
2. **listen backlog 过小**：40 个客户端同时握手时 13 个 `Connection reset`。
   `build_server` 将 `request_queue_size` 提到 128 并重新 `listen()`。
3. **路由按原始路径匹配**：`/v1/./verify` 这类规范化后合法的路径被直接 404。
   服务端先对路径做同一套规范化再路由，且规范化失败（如 `%zz`）返回 400。
4. **nonce 过期边界与时间窗口口径统一**：清理条件从 `expires_at <= now` 改为
   `expires_at < now`，与“`|now-ts| <= 300` 边界含等号”保持一致；到期当刻仍算重放。
5. 两个测试自身的期望错误（`%2B` 收敛到字面 `+` 的规范化结果断言写反；
   “多种路径写法共用一个 nonce”本身就是重放）已随设计修正。

## 4. 未通过项 / 已知限制

- 无未通过测试：当前 50/50 单元/集成测试与 11/11 现场验收全部通过。
- 已知限制（设计取舍，非缺陷）：
  - 内存版 nonce 库（`:memory:`）只在单进程内成立；多进程部署必须使用文件 SQLite
    （默认 `data/nonces.db`，已用双进程测试验证）；多机部署需换共享存储/Redis，
    接口保持 `register(kid, nonce) -> bool`。
  - 服务不提供 TLS；生产必须置于 HTTPS 终端之后。
  - nonce 在窗口内持续占用存储，由守护线程 60s 周期清理 + 插入前按需清理；
    高基数 kid 场景未做配额限制。
  - 时钟偏差假设对称 300s；依赖节点 NTP。
