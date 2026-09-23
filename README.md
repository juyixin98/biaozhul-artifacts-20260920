# 一致性哈希再平衡服务（Weighted Consistent Hash Rebalancer）

纯后端服务：加权一致性哈希环 + 虚拟节点路由 + **精确迁移计划**。
仅使用 Go 标准库（`net/http` 等），无任何第三方依赖。无界面，全部通过 HTTP/JSON 交互。

- 语言：Go 1.23（仅标准库）
- 监听：`0.0.0.0:8080`
- 修订（revision）模型：每次增删/替换节点生成一个**不可变**新版本；可查询任意两个保留版本之间的迁移计划。

---

## 1. 设计约定（全部固定，保证结果可复现）

| 项目 | 固定规则 |
|---|---|
| 哈希空间 | 圆环 `[0, 2^64-1]`，总长度 `2^64` |
| 哈希函数 | **FNV-1a 64 位**（常数与标准库 `hash/fnv` 完全一致）后接 **Murmur3 `fmix64` 雪崩混合**。纯算术、确定性；`fmix64` 是双射，不改变“不同输入不冲突”的性质。 |
| 为什么要再混合 | 原始 FNV-1a 对“只在末尾变化的短串”雪崩很差，而虚拟节点键恰好是 `<id>#00000000`、`<id>#00000001`…；直接用会让同一节点的成百上千个虚拟节点**挤在同一个小扇区**（实测 128 个全落进同一个 1/8 桶），负载严重不均。加 `fmix64` 后恢复均匀分布。 |
| 虚拟节点数 | `weight × base_vnodes`，`base_vnodes` 默认 64（`replace` 时可配） |
| 虚拟节点键 | `"<节点ID>#<8位定长十进制序号>"`，例如 `node-a#00000007`；节点 ID 不允许含 `#` |
| 虚拟节点排序 | `(hash 升序, 节点ID 字典序, 副本序号 升序)`，哈希碰撞也有确定的全序 |
| 键的归属 | 哈希 `h` 归属于**第一个 hash ≥ h 的虚拟节点**；没有则回绕到最小 hash 的虚拟节点。每个虚拟节点拥有其与前驱之间的**半开弧 `(前驱hash, 本hash]`** |
| 半开弧方向 | 沿圆环正向：`(start_exclusive, end_inclusive]`，`wrap=true` 表示跨过 0 点 |

### 迁移计划如何保证“精确、无缝、无重”

1. 收集**新旧两环全部虚拟节点 hash** 作为切点，升序去重；
2. 在切点之间形成常量属主的最小区段（含跨 0 的回绕段），逐段比对旧/新属主；
3. 相邻且 `(旧属主, 新属主)` 相同的区段合并成**最大弧**；
4. 输出 `moved`（属主改变）与 `stayed`（属主不变）两类弧。

所有弧（moved ∪ stayed）两两不相交、首尾相接，长度之和严格等于 `2^64`。
`moved` 即“原归属 → 新归属”的精确区间；`stayed` 即**不迁移区间**，用于显式证明哪些范围不动。

---

## 2. 启动与依赖

### 依赖

- Go ≥ 1.23（本机实测 go1.23.4 linux/amd64）
- **无第三方 Go 依赖**。`go.mod` 无 `require`，因此没有也不需要 `go.sum`；
  `GOFLAGS=-mod=readonly go build ./...` 验证通过（即“依赖已锁定”：锁定结果为零外部依赖，可离线构建）。

### 构建与运行

```bash
# 在项目根目录
go build -o consistent-hash .
./consistent-hash
# => consistent-hash listening on :8080
```

或直接：

```bash
go run .
```

### 运行测试

```bash
go test ./...            # 全部单元 + HTTP 集成测试
go test -race ./...      # 竞态检测
go test -cover ./...     # 覆盖率
go test -v -run TestPlan ./...
```

---

## 3. HTTP 接口

所有请求/响应均为 JSON。错误形如：`{"error":"bad_request","message":"..."}`。

