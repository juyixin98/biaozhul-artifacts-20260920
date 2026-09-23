# NFT 元数据一致性验证服务（纯后端）

本地运行的 **NFT 元数据内容寻址（content addressing）一致性验证服务**。
输入代币 ID、CID 和本地内容块字节，**不访问任何公网网关 / IPFS 节点**：
所有哈希、编码、密码学操作都在进程内对真实字节执行。

技术栈：**TypeScript（严格模式）+ Fastify 4 + better-sqlite3**，运行时 Node ≥ 18.19。

---

## 1. 它实际验证什么

每个引用都不是"字符串外形校验"，而是逐层对真实字节做计算并给出**校验依据**：

| 层 | 真实执行的检查 |
|---|---|
| **CID 结构** | 只接受 **CIDv0（`Qm…` dag-pb）** 和 **CIDv1**，multibase 仅允许 `b`（RFC 4648 小写无填充 base32）与 `z`（base58btc）。其他 multibase（base16 `f` / base36 `k` / base64 `m` / 大写 `B` …）、CIDv2、非最小 varint、尾随字节一律拒绝 |
| **multihash** | 只接受 **sha2-256（code 0x12）+ 32 字节摘要**。identity multihash（自证内容）、截断摘要、未知哈希函数全部拒绝 |
| **内容寻址** | 对存储块**重新计算 sha2-256**，与 CID 内嵌摘要做常量时间比较。一个比特被篡改即 `CONTENT_MISMATCH`，且篡改块永远进不了库（入库前先复算） |
| **JSON 编码** | 自写 RFC 8259 递归下降解析器 + 严格 RFC 3629 UTF-8 解码器：拒绝重复键（`JSON.parse` 会静默后者覆盖）、孤立 UTF-16 代理转义、非 UTF-8 字节（overlong、surrogate 编码、>U+10FFFF、截断序列）、前导零/`+`、尾随数据 |
| **元数据 schema** | 根块必须是 json codec：`name` / `version` / `image` / `image_type` / `image_size`，可选递归 `links[]`；未知字段拒绝；link 块禁止携带 `version` |
| **媒体引用** | raw codec 块通过**魔数签名**嗅探真实类型（PNG 含 IHDR 校验 / JPEG SOI / GIF87a·89a / RIFF·WEBP），与声明的 `image_type` 比对；`image_size` 必须等于真实字节长度；SVG 等文本格式不支持 |
| **引用图** | 沿 `image` / `links[]` 遍历，每个被引用 CID 必须在本地块库中且 codec 符合预期；**循环引用拒绝**（引用栈检测，支持菱形去重）；有深度/节点数上限 |

每个节点的响应都含 `claimedDigest`（CID 声称的摘要）与 `computedDigest`（实算摘要）
及一组带 `id/detail/ok` 的 `checks`，即"每层引用的校验依据"。

### 版本与链重组语义

* 元数据改版：`version` 必须严格 +1；首个修订必须是 1；**重复/回退/跳号一律 409**。
  每个旧 CID 与其锚定的链上高度**永久保留**（状态机 `pending → active → superseded`，重组撤销为 `revoked`，不删除行）。
* 内置一条轻量本地链（height、block_hash、parent_hash，由调用方提供哈希，不接触公网）。
  修订在链尖登记；**3 个确认**后变为 `active`（终局），之前为 `pending`。
* **链重组**：`reorg` 替换从某高度起的规范链块；锚定在被废弃槽位上的**未确认更新被撤销**，
  代币当前 CID 自动回退到最近的已终局版本。试图回滚已达到 3 确认深度的块会被
  `DEEP_REORG_REJECTED` 拒绝（终局性保护）。

---

## 2. 目录结构

```
src/
  crypto/           真实密码学/编码实现（无第三方 multiformats 库）
    varint.ts       LEB128 unsigned varint（最小编码校验）
    base58.ts       base58btc（BigInt 实现）
    base32.ts       RFC4648 base32 小写无填充（严格：拒绝大写/填充/非规范尾位）
    multihash.ts    multihash 解析 + sha2-256 复算（node:crypto）
    cid.ts          CIDv0/CIDv1 解析、构造、规范化往返校验
  codec/
    utf8.ts         严格 UTF-8 解码器
    json-parse.ts   严格 JSON 解析器（重复键/lone surrogate/…）
    media-sniff.ts  魔数嗅探 PNG/JPEG/GIF/WebP
    metadata.ts     元数据 schema
  store/
    blockstore.ts   SQLite 块存储
    chainstore.ts   链、代币、修订状态机 + 重组事务
  services/block-service.ts  入库：base64 解码 → CID 解析 → 复算哈希
  verifier.ts       DAG 遍历 + 分层证据
  app.ts            Fastify 路由与错误处理
  index.ts          启动入口
test/               node:test 自动化测试（48 个，全部离线）
scripts/make-examples.ts    用真实哈希生成 examples/
examples/demo.sh            端到端验收脚本（11 项断言）
```

---

## 3. 本地启动

```bash
npm install          # 已提交 package-lock.json，锁定依赖
npm test             # 48 个自动化测试（CID/UTF-8/JSON/媒体/验证器/HTTP/重组）
npm run build        # 类型检查 + 编译到 dist/
npm start            # 运行编译产物（默认 127.0.0.1:3000，SQLite 在 data/nft.db）
# 或开发模式：
npm run dev
```

环境变量：`PORT`（默认 3000）、`HOST`（默认 127.0.0.1）、`NFT_DB_PATH`（默认 data/nft.db）、`LOG_LEVEL`。

