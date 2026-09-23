# seqcep — 复杂事件序列匹配（A → B → C）

纯 JDK 实现的后端服务：按实体（entity）分组，在 **10 秒窗口**内匹配 **A 后 B 后 C** 的事件序列。
无界面，仅 HTTP API；**零第三方依赖**（HTTP 服务用 JDK 内置 `com.sun.net.httpserver`，
JSON 为自带的最小解析器，测试为自带极简框架）。

## 功能与语义

- **模式**：同一实体内，事件 A、B、C 满足 `tsA <= tsB <= tsC` 且 `tsC - tsA <= 10000ms`
  （窗口边界**含** 10000ms 整）即构成一次匹配。
- **分组**：匹配状态严格按 `entity` 隔离，绝不跨实体组合。
- **跳过无关事件**：`type` 非 A/B/C 的事件会被持久化记录（保证可重放），但不影响匹配状态。
- **重叠匹配**：部分匹配状态在命中后**不消费**。`A,A,B,C` 产生 **2** 次匹配
  （两个 A 各自与同一对 B、C 组合）；`A,A,B,B,C` 产生 **4** 次（2×2 全组合）。
- **相同时间戳排序**：时间戳相同的事件按**输入序号**（服务端分配的全局递增 `seq`）决定先后。
  例如同一时刻先 B 后 A，则该 B 不能与该 A 配对。
- **禁止静默截断**：
  - `/v1/matches` 不带 `limit` 时返回**全部**匹配；带 `limit` 时响应里显式给出
    `total` 与 `hasMore`，截断永远显式可见。
  - WAL 损坏（CRC 不符、尾部撕裂）时**拒绝启动并明确报错**，绝不自动丢弃数据。
- **故障恢复**：每个事件在应答前追加并 `fsync` 到 WAL（`data/events.log`，
  记录格式：magic + 长度 + CRC32 + JSON）。重启时按序重放，确定性地重建
  部分匹配状态与全部匹配结果。`kill -9` 后状态可完整恢复（见下方实测）。

## 依赖（锁定）

见 [dependencies.lock](dependencies.lock)。要点：**仅需 JDK 17+**，无任何第三方依赖。
本机验证环境：OpenJDK 17.0.20.1 / javac 17.0.20.1。

## 启动

```bash
./scripts/run.sh
# 或自定义：
SEQCEP_PORT=8080 SEQCEP_DATA_DIR=./data java -cp build/classes seqcep.Main
```

环境变量：`SEQCEP_PORT`（默认 8080，0 表示随机端口）、`SEQCEP_DATA_DIR`（默认 `./data`）、
`SEQCEP_ENGINE_ID`（默认 `default`）。

## 测试

```bash
./scripts/test.sh    # 编译并运行全部 26 个自动化测试
```

## HTTP API

### `POST /v1/events` — 写入事件（单个或批量）

请求体三种形式均可：单个对象 `{"type":"A","entity":"e1","ts":1000}`、
`{"events":[...]}`、或裸数组 `[...]`。字段：`type`（字符串）、`entity`（字符串）、
`ts`（epoch 毫秒，整数）。批内事件按数组顺序分配 `seq` 并逐条落盘。

```bash
curl -X POST localhost:8080/v1/events -d '{"events":[
  {"type":"A","entity":"dev-1","ts":1000},
  {"type":"A","entity":"dev-1","ts":1500},
  {"type":"B","entity":"dev-1","ts":2000},
  {"type":"C","entity":"dev-1","ts":3000}]}'
```

响应（节选）：`{"accepted":4, "events":[{...,"seq":1}...], "totalMatches":2, "matchesByEntity":{"dev-1":2}}`

### `GET /v1/matches?entity=<id>&limit=<n>&offset=<n>`

`entity` 可选；不带 `limit` 返回全部（`hasMore:false`）。

```bash
curl "localhost:8080/v1/matches?entity=dev-1"
```

### `GET /v1/state` — 部分匹配状态快照（用于恢复一致性核对）

返回每个实体未完成的 `openA` / `openAB` 链、`matchCount`、全局 `lastSeq` 等。

### `DELETE /v1/state` — 清空状态并轮换 WAL

### `GET /healthz` — 存活探针

错误统一返回 `{"error":"...","status":<code>}`：JSON 畸形/缺字段 → 400，
未知路由 → 404，方法错误 → 405。

## 实测记录（2026-09-23，本机 OpenJDK 17.0.20.1）

- `./scripts/test.sh`：**26/26 通过**。覆盖：两个验收场景（`A,A,B,C`→2；`A,B,超时,C`→0）、
  窗口边界（10000ms 命中 / 10001ms 不命中）、无关事件跳过、同时间戳按 seq 排序、
  实体隔离、全组合枚举（`A,A,B,B,C`→4）、乱序事件不倒退匹配、过期部分链清理、
  崩溃后部分状态指纹一致、WAL 截断/位翻转显式报错、reset 轮换、HTTP 端到端与分页。
- 真实进程演示（curl 实测）：
  - `A,A,B,C`（dev-1）→ `totalMatches: 2` ✅
  - `A,B,超时,C`（dev-2，C 距 A 12 秒）→ `total: 0` ✅
  - `kill -9` 后重启：`/v1/state` 中 dev-1 的 2 条 openA、2 条 openAB 与崩溃前**完全一致**，
    匹配列表 2 条完整恢复，`lastSeq` 连续（新事件分配到 seq 8），
    恢复出的待完成链补一个 C 后正常命中（总匹配 2→4）✅

## 已知限制 / 未完成项

- 事件按**到达顺序**处理；`ts` 倒退的乱序事件不会改写历史（不会与时间上更早的
  后续事件反向配对），这是有意语义而非缓冲重排。
- WAL 只增不减，长期运行需自行归档（`DELETE /v1/state` 可重置）；暂无快照压缩。
- 单机单进程；无鉴权、无 TLS（前置反向代理可自行叠加）。
- 批量的每条事件单独 fsync（保证崩溃语义明确），高吞吐场景可改为组提交——未实现。
