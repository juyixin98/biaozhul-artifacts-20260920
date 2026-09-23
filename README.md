# 流式分位数摘要服务 (Streaming Quantile Summary Service)

纯 Java 后端服务，实现**可合并的 ε-近似分位数摘要**。不保存任何原始样本，
只保存亚线性规模的摘要元组（tuple），支持分片（shard）合并、序列化传输和
参数兼容性检查。仅依赖 JDK 自带的 HTTP 服务器（`com.sun.net.httpserver`），
**零第三方依赖**，离线即可构建和运行。

- 算法：Greenwald–Khanna（GK）ε-approximate quantile summary
- 语言/运行时：Java 17（仅用 JDK，不含任何外部库）
- 构建：`javac`（仓库自带脚本，无需 Maven/Gradle）

---

## 1. 算法与秩误差保证

### 1.1 数据结构

摘要维护一个按值排序的元组序列 `(v, g, Δ)`：

- `v`：一个观测到的值；
- `g`：相邻元组最小可能秩之差，`rmin(v_i) = Σ_{j≤i} g_j`；
- `Δ`：秩不确定带宽，`rmax(v_i) = rmin(v_i) + Δ_i`。

始终保留精确的最小值和最大值（`Δ = 0` 哨兵）。

### 1.2 核心不变量与误差界

每次插入和压缩后，对所有元组满足 GK 不变量（论文 Corollary 1）：

```
g_i + Δ_i ≤ ⌊2 ε n⌋
```

论文 Proposition 1 据此保证：对任意 φ ∈ [0,1]，目标秩 `r = ⌈φ n⌉`，必存在
某个存储元组 v 使

```
r − rmin(v) ≤ ε n   且   rmax(v) − r ≤ ε n
```

即**加性秩误差不超过 ε·n**；等价地，返回值落在真实顺序统计量
`x_{(φ−ε)n}` 与 `x_{(φ+ε)n}` 之间。`/quantile` 接口在所有满足双侧条件的
元组中选择离目标秩最近的一个；φ=0/φ=1 精确返回最小值/最大值。

### 1.3 关于重复值（重要且诚实的说明）

当大量观测取**同一个值**时，该值的真实秩 `rankLE(v)` 可以是一个很宽的
tie 块里的任意位置。任何亚线性摘要都不保存原始重数，因此无法把
`rankLE(v)` 定位到 εn 内。本服务采用论文定义的**值空间保证**：返回值是
跨越目标秩的真实顺序统计量（即答案值本身正确）。在连续/不同值数据上，
这与 `|rankLE(v) − r| ≤ εn` 的秩距离界等价；测试对两者都做了区分性验收。

`/rank`（CDF：给定值求估计秩）在不同值数据上误差 ≤ εn。

### 1.4 合并（分片聚合）

两个用**相同 ε** 构建的摘要可以合并为表示两流并集的新摘要，输入不被修改。
采用加权元组构造：

1. 将两路元组按值交错；
2. 来自 X 路的元组 `(v,g,Δ)` 进入并集时保留权重 g，其秩不确定度加上另一路
   的秩松弛 `2 ε n_Y`（界定另一路有多少观测可能排在 v 之前）；
3. 全局最小/最大保持精确（Δ=0），并把任何超过 N 的 rmax 截断到 N；
4. 在合并长度 N = n₁+n₂ 上做一次 GK 压缩，恢复 `g+Δ ≤ 2εN` 不变量。

因此合并结果保持 **ε·N 的分位数秩误差界**。支持链式合并与平衡树合并
（测试覆盖 8 分片左折叠和树折叠）。

**参数兼容检查**：ε 不同直接拒绝并返回 HTTP 422；序列化快照还校验
`algorithm` / `version` / `order`，外部算法（如 t-digest）、未来版本、
不同排序、被篡改（g 之和 ≠ n 等）的快照一律拒绝。

### 1.5 内存规模

空间复杂度 O((1/ε) · log(ε n)) 个元组，与观测数无关。固定种子的实测：