### 一键验收（两个终端）

```bash
# 终端 A
npm run dev

# 终端 B
npm run make:examples     # 生成 examples/*.json（CID 全部现算，非硬编码）
bash examples/demo.sh     # 期望最后输出：RESULT: 11 passed, 0 failed
```

`demo.sh` 覆盖：块上传、篡改字节拒绝、非 UTF-8 拒绝、缺块、链前登记拒绝、
确认数推进、重复版本拒绝、重组撤销未确认更新、旧 CID 保留、深度重组拒绝。

---

## 4. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 + 已存块数 |
| PUT  | `/blocks/:cid` | 上传内容块，body `{ "data_base64": "…" }`（标准 base64，拒绝 base64url/非规范填充）；服务端复算 sha2-256 与 CID 比对 |
| GET  | `/blocks` / `/blocks/:cid` | 列出块 / 查元数据（不回吐字节） |
| POST | `/verify` | 离线验证一个根 CID 的完整 DAG，返回分层证据；body `{ "root_cid": "…", "expected_version"?: n }` |
| POST | `/chain/blocks` | `{action:"append", block_hash, parent_hash}` 或 `{action:"reorg", from_height, new_blocks:[{hash,parent}]}` |
| GET  | `/chain` | 当前链、链尖、确认数阈值 |
| PUT  | `/tokens/:tokenId` | 登记/推进元数据修订，body `{ "root_cid": "…" }`；版本号取自链状态，JSON 内 `version` 必须匹配 |
| GET  | `/tokens` / `/tokens/:tokenId` | 列表 / 当前状态（currentCid、状态、确认数） |
| GET  | `/tokens/:tokenId/history` | 全部修订（含 revoked，旧 CID 与锚点保留） |
| GET  | `/tokens/:tokenId/revisions/:v/evidence` | 该修订登记时刻的证据快照 |

错误统一为 `{ "error": CODE, "message": …, "details": … }`，关键码：
`UNSUPPORTED_CID / UNSUPPORTED_ENCODING / UNSUPPORTED_MULTIHASH / UNSUPPORTED_CODEC /
CONTENT_MISMATCH / INVALID_JSON / INVALID_METADATA / BLOCK_NOT_FOUND / DAG_CYCLE /
BLOCK_TOO_LARGE / VERSION_CONFLICT / CHAIN_CONFLICT / DEEP_REORG_REJECTED / INVALID_BASE64`。

### curl 最小示例

```bash
# 元数据 + 图片字节见 examples/manifest.json（由脚本真实计算）
CID=$(jq -r .rootCidV1 examples/manifest.json)
curl -s -X PUT "http://127.0.0.1:3000/blocks/$CID" \
  -H 'content-type: application/json' --data-binary @examples/put-metadata-v1.json

curl -s -X POST http://127.0.0.1:3000/verify \
  -H 'content-type: application/json' -d "{\"root_cid\":\"$CID\"}" | jq
```

---

## 5. 需求逐条对照（含测试位置）

| 要求 | 实现 / 测试 |
|---|---|
| 限定 CID 版本与哈希算法 | 仅 CIDv0/CIDv1 + sha2-256/32B；`test/cid.test.ts`（未知 multibase、identity、截断摘要、CIDv2、dag-cbor 全部拒绝） |
| 真实内容寻址，非字符串外形 | 入库与遍历均复算 sha2-256；`verifier.test.ts`「tampered stored bytes…」「tampered bytes are rejected by BlockService」、`api.test.ts` 篡改 PUT |
| JSON 编码真实校验 | 自写解析器：重复键、lone surrogate、严格 UTF-8；`codec.test.ts` |
| 媒体引用与大小 | 魔数嗅探 + 类型/大小比对；`verifier.test.ts` 媒体类型/大小不匹配 |
| 未知编码拒绝 | multibase 白名单 + 未知 multicodec + 非 image 签名；`cid.test.ts`、`api.test.ts`「unknown CID encodings」 |
| 每层引用的校验依据 | 每个节点 `checks[]` + claimed/computed digest；`/verify` 与修订证据快照 |
| 篡改字节 | 入库 422 + 存储层篡改检测；见上 |
| 循环引用 | 引用栈检测（注意：真实内容寻址下循环在数学上不可构造——每个 CID 都承诺其后继；测试通过显式 test seam 伪造存储态来验证防护，生产路径不使用该开关） |
| 缺块 | `BLOCK_NOT_FOUND` 带引用路径；`api.test.ts`「ad-hoc verify of missing block」 |
| 非 UTF-8 | `INVALID_JSON`；`examples/put-non-utf8.json`、`api.test.ts` |
| 重复版本 | 严格 +1，409；`api.test.ts`「duplicate / old metadata version」 |
| 改版保存旧 CID 与生效块 | `revisions` 表不删除，`/history` 可查；reorg 测试断言旧 CID/anchorHeight 保留 |
| 链重组撤销未确认更新 | `reorganize` 事务 + 状态重算；`api.test.ts`「reorg revokes the unconfirmed update」 |
| 计算/密码操作真实、失败如实报告 | node:crypto SHA-256、常量时间摘要比较；CID 结果用 Python 独立实现交叉验证（`test/cid.test.ts` 注释）；无 mock，无网络调用 |

限制：链块哈希由调用方提供（服务不做 PoW / 不模拟真实共识）；媒体仅 PNG/JPEG/GIF/WebP；
不解析 dag-pb 的 UnixFS 内部结构（CIDv0 可解析校验但不能作为 JSON DAG 成员）。