| 方法与路径 | 说明 |
|---|---|
| `GET /healthz` | 健康检查 |
| `GET /v1/ring?revision=N&include_vnodes=1` | 查看某版本环；省略 `revision` 取最新；`include_vnodes=1` 返回全部虚拟节点（含十进制与 0x 十六进制 hash） |
| `GET /v1/revisions` | 列出全部保留版本 |
| `POST /v1/nodes` | 新增节点：`{"id":"node-a","weight":3}`，`weight` 省略默认 1；同 ID 同权重幂等（200，不产生新版本） |
| `DELETE /v1/nodes/{id}` | 删除节点，产生新版本 |
| `POST /v1/ring/replace` | 整体替换节点集合 / 调权重；可带 `base_vnodes` 配置虚拟节点密度 |
| `GET /v1/route?key=...&revision=N` | 单个键路由；省略版本取最新；空环返回 503 |
| `POST /v1/route/batch` | 批量路由：`{"keys":[...],"revision":N}`，`revision=0` 或缺省表示最新 |
| `GET /v1/plans?from=N&to=M` | 迁移计划；省略时默认“上一版 → 最新版” |

修订保留上限 100 个（版本 0 空环永不淘汰）。请求体上限 1 MiB，未知字段拒绝。

### 计划响应字段

```jsonc
{
  "from_revision": 4,
  "to_revision": 5,
  "moved": [                 // 属主改变的最大弧
    {
      "from": "node-a", "to": "node-e",
      "arc": {
        "start_exclusive": 274614742614373320,   // 不含
        "end_inclusive": 355597040026631308,     // 含
        "wrap": false,
        "start_exclusive_hex": "0x03cfa0a7479d73c8",
        "end_inclusive_hex": "0x04ef559fb5f7bc8c",
        "length": "80982297412257988"            // 该弧 hash 位置数
      }
    }
  ],
  "stayed": [ ... ],         // 属主不变的最大弧（显式给出“不迁移区间”）
  "node_flows": [            // 每节点 incoming / outgoing / unaffected 长度
    {"node":"node-e","incoming_length":"2376340660037113037",
     "outgoing_length":"0","unaffected_length":"0"}
  ],
  "total_length": "18446744073709551616",        // 恒为 2^64
  "moved_length": "2376340660037113037",
  "moved_fraction": 0.12882168530889376
}
```

`from=""` 表示旧环为空（初次分配）；`to=""` 表示新环为空（排空）。
所有长度用十进制字符串输出（可能超过 2^53，避免 JS 等客户端精度丢失），并附 16 位十六进制。

---

## 4. 请求样例（curl）

```bash
# 健康检查
curl -s localhost:8080/healthz

# 构建加权环（权重 3:1:2:1）
curl -s -X POST localhost:8080/v1/nodes -d '{"id":"node-a","weight":3}'
curl -s -X POST localhost:8080/v1/nodes -d '{"id":"node-b","weight":1}'
curl -s -X POST localhost:8080/v1/nodes -d '{"id":"node-c","weight":2}'
curl -s -X POST localhost:8080/v1/nodes -d '{"id":"node-d","weight":1}'
# => 最新版本 revision=4

# 单键 / 批量路由
curl -s "localhost:8080/v1/route?key=user:1001"
curl -s -X POST localhost:8080/v1/route/batch \
  -d '{"keys":["user:1001","user:1002","order:abc"]}'

# 增加一个节点 -> revision=5，并查看 4->5 的精确迁移计划
curl -s -X POST localhost:8080/v1/nodes -d '{"id":"node-e","weight":1}'
curl -s "localhost:8080/v1/plans?from=4&to=5" | jq .

# 调整权重 / 删节点（整体替换）-> revision=6
curl -s -X POST localhost:8080/v1/ring/replace -d '{
  "nodes":[
    {"id":"node-a","weight":6},
    {"id":"node-c","weight":2},
    {"id":"node-d","weight":1},
    {"id":"node-e","weight":1}
  ]
}'
curl -s "localhost:8080/v1/plans?from=5&to=6" | jq .

# 从空环到当前版本的全量初次分配计划
curl -s "localhost:8080/v1/plans?from=0&to=6" | jq .
```

---

## 5. 验收点与实测结果（本机真实运行记录）

环境：go1.23.4 linux/amd64。命令均已实际执行。

- `go vet ./...`：无问题；`gofmt`：全部格式化。
- `go test ./...`：**PASS**；`go test -race ./...`：**PASS**；覆盖率 **89.4%**。
- `GOFLAGS=-mod=readonly go build ./...`：**通过**（零外部依赖、可离线构建）。

### 5.1 增删节点后对大量键核对路由

- 固定随机种子生成 **20,000 个键**，分别按 revision 4 与 5 批量路由；
  再与 `/v1/plans?from=4&to=5` 的每个弧逐一核对：
  - 落在 `moved` 弧中的键，实际属主必然改变且 `from/to` 与两次路由完全一致；
  - 落在 `stayed` 弧中的键，实际属主必然不变。
  - **实测：20000 键，0 失配**；实测迁移比例 `0.1263`，计划理论值 `0.1288`（20k 采样正常波动）。
