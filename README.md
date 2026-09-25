# enrange：固定块 AEAD 加密对象存储与跨块范围读取

纯后端、本地运行的安全数据处理服务。数据以**固定大小分块的 AEAD 格式**落盘，
支持对加密对象做**跨块范围读取**：先逐块完成认证，再返回该块明文；关联数据（AAD）
把对象身份、块位置与长度全部绑定，密文块被移动、交换、截断或跨对象复用都会验证失败。

- 语言：Python 3.9+（实测 3.12.3）
- 密码库：[`cryptography`](https://cryptography.io/)（AES-256-GCM、HKDF-SHA256），
  **不自创加密算法**
- 密钥：本地随机生成，文件权限 `0600`；**不接入任何生产账号 / KMS**
- 服务：仅标准库 `http.server`，默认绑定 `127.0.0.1`，无前端

---

## 1. 安全模型与保证

| 能力 | 实现 |
| --- | --- |
| 机密性 | AES-256-GCM；每对象经 HKDF-SHA256 从主密钥派生独立密钥，主密钥不直接参与 AEAD |
| 完整性 / 真实性 | GCM 认证标签：头部一个标签，每个数据块一个标签 |
| 位置绑定 | 块 AAD 含对象 id、块序号、明文偏移、本块长度、对象总长、总块数 |
| 长度绑定 | 头部 AAD 含 `block_size`、明文总长度、封装区总长度 |
| 防块交换 | 块序号/偏移在 AAD 中，交换同长块后标签验证失败 |
| 防跨对象移植 | 对象 id 既参与 HKDF 派生（密钥不同），又写入 AAD |
| 防截断 / 追加 | 头部声明长度并与实际文件大小比对，不一致即认证失败 |
| Nonce 不重复 | 确定性计数器 nonce（4 字节前缀 + u64 序号），块空间与头部空间前缀互斥 |

**核心不变量：任何返回给调用方的明文字节，都来自已经通过 AEAD 验证的块。**
范围读取时，只有与请求区间相交的块会被读取和解密；其中任一块验证失败，整体返回
`409 Conflict` / CLI 退出码 4，不返回任何已受影响的数据。

### 明确不在范围内

- 不是网络对外服务：无 TLS、无鉴权/多租户，设计为 localhost 本地工具。
- 不防持有主密钥的人：主密钥泄露即可解密所有对象。
- 不是端到端加密通信系统，也不提供密钥轮换、删除归零化（zeroization）等高级管理。
- 每次 `PUT` 整体重写对象文件（原子 rename），非原地追加。

---

## 2. 磁盘格式

每个对象一个文件：`<data_dir>/objects/<32位hex对象id>.bin`。

```
+--------------------------- header (49 字节) ---------------------------+
| magic(8)="ENRANGE1" | ver(1)=0x01 | block_size u32 | plaintext_len u64 |
| encap_bytes u64 | num_blocks u32 | header_tag(16)                     |
+-----------------------------------------------------------------------+
| block frame 0 | block frame 1 | ... | block frame N-1                 |
+-----------------------------------------------------------------------+
```

- 每个块帧 = `AES-GCM 密文 || 16 字节标签`，无额外帧头。
- 非末尾块明文长度 = `block_size`，末尾短块明文长度 = 余数（1..block_size）；
  空对象没有块帧（`num_blocks=0`，文件仅 49 字节头部）。
- nonce 由块序号确定性生成，无需存储随机 nonce。

### AAD 线格式（长度前缀均为大端）

```
header: "enrange-v1-hdr\0" || u32(len(id)) || id
        || u32(block_size) || u64(plaintext_len) || u64(encapsulated_bytes)

block : "enrange-v1-blk\0" || u32(len(id)) || id
        || u32(block_size) || u64(block_index) || u64(plaintext_offset)
        || u32(plaintext_len) || u64(object_plaintext_len) || u32(total_blocks)
```

---

## 3. 安装

```bash
python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -r requirements.txt                       # 仅依赖 cryptography
```

## 4. 命令行用法

```bash
# 生成本地随机主密钥（首次自动创建目录，master.key 权限 0600）
python3 -m enrange keygen --data-dir ./data

# 存入对象（--id 可省略，省略则随机生成；区间为固定分块大小）
python3 -m enrange put --data-dir ./data --id 0123…cdef --in file.bin --block-size 64

# 全量 / 范围读取（区间为半开 [start,end)，省略 --end 读到末尾）
python3 -m enrange get  --data-dir ./data --id 0123…cdef --start 60 --end 72
python3 -m enrange stat --data-dir ./data --id 0123…cdef
python3 -m enrange list --data-dir ./data
```

退出码：`0` 成功，`2` 用法/IO 错误，`3` 对象不存在，`4` **认证失败（篡改/损坏）**，
`5` 非法区间。

## 5. HTTP 服务

```bash
python3 -m enrange serve --data-dir ./data --host 127.0.0.1 --port 8099
```

| 方法与路径 | 说明 |
| --- | --- |
| `POST /objects?block_size=N` | 用请求体创建对象，服务端生成 id，返回 JSON 元数据 |
| `PUT /objects/{id}?block_size=N` | 用显式 id 存储请求体 |
| `GET /objects/{id}` | 全量 `200` 或范围 `206` |
| `HEAD /objects/{id}` | 仅返回经认证的长度等头，无正文 |
| `GET /objects/{id}/meta` | 经认证的元数据 JSON |
| `GET /objects` | 列出对象 id |
| `GET /healthz` | 存活检查 |

范围选择（二选一，不混用）：

- HTTP 头（RFC 7233 单个闭区间）：`Range: bytes=0-0`、`Range: bytes=49-`
- 查询参数（半开区间）：`?start=14&end=20`，省略 `end` 到末尾

状态码：`201` 创建；`206` 部分内容（带 `Content-Range`）；`400` 请求错误；
`404` 对象不存在；`409` **认证失败，不返回明文**；`411` 缺少 `Content-Length`；
`416` 区间无法满足。仅支持单区间，多区间 `Range` 被拒绝（`400/416`）。

可运行样例：

```bash
bash examples/curl-examples.sh
python3 examples/http_client_demo.py
```

---

## 6. 测试

```bash
python3 -m pytest -q
```

测试覆盖（`tests/`）：

- 密码原语：HKDF 密钥独立、计数器 nonce 唯一、AAD/密文改动触发 `InvalidTag`
- 存储层：全量往返；**首/尾字节、跨块、末尾短块、空对象**；非法区间
- **密文交换**：同一对象内交换块帧、跨对象复制块帧、移动到不同位置
- 篡改：翻转密文字节 / 标签位、改头部长度与块大小、截断、追加、破坏 magic
- 错误主密钥（不同密钥打不开对象）；`master.key` 权限为 `0600`
- HTTP 端到端：真实本地线程服务器，覆盖 `200/201/206/404/409/411/416`
- CLI 端到端：keygen/put/stat/get/list 与篡改退出码

实际运行命令与结果见 [`RUN_LOG.md`](./RUN_LOG.md)。

## 7. 目录结构

```
enrange/
  crypto.py     # AES-256-GCM / HKDF 封装，nonce 与 AAD 构造
  store.py      # 磁盘格式、加密写入、逐块验证的范围读取
  server.py     # 标准库 HTTP 服务
  __main__.py   # CLI（keygen/serve/put/get/stat/list）
  errors.py
tests/          # pytest 套件（50 个用例）
examples/       # curl 与 Python 标准库请求样例
```
