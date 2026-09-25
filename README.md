# 透明日志包含证明服务（Transparent Log — Inclusion & Consistency Proofs）

纯后端、本地运行的**追加式 Merkle 透明日志**。任何客户端都可以：

- 向日志追加数据条目；
- 取得一个由测试密钥签名的**签名树头（Signed Tree Head, STH）**；
- 请求某条目的**包含证明（inclusion proof）**，并用本地持有的树根独立核验；
- 请求两个树大小之间的**一致性证明（consistency proof）**，核验日志只追加、未改写历史。

哈希方案遵循 **RFC 9162（Certificate Transparency v2，前身 RFC 6962）第 2 章**，
SHA-256 并带**叶/内部节点域分离前缀**：

```
MTH({})                = SHA256("")
叶哈希  leaf_hash(d)   = SHA256(0x00 || d)
内部哈希 node_hash(l,r)= SHA256(0x01 || l || r)
```

`0x00/0x01` 前缀保证内部节点不能被当作叶子（或反之）提交，避免哈希结构混淆。

> 范围声明：本服务是教学/本地测试实现。它证明**单条日志**的只追加性质，
> **不解决日志分叉（fork）与运营者-受众共谋问题**——即不提供 gossip、
> STH 法定人数（quorum）或最小分叉检测延迟（MMD）保证。详见文末「安全边界」。

---

## 1. 目录结构

```
tl/
  merkle.py    RFC 9162 MTH、包含证明、一致性证明的生成与核验（纯函数）
  store.py     追加式 JSONL 持久化（fsync，重启重放）
  signing.py   Ed25519 STH 签名/验证（域分离签名串），本地测试密钥
  log.py       高层服务：追加、STH、包含/一致性
  server.py    标准库 http.server 实现的本地 HTTP API
  client.py    stdlib 客户端 + CLI，并在本地独立核验证明
tests/         pytest 自动化测试（44 个）
examples/
  demo.py      进程内端到端演示（含攻击者尝试全部被拒绝）
  requests.sh  全部端点的 curl 请求样例
requirements.txt
RUN_LOG.md     实际执行命令与结果的如实记录
```

## 2. 安装

仅需一个成熟密码学库；其余全部使用 Python 标准库。

```bash
python3 -m pip install -r requirements.txt   # cryptography>=41
# 环境已验证：Python 3.12.3 + cryptography 41.0.7 + pytest 9.1.1
```

不接入任何生产账号、云 KMS 或网络托管密钥；密钥只在本地生成。

## 3. 运行服务

```bash
python3 -m tl.server --host 127.0.0.1 --port 8088 --data-dir ./.tl-data
```

首次启动在 `--data-dir` 本地生成：

- `ed25519_test_key.bin`：32 字节 Ed25519 种子，权限 `0600`（仅属主可读写）；
- `log.jsonl`：每行一条 JSON 记录 `{"index","data_b64","ts"}}`，只追加。

## 4. HTTP API

所有哈希与证明均为标准 base64 JSON 字符串。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康状态与当前树大小 |
| GET  | `/v1/public-key` | Ed25519 公钥（raw base64 + PEM）与 STH 签名上下文 |
| POST | `/v1/entries` | 追加：`{"data_b64":"..."}` 或 `{"text":"..."}` → `{"index","leaf_hash"}` |
| GET  | `/v1/entries?index=n` | 取回第 n 条 |
| GET  | `/v1/sth` | 当前签名树头（树大小、根、微秒时间戳、Ed25519 签名） |
| GET  | `/v1/proof/inclusion?leaf_index=i&tree_size=t` | 包含证明（`tree_size` 可省略=当前，可指定历史大小） |
| GET  | `/v1/proof/consistency?old_size=a&new_size=b` | 一致性证明 |
| POST | `/v1/verify` | 服务端再核验入口（生产中客户端应本地核验） |

STH 签名覆盖的字节串有独立上下文，不能与其他协议消息混用：

```
"TL-STH-v1" || u64be(tree_size) || root_hash(32B) || u64be(timestamp_us)
```

### 客户端 CLI（本地核验）