- 单元测试另含：`TestPlanAddNode_PartitionsAndMatchesRouting`（10 万随机哈希 + 全部边界点）、
  `TestPlanFuzzManyConfigurations`（60 组随机增删/权重/`base_vnodes` 配置，边界点 + 数千随机点）、
  HTTP 层 `TestHTTPLargeKeySetMatchesPlan`、并发 20 路增节点的 `TestHTTPConcurrentMutations`。

### 5.2 未受影响区间不迁移

- `TestRemoveNodeOnlyLosesItsKeys`：50,000 键，删除某节点后，**只有原本属于该节点的键改道**，其余键全部留在原节点（`key moved although owner was not the removed node` 即判失败）。
- 计划同时返回 `stayed` 弧与每节点 `unaffected_length`；权重调整 5→6 实测：
  node-a 扩容但 `outgoing=0`（它不收的键不迁走），node-b 被删则全量 `outgoing=2528823261522701302`，
  其余节点满足 `新份额 = unaffected + incoming`、`旧份额 = unaffected + outgoing`（`TestPlanNodeFlows` 强校验，守恒恒等式逐节点成立，全局 incoming 总和 == outgoing 总和 == moved_length）。

### 5.3 区间覆盖无缝、无重

- 计划中的全部弧展开为线性 `[lo,hi)` 区间后按起点排序，要求严格 `lo == 当前游标` 且最终游标 `== 2^64`；
  任意哈希位置必须且只能命中一个弧（`containingArc` 命中数恰为 1，否则测试失败）。
- 实测 4→5 计划：379 个线性区间**首尾相接、无重叠、总长度恰为 2^64 = 18446744073709551616**。
- 对应测试：`assertTilesCircle`（被每个计划测试调用）。

### 5.4 权重效果与可复现

- 权重 3:1:2:1、20 万随机键，实测各节点份额（`TestWeightedShare`，容差 ±0.02）：
  约 **0.4354 / 0.1635 / 0.2511 / 0.1500**，与权重占比 3/8、1/8、2/8、1/8 吻合。
- 加一个等权节点（10 → 11 节点）迁移量约 **1/11**（`TestAddOneNodeMovesAboutOneNth`，实测落区间内）。
- 可复现：
  - 相同节点集合以不同传入顺序构建，虚拟节点序列逐字节相同（`TestRingIndependentOfInsertionOrder`）；
  - 用相同配置独立重建两个 Ring，50,000 键路由完全一致（`TestWeightChangeReproducible`）；
  - 相同配置独立构建的计划 **JSON 序列化逐字节相同**（`TestPlanDeterministicAcrossRuns`）；
  - 哈希实现与标准库 `hash/fnv` 交叉验证（`TestFNVMatchesStandardLibrary`），并对 fmix64 做 20 万输入无碰撞与均匀桶分布测试（`TestHashUniformity`）。

---

## 6. 源码结构

| 文件 | 内容 |
|---|---|
| `ring.go` | 固定哈希（FNV-1a + fmix64）、虚拟节点、加权环、路由 |
| `plan.go` | 切点切段、最大弧合并、迁移/驻留计划、节点流量统计 |
| `server.go` | `net/http` 服务、版本管理、全部 HTTP handler |
| `*_test.go` | 单元 + 计划数学性质 + HTTP 集成/并发测试 |
| `go.mod` | 模块声明，无第三方 require |

---

## 7. 未完成项 / 已知边界

- **无持久化**：版本只保存在内存，进程重启后从空环（revision 0）重新开始；计划本身是确定性的，只要重新执行相同的节点操作序列即可复现相同版本与计划。
- **监听地址固定为 `:8080`**：暂未提供命令行 flag / 环境变量配置（如需可加，约几行）。
- **版本保留上限 100**：超出后淘汰最旧版本（但永不淘汰版本 0）；被淘汰版本之间的历史计划将返回 `unknown_revision`。当前所有相邻版本始终可用。
- **半开弧端点语义**：弧是 `(start, end]`（前开后闭），切点本身归后一个虚拟节点；这与“第一个 hash ≥ h”的路由规则一致，消费方迁移时需按同一端点约定执行。
- 未做鉴权/TLS/限流（按“纯后端、本地服务”定位，未在需求范围内）。
