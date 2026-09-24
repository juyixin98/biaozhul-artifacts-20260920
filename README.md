# 一致性哈希再平衡服务（Go / net/http）

纯后端服务：加权一致性哈希的**虚拟节点路由**与**精确迁移计划**生成。无界面，仅提供
JSON HTTP 接口。哈希算法与排序规则全部固定，同一份配置在任何机器上生成完全相同的环
与迁移计划。

- 语言：Go 1.23（仅标准库，`net/http` 路由）
- 外部依赖：**无**（没有任何第三方包，因此没有 `go.sum`；`go.mod` 即为依赖锁定）
- 状态：内存态，重启清空；环一经创建不可变（适合作为配置快照）

## 目录结构

```
.
├── go.mod                         # 模块定义；无 require，依赖已锁定为标准库
├── cmd/server/main.go             # HTTP 服务入口
├── internal/
│   ├── ring/ring.go               # 确定性加权一致性哈希环（固定哈希/排序）
│   ├── plan/plan.go               # 精确迁移计划：无缝无重的整数区间
│   └── server/server.go           # JSON HTTP API
├── examples/
│   ├── demo.sh                    # curl 端到端示例脚本
│   ├── sample-route.pretty.json   # 真实路由响应样例
│   └── sample-plan.json           # 真实迁移计划响应样例（已截断长列表）
└── README.md
```

## 算法约定（固定，不可配置）

| 项 | 约定 |
|---|---|
| 键哈希 | `SHA-256(key)` 取前 8 字节，**大端序**解释为 `uint64`，空间 `[0, 2^64)` |
| 虚拟节点键 | `<节点ID>#vn<序号>`，序号 `0 … weight*vnodes_per_weight_unit-1` |
| 节点虚节点数 | `weight * vnodes_per_weight_unit`；`weight=0` 视为 1；默认每权重 64 个虚节点 |
| 环排序 | 依次按 `(位置, 节点ID, 虚节点序号)` 升序，完全确定，与节点输入顺序无关 |
| 归属规则 | 位置 `h` 归属于第一个 `位置 >= h` 的虚节点；没有则环绕到最小虚节点。即位置 `p` 处的虚节点拥有左开右闭段 `(前一不同位置, p]`，最小虚节点额外拥有环绕段 |

因为排序与哈希都固定，**权重变化的结果可复现**：打乱输入节点顺序、重新构建环，
虚节点布局、迁移区间、键计数逐字节一致（有自动化测试与线上核对验证）。

## 迁移计划语义

给定旧环、新环：

1. 收集**新旧两环全部虚节点位置**作为切点，去重升序；
2. 相邻切点之间，旧归属与新归属均恒定，形成一个段；
3. 输出在整数空间 `[0, 2^64)` 上无缝、无重叠、完整覆盖的**包含式区间** `[lo, hi]`；
4. `from_node != to_node` 的段即迁移段，并按 `(from, to)` 归并为 `moves`；
5. 给出每个区间的精确键数（`hi-lo+1`，用 `big.Int` 承载 `2^64`）、总迁移/未迁移键数、
   新环各节点精确份额分数与小数。

JSON 中所有 64 位及以上整数均以**十进制字符串**输出（另附 `0x` 十六进制），
避免 JavaScript 等客户端的数字精度丢失。

不变量（测试强制保证）：

- 所有段 `lo <= hi`、严格首尾相接（下一段 `lo = 上一段 hi + 1`），首段 `lo=0`、末段 `hi=2^64-1`；
- 各段 `key_count` 之和恰为 `2^64`；
- 迁移段键数之和 = 各 move 键数之和 = `moved_keys`；`moved_keys + unchanged_keys = 2^64`；
- 迁移段集合与其精确区间一一对应，无缝无重；
- 每个被探针键实际哈希到的位置，计划标注的 from/to 与两环直接路由结果一致。

## 依赖与构建

需要 Go 1.23+。无第三方依赖，无需联网拉包。

```bash
go build ./...            # 编译
go vet ./...              # 静态检查
gofmt -l .                # 格式检查（应为空）
go test ./...             # 全部自动化测试
go test -race -count=1 ./...   # 带竞态检测
go test -cover ./internal/...  # 覆盖率
```

## 启动

```bash
go run ./cmd/server -addr :8080
# 或先构建：go build -o ch-server ./cmd/server && ./ch-server -addr :8080
```

启动后日志打印监听地址。健康检查：`GET /health` → `{"status":"ok"}`。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/rings` | 列出已存储环名 |
| POST | `/api/rings` | 创建环（重名返回 409） |
| GET | `/api/rings/{name}` | 环摘要；加 `?vnodes=1` 返回全部虚节点 |
| GET | `/api/rings/{name}/route?key=a&key=b` | 单/多键路由（可重复 `key` 参数） |
| POST | `/api/rings/{name}/route` | 批量路由，体 `{"keys":[...]}` |
| POST | `/api/plan` | 生成迁移计划；`old`/`new` 可为**已存环名**或**内联环定义** |

### 创建环

```bash
curl -s -X POST http://127.0.0.1:8080/api/rings \
  -H 'Content-Type: application/json' -d '{
    "name": "cluster-v1",
    "vnodes_per_weight_unit": 64,
    "nodes": [
      {"id": "node-a", "weight": 1},
      {"id": "node-b", "weight": 2}
    ]
  }'