| 场景 | n | ε | 元组数 | 约占 n | 近似字节 |
|---|---|---|---|---|---|
| 升序 | 100,000 | 0.01 | 74 | 0.074% | ~1.8 KB |
| Pareto(1.1) | 200,000 | 0.01 | 226 | 0.113% | ~5.4 KB |
| 重尾内存规模测试 | 2,000,000 | 0.005 | 790 | 0.0395% | ~19 KB |

服务从不保存原始样本数组。

参考：Greenwald, M. and Khanna, S. (2001), *Space-Efficient Online
Computation of Quantile Summaries*, SIGMOD.

---

## 2. 依赖

**零第三方运行时/编译期依赖。**

- 唯一依赖：JDK 17+（使用 `com.sun.net.httpserver.HttpServer`、
  `java.net.http.HttpClient` 等标准模块）。
- JSON 解析/序列化为本仓库自带的最小实现（`Json.java`）。
- 无 `pom.xml`/`build.gradle`/`pom.lock`；“锁定依赖”即明确的空依赖集合，
  任何装有 JDK 17 的环境离线可复现构建。

构建/测试脚本：

- `scripts/build.sh`：用 `javac` 编译主程序到 `build/classes`。
- `scripts/test.sh`：编译并运行全部 4 个测试套件。
- `scripts/run.sh`：启动 HTTP 服务。

---

## 3. 启动

```bash
# 需要 JDK 17
java -version

# 编译
scripts/build.sh

# 启动（默认 0.0.0.0:8080）
scripts/run.sh
# 或指定地址/端口
scripts/run.sh --host 127.0.0.1 --port 9090
```

启动后：`GET /healthz` → `{"status":"ok"}`。

摘要保存在进程内存中；重启后清空。需要持久化/跨节点传输时使用
`/snapshot`（见下）。

---

## 4. HTTP 接口

所有请求/响应均为 JSON（流式写入接口除外）。ID 允许字符
`A-Z a-z 0-9 . _ -`，长度 ≤ 128。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/v1/summaries` | 列出所有摘要元数据 |
| PUT | `/v1/summaries/{id}` | 创建摘要，body `{"epsilon":0.01}`（默认 0.01） |
| GET | `/v1/summaries/{id}` | 查看元数据（计数、元组数、误差界） |
| DELETE | `/v1/summaries/{id}` | 删除 |
| POST | `/v1/summaries/{id}/observations` | 批量写入，body `{"values":[...]}` |
| POST | `/v1/summaries/{id}/observations/stream` | 空白分隔文本流式写入 |
| GET | `/v1/summaries/{id}/quantile?q=0.5` | 单个分位数 |
| GET | `/v1/summaries/{id}/quantile?qs=0.5,0.9,0.99` | 多个分位数 |
| GET | `/v1/summaries/{id}/rank?value=42` | 值的估计秩与 CDF |
| GET | `/v1/summaries/{id}/snapshot` | 导出序列化快照 |
| PUT | `/v1/summaries/{id}/snapshot` | 导入快照（含完整性校验） |
| POST | `/v1/merge` | 分片合并，body 见下 |

状态码：200/201 成功；400 请求非法；404 摘要不存在；405 方法不允许；
409 查询空摘要；**422 摘要参数不兼容，拒绝合并/导入**；5xx 内部错误。

### 创建

```bash
curl -s -X PUT http://localhost:8080/v1/summaries/orders-eu \
  -H 'Content-Type: application/json' \
  -d '{"epsilon":0.01}'
```

`epsilon` 必须在开区间 (0,1)。响应含 `count`、`storedTuples`、
`errorBoundRank = ⌊εn⌋`。

### 写入观测值

JSON 批量：

```bash
curl -s -X POST http://localhost:8080/v1/summaries/orders-eu/observations \
  -H 'Content-Type: application/json' \
  -d '{"values":[12.5, 13, 13, 42.0, 7, 99.25]}'
