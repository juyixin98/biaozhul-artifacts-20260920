# NFT 元数据一致性验证服务（本地、内容寻址、无网关）

纯后端服务，用来在**本地**验证 NFT 元数据与其引用媒体是否真正满足内容寻址一致性。
输入代币 ID、CID 和本地内容块，**全程不访问任何公网网关**：所有哈希、编解码、
协议解析、DAG 遍历都在进程内真实执行。

- 语言/运行时：TypeScript（ESM，Node.js ≥ 18.17）
- HTTP 框架：[Fastify](https://fastify.io) 4
- 存储：SQLite（`better-sqlite3`，WAL 模式，同步事务）
- 密码学：Node 内置 `crypto`（SHA-256，无第三方密码库）

> 编解码器（base58btc / base32 / multihash / CID / dag-pb / UnixFS / JSON / 媒体签名）
> 全部在 `src/codec/` 手写实现，不是字符串外形校验。`multiformats` 仅作为
> **开发期**参考实现出现在测试里做交叉验证，不在生产依赖中。

---

## 1. 它到底校验什么（每层的校验依据）

给定一个元数据 CID，服务递归解析本地 DAG，对**每一层**给出可机读的证据：

| 层 | 真实执行的校验 |
|---|---|
| **multihash** | 对**实际块字节**计算 SHA-256（`0x12`/32 字节），与 CID 携带的摘要逐位比较；声明长度与剩余字节不一致即拒 |
| **CID** | 只接受 CIDv0（base58btc、34 字节 `0x12 0x20…`）和 CIDv1（multibase `b` 小写无填充 base32，codec `dag-pb=0x70`/`raw=0x55`）。CIDv2、其他 multibase（`z`/`m`…）、其他哈希（identity/sha1/sha512）、其他 codec（dag-cbor…）一律拒绝 |
| **protobuf** | 手写严格解码器：真实 tag/varint 进位运算；拒绝 groups(wire 3/4)、fixed32/64、非最小 varint（如 `80 00`）、字段号 0、截断、尾随垃圾 |
| **dag-pb** | 处理历史特例（`Links` 与 `Data` 在 wire 上都用 tag `0x0a`，按位置和 field-1 wire 类型判别）；每个链接必须有 CID；链接名按字节序排序且唯一；`Name` 严格 UTF-8；规范再编码必须与原块逐字节相等 |
| **UnixFS** | 解析真实 Data 消息；只接受 File(2)/Raw(0) 作为载荷；`Type`、`filesize`、逐条 `blocksizes` 必须与解析出的子块实际字节数一致；声明的 `Tsize` 必须等于按子 DAG 重新计算的累计大小 |
| **JSON** | 先以 fatal 模式严格 UTF-8 解码（拒绝非 UTF-8、CESU-8 孤立代理 `ED A0 80`），再走手写 RFC 8259 文法解析器：**重复键拒绝**、拒绝注释/尾逗号/`NaN`/孤立 `\u` 代理；超 2^53 整数用 bigint 保真 |
| **元数据模型** | 白名单字段（`name/description/image/animation_url/external_url/attributes/properties`）；未知字段拒绝；`image`/`animation_url` 只接受内容寻址的 `ipfs://<cid>[/path]`，路径段 percent-decode 后再做严格 UTF-8 校验；`http(s)` 网关引用拒绝 |
| **媒体编码** | 按**内容魔数**识别（不看文件名/声称的 Content-Type）：PNG（含真实 IHDR 宽高）、JPEG、GIF、WEBP、SVG、MP4/ISO-BMFF（ftyp brand）、OGG、MP3、WAV、PDF；识别不出的编码拒绝；按拼装后的真实字节数做大小上限校验 |

返回体里每层都有 `hashCheck / encodingCheck / unixfsCheck / links[].tsize*`，
每条都带人类可读的 `basis`（依据）。见 `examples/sample-verification-report.json`。

### 版本与链重组

- 元数据改版为**只追加**：每次提案成为一个新版本，旧版本 CID 与**生效块哈希/高度**永久保留（状态 `historical`）。
- 提案在 `CONFIRMATIONS`（默认 3）个区块之后才生效。
- 报告链重组（`/chain/reorg`）时，**未确认**的更新被撤销（状态 `reorged`），此前已生效的 CID 继续有效；已经确认的版本**永不被静默回滚**，而是作为 `protectedConfirmed` 返回。
- `(token, CID)` 去重：同一 CID 的存活版本不能重复提案（409），但被重组撤销的 CID 可以重新提案。
- 所有 PROPOSE/CONFIRM/REORG 写入只追加的 `events` 审计表。

---

## 2. 快速开始

```bash
npm install          # 安装依赖并生成 package-lock.json（已锁定）
npm run gen-fixtures # 生成真实的本地 NFT DAG（图片目录 + 分片视频 + 元数据）
npm start            # 启动服务，默认 http://127.0.0.1:3000
```

生成的样例：

- `examples/payloads/metadata.json` — 人类可读的元数据
- `examples/blocks/<cid>.b64`、`examples/blocks/all-blocks.json` — 每个内容块（base64）
- `examples/fixture-cids.json` — 根/目录/叶子 CID 与字节数
- `examples/sample-verification-report.json` — 一份真实的分层校验报告
- `examples/demo.sh`、`examples/adversarial.sh` — 见第 5 节

环境变量：`HOST`、`PORT`、`DB_PATH`、`LOG_LEVEL`、`MAX_BLOCK_SIZE`（默认 1 MiB）、
`MAX_METADATA_BYTES`（256 KiB）、`MAX_MEDIA_BYTES`（2 MiB）、
`MAX_DAG_DEPTH`（16）、`MAX_LINKS_PER_NODE`（1024）、`CONFIRMATIONS`（3）。

---

## 3. API（均为 JSON，本地接口）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/blocks` | 录入一个内容块 `{cid,dataBase64}`；先做 SHA-256/规范校验，不合法返回 422 |
| GET  | `/api/v1/blocks/:cid?raw=1` | 块信息与校验报告；`raw=1` 返回原始字节 |
| POST | `/api/v1/verify/cid/:cid` | 仅校验已存储的单个块 |
| POST | `/api/v1/verify/metadata` | `{cid}` 全量校验元数据 DAG 与全部媒体引用（内容失败返回 200 报告的 422 包装） |
| PUT  | `/api/v1/tokens/:tokenId/metadata` | 提案改版 `{cid,height,blockHash}`（必须先通过全量校验） |
| GET  | `/api/v1/tokens/:tokenId` | 当前生效版本、待确认提案、完整版本历史 |
| POST | `/api/v1/chain/tip` | `{height,blockHash}` 推进链尖峰；达到确认数的提案自动生效 |
| POST | `/api/v1/chain/reorg` | `{height,oldHash,newHash}` 撤销未确认更新；`oldHash:"*"` 匹配该高度任意哈希 |
| GET  | `/api/v1/events?limit=N` | 只追加审计账本 |
| GET  | `/healthz` | 健康检查 |

最小手动流程：

```bash
# 1) 录入块
curl -X POST localhost:3000/api/v1/blocks -H 'content-type: application/json' \
  -d '{"cid":"<cid>","dataBase64":"<base64 块字节>"}'

# 2) 全量校验
curl -X POST localhost:3000/api/v1/verify/metadata -H 'content-type: application/json' \
  -d '{"cid":"<元数据 CID>"}'

# 3) 提案（链上交易，高度 100）
curl -X PUT localhost:3000/api/v1/tokens/tok-1/metadata -H 'content-type: application/json' \
  -d '{"cid":"<元数据 CID>","height":100,"blockHash":"0xaaaa1111"}'

# 4) 推进到 103（3 个确认后生效）
curl -X POST localhost:3000/api/v1/chain/tip -H 'content-type: application/json' \
  -d '{"height":103,"blockHash":"0xdddd4444"}'
```

---

## 4. 自动化测试与验收

### 一键验收（推荐）

```bash
npm test          # node:test，73 个用例全部真实执行（编解码/密码/协议/HTTP/链逻辑）
npm run build     # tsc 类型检查 + 编译到 dist/
```

测试覆盖（`test/`）：

- `cid.test.ts` — 受限 CID 版本/哈希/codec/multibase/长度，拒绝伪造外形
- `known-answer.test.ts` — 与 kubo 规范 CID 对齐的黄金向量
  （空节点、空 UnixFS 目录/文件，逐字节固定）
- `crosscheck.test.ts` — 用独立参考库 `multiformats` 对全部夹具 CID 做交叉验证
- `dagpb.test.ts` — protobuf 严格性、dag-pb 规范/排序/UTF-8/重复字段/Link-Data 判别
- `json-strict.test.ts` — 重复键、非 UTF-8、CESU-8、孤立代理、非法数字等
- `integration.test.ts` — 经真实 HTTP 的端到端对抗与链上生命周期

### 明确的对抗场景

| 攻击/异常 | 期望 | 测试位置 |
|---|---|---|
| **篡改字节**（翻转 1 个字节仍声称原 CID） | 422，`sha256-content-address.ok=false`，给出实际/声明摘要 | `integration.test.ts` “byte-flipped”、“stored-block tamper” |
| **循环引用** | 遍历栈检出祖先 CID，报 `cycle` 且终止（用伪造块存储重放真实 DAG 标签来构造环） | `integration.test.ts` “closes a cycle” |
| **缺块** | 422，阶段 `missing-block` | `integration.test.ts` “referenced block is missing” |
| **非 UTF-8 / 重复版本键** | JSON 层拒绝（fatal UTF-8 / duplicate key） | `json-strict.test.ts`、`integration.test.ts` |
| **重复版本** | 同一存活 `(token,CID)` 提案 → 409；被重组撤销后允许重提 | `integration.test.ts` 修订/重组用例 |
| 未知媒体编码 / 网关 URL / 错误 Tsize / 错误 blocksizes / 目录当文件 | 逐项拒绝并给出依据 | `integration.test.ts` |

### HTTP 端到端验收脚本

需要先 `npm run gen-fixtures && npm start`（另开一个终端保持服务运行）：

```bash
# 正常全链路：录入 -> 全量校验 -> 提案 -> 未确认 -> 重组撤销 -> 再提案 -> 生效
BASE=http://127.0.0.1:3000/api/v1 bash examples/demo.sh

# 对抗探针：篡改字节、非法 multibase/多哈希/CIDv2、缺块，全部必须被拒
BASE=http://127.0.0.1:3000/api/v1 bash examples/adversarial.sh
```

---

## 5. 目录结构

```
src/
  codec/
    base.ts           手写 base58btc（CIDv0）与 RFC4648 base32（CIDv1）
    multihash.ts      仅 sha2-256；真实 createHash('sha256') 校验
    cid.ts            CIDv0/v1 解析/构造/等价性（受限白名单）
    pb.ts             严格 protobuf 线格式解码器 + 编码器
    dag-pb.ts         IPLD dag-pb 规范模型（含 0x0a 历史特例判别）
    unixfs.ts         UnixFS Data 与 filesize/blocksizes 一致性
    json-strict.ts    严格 UTF-8 + 手写 JSON 文法（重复键拒绝）
    metadata-model.ts NFT 元数据白名单模型与 ipfs:// 解析
    media-sniff.ts    内容魔数签名识别
  verify/verifier.ts  递归 DAG 验证引擎，产出每层证据
  store.ts            SQLite 表结构与只追加版本/事件账本
  chain-service.ts    确认窗口与重组语义
  routes.ts  app.ts  server.ts  config.ts  errors.ts  zod-lite.ts
examples/             夹具、样例报告、demo/对抗脚本
test/                 node:test 单元 + 集成 + 交叉验证 + 黄金向量
```

## 6. 安全模型与边界

- 无任何出站网络调用；内容必须由调用方以本地块形式提供。
- 内容寻址是信任根：块一旦与自身 CID 不符即拒绝；不存在“外形像 CID 就通过”的路径。
- 深度、链接数、块大小、载荷大小均有上界，避免恶意 DAG 造成资源耗尽。
- 链为本地模拟（高度 + 区块哈希），用于演示确认/重组语义；它不连接真实链节点。
```
