# 透明日志包含证明（Transparent Log — Inclusion & Consistency Proofs）

纯后端、纯本地的「透明日志」教学/测试实现：一棵**追加式 Merkle 树**，
支持：

- **包含证明（Inclusion Proof）**：证明某条叶子确实在某个（已签名的）树头中；
- **树大小间一致性证明（Consistency Proof）**：证明新旧两个树头前缀一致，
  旧叶子既没有被改写也没有被重排，新树只在末尾追加；
- **签名树头（STH, Signed Tree Head）**：树根 + 树大小 + 时间戳由本地
  测试用 Ed25519 密钥签名；
- **域分离（Domain Separation）**：叶子哈希与内部节点哈希分别用
  `0x00` / `0x01` 前缀，防止内部节点被冒充为叶子（第二原像攻击）。

算法逐条对应 **RFC 9162（Certificate Transparency v2）§2.1**，
SHA-256 为哈希算法。结构测试向量取自 RFC 6962 §2.1.3 的七叶树示例。

> 无前端、无外部账号。仅监听 `127.0.0.1`。密码学只使用成熟库
> [`cryptography`](https://cryptography.io/)（Ed25519, RFC 8032），
> 不自创任何加密/哈希算法；密钥均为本地生成的**测试密钥**。

---

## 目录结构

```
a/
├── tlog/                    # 库 + 服务 + CLI
│   ├── hashing.py           # SHA-256 叶子/内部节点哈希，0x00/0x01 域分离
│   ├── merkle.py            # 树根、包含证明、一致性证明（RFC 9162 逐行翻译）
│   ├── keys.py              # Ed25519 测试密钥生成/读写/STH 签名与验签
│   ├── log.py               # 追加式存储（leaves.jsonl + 哈希链）与 Log 入口
│   ├── server.py            # 标准库 http.server 实现的 JSON API（仅 127.0.0.1）
│   ├── client.py            # urllib 客户端 + 独立验证器
│   └── cli.py               # python -m tlog.cli 命令行
├── tests/                   # 57 个自动化测试（unittest，无外部依赖）
│   ├── test_merkle.py       # 核心：三套独立实现交叉验证 + RFC 符号向量 + 攻击用例
│   ├── test_keys.py         # Ed25519 密钥与 STH 签名
│   ├── test_log.py          # 追加存储、哈希链篡改检测、持久化
│   └── test_server.py       # 真实起 HTTP 服务的端到端测试
├── scripts/demo.py          # 命令行端到端演示（含攻击演示）
├── examples/
│   ├── requests.sh          # curl 请求样例
│   └── sample_responses.json# 实际运行保存的响应样例
├── requirements.txt
├── RUNLOG.md                # 实际运行记录（命令、结果、发现的问题）
└── README.md
```

---

## 快速开始

环境：Python 3.12（3.10+ 亦可），安装一个依赖：

```bash
pip install -r requirements.txt
```

### 1. 跑自动化测试

```bash
python3 -m unittest discover -s tests -v
```

### 2. 跑端到端演示（自动构造非二次幂 13 叶树、逐项验证、攻击演示）

```bash
python3 scripts/demo.py
```

### 3. 起 HTTP 服务并用 curl/CLI 交互

```bash
# 终端 A
python3 -m tlog.cli serve --host 127.0.0.1 --port 8080 --data-dir ./log_data

# 终端 B
bash examples/requests.sh

# 追加 / 查询 / 本地验证
python3 -m tlog.cli add "第一条记录"
python3 -m tlog.cli add "第二条记录"
python3 -m tlog.cli sth                 # 获取并验签 STH
python3 -m tlog.cli inclusion 0         # 取包含证明并本地验证（退出码 0/1）
python3 -m tlog.cli consistency 1       # 取一致性证明并本地验证
```

验证方**只信任**自己手里已验签的 STH（根 + 树大小）与带外获得的日志公钥，
不信任服务端响应中的任何「是否通过」结论——服务端只返回叶子与路径，
验证在客户端本地重算完成。

---

## HTTP JSON API

仅监听 `127.0.0.1`。成功 200，参数错误 400，方法不对 405，路由不存在 404。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/add` | `{"data_utf8": "..."}` 或 `{"data_b64": "..."}`；返回 `leaf_index/tree_size/leaf_hash_hex` |
| `GET` | `/sth` | 签名树头：树大小、时间戳、根、Ed25519 签名、公钥 |
| `GET` | `/get-inclusion-proof?leaf_index=m&tree_size=n(可选)` | 包含证明（兄弟哈希列表） |
| `GET` | `/get-consistency-proof?first=m` | 旧大小 m → 当前大小的一致性证明 |
| `GET` | `/get-leaf?index=i` | 叶子原文（base64，可 UTF-8 时附明文） |
| `GET` | `/health` | `{"status":"ok","tree_size":n}` |

`tree_size` 省略时表示当前树；显式给出时可取「历史树大小」上的证明。

### 本地存储

数据目录（默认 `log_data/`）：

- `leaves.jsonl`：每行一条叶子，只追加。每条记录带 SHA-256 哈希链
  （`record_hash = SHA256(prev_hash ‖ u64 added_ms ‖ u32 len ‖ data)`），
  启动时重放全链，能检测文件内容被改、插、删、乱序；
- `test_ed25519_private.pem` / `test_ed25519_public.pem`：首次启动自动
  生成的本地**测试**密钥（私钥 PEM 未加密，仅供本地演示，勿用于生产）。

---

## 算法要点（对应 RFC 9162）

```
MTH({})                = SHA256("")                         # 空树根
MTH({d0})              = SHA256(0x00 ‖ d0)                  # 叶子（域分离）
MTH(D[n])              = SHA256(0x01 ‖ MTH(D[0:k]) ‖ MTH(D[k:n]))  # 内部节点
                         k = 严格小于 n 的最大二次幂
```

- **包含证明** PATH(m, D[n])：叶子 m 到根路径上各层兄弟哈希的有序列表，
  验证器用 `fn=m, sn=n-1` 的最低位（LSB）循环决定每层是 `H(0x01‖p‖r)`
  还是 `H(0x01‖r‖p)`，最终比对重建根与 STH 根。
- **一致性证明** PROOF(m, D[n])：验证器同时重建旧根 `fr` 与新根 `sr`，
  两者都匹配且计数器归零才通过。旧大小恰为二次幂时，验证器把旧根
  预置进路径（生成器在最左脊上省略该冗余节点）。
- 单叶树包含证明为空列表 `[]`；空树一致性证明按 RFC 视为非法（`first>0`）。

### 验证器语义提醒（安全用法）

RFC 的包含验证器**不检查路径长度**。同一条证明可能对多个树大小
（位形相同的一个区间，如本项目测试中 m=3、n=11 的证明对谎报大小
9–16 都能重建出同一个 11 叶根）通过。这不是缺陷：`tree_size` 与
`root_hash` 必须取自**已验签的 STH**，由验证方钉住，而不是连同证明
一起信任。测试 `test_tree_size_must_come_from_signed_sth_*` 中
如实记录并固化了该性质。

---

## 威胁模型：保证什么，不保证什么

**提供的保证（在持有可信旧 STH 与日志公钥的前提下）：**

1. **包含性**：一条叶子在指定树头中存在且位置明确，无法伪造
   （错误索引、错误叶子、路径篡改、路径截断/追加、伪根均被拒绝）。
2. **只增性 / 一致性**：同一观察者先后看到的两个 STH，旧树是新树的
   前缀，旧内容不可改、不可重排。
3. **树头真实性**：STH 的根、大小、时间戳由日志私钥签名，篡改即验签失败。
4. **本地文件完整性**：`leaves.jsonl` 的哈希链能发现磁盘记录被改/插/删。

**明确不解决（按需求边界，不声称防御）：**

- **日志分叉 / 伪装（forking / equivocation）与运营者共谋**：日志运营者
  可以用一套叶子维护树 A、用另一套叶子维护树 B，向互不交换信息的
  观察者出示不同的 STH。一致性证明只能约束「同一条观察链上的两个
  STH」，无法跨两条独立视图发现分叉。真实系统需引入外部机制：
  STH gossip、witness cosigning（如 SUMDAK/Static Certificate
  Transparency 的见证方）、或第三方审计员交叉比对。本项目不实现这些。
- 私钥泄露、运营者主动用同一把密钥签名多个树头（这正是分叉问题）、
  拒绝服务、叶子内容本身的合规性审查，均不在本项目范围。

---

## 测试与验证方法说明

为避免「生成器和验证器犯同一个错误」，`tests/test_merkle.py` 使用：

1. **生产实现**：RFC 9162 伪代码的逐行翻译（递归生成 + LSB 循环验证）；
2. **独立参考实现**：测试文件内自底向上「相邻配对、奇数节点上提」
   的另一套算法，独立算根、独立按区间坐标行走生成路径，与生产实现
   逐元素交叉比对；
3. **硬编码锚点**：`SHA256("")`、`SHA256(0x00)` 等可手算值，以及
   RFC 6962 §2.1.3 七叶树（非二次幂）的符号结构向量
   （d0→[b,h,l]、d3→[c,g,l]、d4→[f,j,k]、d6→[i,k]；
   PROOF(3,7)=[c,d,g,l]、PROOF(4,7)=[l]、PROOF(6,7)=[i,j,k]）。

覆盖（561+ 片叶子的逐项核验）：

- 小树 n=1..33 **每一片叶子**逐项验证；另含 37/50/100/200/257/333 叶全量；
- 非二次幂：3,5,6,7,9,15,17,31,33,63,65,127,129,255,257,511,513,1000,1023,1025；
- 伪造旧根、错误索引、他人叶子、路径元素翻转、截断、追加、乱序、
  错误树大小、畸形长度、空树等拒绝路径；
- 一致性证明 n=2..39 的全部 (first,n) 组合，及更大树的关键位置；
- 旧前缀不同的两棵树，A 树证明无法关联 B 树根；
- Ed25519 签名对树大小/时间戳/根/签名本身/公钥五类篡改的拒绝；
- leaves.jsonl 改内容 / 插入 / 删除记录均被哈希链发现；
- HTTP API 端到端：追加 → 旧 STH 验签 → 增长 → 一致性连接，及 400/404。

实际运行命令与结果见 **[RUNLOG.md](RUNLOG.md)**（含开发中发现并修复的
一个真实 bug，与一处验证语义的实测澄清）。