```

文本流式（空白分隔，每行一个或多个均可，适合管道/大文件）：

```bash
seq 1 1000000 | curl -s -X POST \
  http://localhost:8080/v1/summaries/orders-eu/observations/stream \
  --data-binary @-
```

非有限值（NaN/Infinity）返回 400。单请求 JSON body 上限 64 MiB，
`values` 数组上限 500 万元素。

### 查询

```bash
curl -s 'http://localhost:8080/v1/summaries/orders-eu/quantile?qs=0.5,0.9,0.99'
curl -s 'http://localhost:8080/v1/summaries/orders-eu/rank?value=42'
```

`quantile` 响应中每个结果含 `q`、`value`、`estimatedRank`、
`errorBoundRank`。`rank` 响应含 `estimatedRank`、`cdf`、`errorBoundRank`。

### 快照（序列化）

```bash
curl -s http://localhost:8080/v1/summaries/orders-eu/snapshot > snap.json
curl -s -X PUT http://localhost:8080/v1/summaries/restored/snapshot \
  -H 'Content-Type: application/json' \
  -d "{\"snapshot\":$(jq -c '.snapshot' snap.json)}"
```

快照是自描述的版本化 JSON：

```json
{
  "algorithm": "gk",
  "version": 1,
  "order": "double-natural",
  "epsilon": 0.01,
  "n": 123456,
  "tuples": [ {"v": 1.5, "g": 12, "d": 1840}, ... ]
}
```

导入时校验：算法名/版本/排序匹配、ε 合法、元组按值有序、g>0、Δ≥0、
Σg == n。任何不符都拒绝（400 或 422）。

### 分片合并

先在各分片（或各节点导入快照）上构建摘要，然后聚合为**新**摘要：

```bash
curl -s -X POST http://localhost:8080/v1/merge \
  -H 'Content-Type: application/json' \
  -d '{"id":"orders-all","sources":["orders-eu","orders-us","orders-ap"]}'
```

也可以直接合并请求体内携带的快照（跨节点、无需先注册）：

```bash
curl -s -X POST http://localhost:8080/v1/merge \
  -H 'Content-Type: application/json' \
  -d '{"id":"orders-remote","snapshots":[ <snapshot1>, <snapshot2> ]}'
```

`sources` 与 `snapshots` 可同时使用。目标 `id` 必须不存在；所有参与方
ε 必须一致，否则返回 **422** 且不产生任何摘要。合并结果保持 ε 误差界。

完整可运行的命令见 `examples/requests.sh`。

---

## 5. 自动化测试

```bash
scripts/test.sh
```

四个套件（均为纯 JDK，无测试框架）：

- `GKCoreTest`：升序/降序/全相等/二元 tie/小字母表/均匀/Pareto(1.1)/
  对数正态/对抗交错，固定随机种子，对照精确排序副本检查误差界；
  200 万样本的内存规模检查（元组数 << n，尾部分位数在界内）。
- `GKMergeTest`：2/8 分片链式与树式合并、重 tie 合并、空合并、12 组随机
  fuzz、快照往返、**拒绝 ε 不兼容合并**、拒绝外部算法/未来版本/被篡改快照。
- `JsonTest`：JSON 编解码往返与非法输入拒绝。
- `HttpIT`：在随机端口启动真实服务器，端到端覆盖全部接口（含 422 拒绝、
  404/400/409、分片合并精度）。

---

## 6. 已知边界 / 未完成项

- 摘要是**进程内存态**，无内置持久化（用快照导入/导出实现持久化与搬迁）。
- 仅支持有限 double 的自然全序；不支持字符串/自定义比较器（快照中
  `order` 字段已为此预留，遇到非 `double-natural` 直接拒绝）。
- 重 tie 场景下 `/rank` 对单一值重数无法精确恢复（见 1.3），这是亚线性
  分位数摘要的固有信息论限制；分位数值本身仍满足值空间保证。
- 无鉴权/TLS；定位为内网或边车（sidecar）服务。