```bash
python3 -m tl.client health
python3 -m tl.client add "hello world"
python3 -m tl.client sth
python3 -m tl.client inclusion 0 --tree-size 1     # 输出含 "locally_verified": true
python3 -m tl.client consistency 2
```

完整 curl 样例：`bash examples/requests.sh`；进程内演示：`python3 examples/demo.py`。

## 5. 证明语义（验收点）

**包含证明**：对树大小 `n` 中索引 `i` 的叶子，返回自底向上的兄弟哈希序列。
核验从叶子哈希出发按路径重建根，与客户端信任的根比较。索引越界、根不符、
证明被截断/加长/篡改一律拒绝（核验函数对畸形输入返回 `False`，不抛异常）。

**一致性证明**：给定旧大小 `m`、新大小 `n`（`m ≤ n`）与两个根，证明新树的
前 `m` 个叶子就是旧树。实现为 RFC 9162 §2.1.3.2 的 `SUBPROOF()` 递归；
当 `m` 是 2 的幂时，验证方预置自己持有的旧根（线上证明不含旧根）。

### 测试如何逐项核验

- `tests/test_rfc_vectors.py`
  - RFC 9162 §2.1.5 的 **7 叶符号例子**逐字对照：包含证明 `[b,h,l]`、
    `[c,g,l]`、`[f,j,k]`、`[i,k]`；一致性 `3→7=[c,d,g,l]`、`4→7=[l]`、
    `6→7=[i,j,k]`（7 为**非二次幂**叶数）。
  - RFC 6962 附录/CT 参考套件的 **8 叶二进制向量**（`d(j)=0x00||j`）：
    空树根 `e3b0c442…`、d(0) 叶哈希 `709e80c8…`、大小 1–8 的全部根
    （n=8 根 `0a2a2c47…`）、索引 0 与 6 的包含证明、`3→8`（非二次幂旧大小）
    一致性证明，全部用发布的十六进制常量断言。
- `tests/test_merkle_properties.py`
  - 穷尽核验：树大小 `1..300` × 每个叶子索引的包含证明全部通过；
    `1..129` 全部 `(旧,新)` 大小对的一致性证明全部通过（自动覆盖 3、5、6、
    7、9、… 等大量非二次幂叶数与二次幂/非二次幂旧大小的组合）。
  - **伪造旧根/新根、错误叶子索引、翻转证明位、截断/加长证明、错误旧大小**
    全部被拒绝。
- `tests/test_store.py`：追加持久化、重启重放、历史根不变、损坏文件检测。
- `tests/test_signing.py`：Ed25519 签名往返；篡改树大小/根/时间戳/签名、
  换公钥全部验证失败；密钥文件权限 0600。
- `tests/test_log_service.py`：服务层追加→签名 STH→包含/一致性端到端，
  含历史 STH（大小 13，非二次幂）与追加后旧根仍有效。
- `tests/test_http_api.py`：在临时端口起真实 HTTP 服务走完整工作流，
  并验证伪造根/错误索引经网络被拒，以及 400/404 错误处理。

运行：

```bash
python3 -m pytest tests/ -v
```

## 6. 安全边界（明确不做的事）

本服务**不**防止以下威胁，这是透明日志问题空间中有意识的边界：

1. **日志分叉 / 共谋**：运营者可以向客户端 A 签发历史 `H₁` 的 STH，
   向客户端 B 签发另一条历史 `H₂` 的 STH，并分别为两边提供自洽的包含证明。
   只要 A、B 从不交叉比对各自看到的 STH，本服务无法察觉。
   检测分叉需要**带外 STH gossip / 见证者（witness）/ 法定人数**与
   最小分叉检测延迟（MMD）机制——本项目**明确不实现**这些，也不声称提供该性质。
2. 不提供准入控制、TLS、认证或多租户隔离；默认仅绑定 `127.0.0.1`。
3. 密钥是**本地测试密钥**，不具备长期保管、轮换或 HSM 保护；不得用于生产。
4. 不自创任何加密原语：哈希为 SHA-256，签名为 Ed25519（`cryptography` 库），
   树算法为 RFC 9162 的标准构造。