```

字段：`name`（必填）、`nodes[].id`（必填、唯一）、`nodes[].weight`（容量权重，0 视作 1）、
`vnodes_per_weight_unit`（可选，默认 64）。

### 路由

```bash
curl -s "http://127.0.0.1:8080/api/rings/cluster-v1/route?key=user:1001&key=user:1002"
curl -s -X POST http://127.0.0.1:8080/api/rings/cluster-v1/route \
  -H 'Content-Type: application/json' -d '{"keys":["user:1001","user:1002"]}'
```

返回每个键的固定哈希位置（十进制 + 十六进制）与归属节点，样例见
`examples/sample-route.pretty.json`。

### 迁移计划

```bash
curl -s -X POST http://127.0.0.1:8080/api/plan \
  -H 'Content-Type: application/json' -d '{
    "old": "cluster-v1",
    "new": "cluster-v2",
    "probe_keys": ["user:1001", "user:1002"]
  }'
```

- `"old"`/`"new"`：写字符串表示引用已创建的环；写对象表示内联临时定义（无需先建环），
  便于对"加节点 / 删节点 / 改权重"直接出计划。
- `probe_keys`：可选。对这些键同时给出新旧归属与 `migrates` 标记，方便抽样核对。

响应含 `segments`（完整区间）、`moves`（按 from→to 归并的区间集合）、`node_shares`
（新环精确份额）、迁移/未迁移键数与段数。完整字段样例见 `examples/sample-plan.json`。

一把梭示例：

```bash
BASE=http://127.0.0.1:8080 ./examples/demo.sh
```

## 自动化测试

- `internal/ring/ring_test.go`：固定哈希测试向量、输入顺序无关、边界点归属（含哈希碰撞）、
  加权/等权分布（大量随机键）、5 万键路由稳定性。
- `internal/plan/plan_test.go`：
  - 加节点、删节点、改权重、整体替换、单节点环、虚节点密度变化等拓扑变化；
  - 每轮强制校验**无缝无重完整覆盖 `[0,2^64)`**、切点即段边界、move 与段一一对应、份额合计 `2^64`；
  - 数万随机键 + 每个切点及其相邻点，计划标注与双环直接路由逐一核对；
  - 改权重后打乱输入重建，计划 JSON **逐字节相同**（可复现）；
  - 24 轮随机拓扑属性测试；大整数 JSON 序列化测试。
- `internal/server/server_test.go`：全 HTTP 流程（建环/查重/路由/计划/内联环/各类 400/404/409）。

## 实测结果（2026-09-24，Go 1.23.4，linux/amd64）

下列结果均为本机实际运行所得：

**单元/集成测试**

```
go vet ./...                → clean
gofmt -l .                  → clean
go test -race -count=1 ./...
  ok  consistenthash/internal/plan    5.707s   （10 个测试）
  ok  consistenthash/internal/ring    2.700s   （10 个测试）
  ok  consistenthash/internal/server  1.029s   （5 个测试）
go test -cover ./internal/...
  plan 98.8% | ring 78.8% | server 85.5%
```

**针对验收点的端到端核对（真实运行服务 + 独立 Python 参考实现交叉验证）**

- 增删节点后路由：对 **1,000,000 个随机键**用独立实现复算哈希与归属，与服务返回的计划
  区间逐一比对，**mismatch = 0**；迁移键恰好落在唯一 move 区间，未迁移键不落入任何 move 区间。
- 未受影响区间不迁移：3 等权 → 4 等权（加 `node-d`），全部 move 的目标仅为 `node-d`，
  老节点之间零迁移；迁移键数占比 **0.2560**（理论约 1/4）。
  删节点场景同样验证只有被删节点的键迁出，存活节点之间零迁移。
- 区间覆盖无缝无重：程序严格断言段首尾相接、首 0 末 `2^64-1`、键数合计 `2^64`
  （实际输出 `18446744073709551616`），HTTP 返回也通过同样校验。
- 权重可复现：1:1 → 3:1 改权重，两次请求计划 JSON 完全一致，打乱节点顺序后虚节点布局
  逐元素相同；该拓扑下迁移仅 `b→a`，3000 探针键中 859 个迁移且全部被唯一区间精确覆盖、
  0 泄漏。新环 a 的**精确**份额为 `0.775896281300`（虚节点离散化使其不严格等于 0.75，
  属一致性哈希的固有现象；加大 `vnodes_per_weight_unit` 可逼近理论权重比例）。
- 错误处理：重名 409、未知环 404、非法 JSON / 空节点 / 重复节点 / 空 key / 未知环名 400，
  均已实测确认。

> 说明：一致性哈希只能在统计意义上按权重分配，少量虚节点时精确份额与权重比例存在离散
> 偏差；这是算法本身特性而非缺陷。默认每权重 64 虚节点在均匀性与计划体积之间取平衡。

## 已知限制 / 未完成项（如实记录）

- **纯内存、单实例**：无持久化、无多副本；重启后已建环丢失。计划接口支持内联定义，
  无状态也可使用。
- **无鉴权 / 限流 / TLS**：仅适合内网或本地使用，未实现认证、请求体大小限制与 HTTPS。
- **无节点增删改与删除环的 HTTP 端点**：环为不可变快照；演进拓扑通过"新建环 / 内联定义
  + `/api/plan"`表达（这正是再平衡的推荐用法）。
- **不实际搬移数据**：服务只产出精确迁移区间与归属，不连接任何存储执行数据迁移；
  区间可直接作为下游数据搬迁任务的输入（按哈希位置范围扫描）。
- **计划体量随虚节点数线性增长**：段数约为两环切点数 +1。超大集群、超大
  `vnodes_per_weight_unit` 时单次响应可能很大，未做分页/流式输出。
- 未提供语言客户端 SDK；接口为简单 JSON，curl 即可调用。
