# stable-pager — 查询游标稳定分页（版本化结果集）

纯后端 Java 服务，用 JDK 内置 `com.sun.net.httpserver.HttpServer` 实现，**零第三方依赖**。
解决排序键重复时的稳定分页问题：游标绑定「查询 + 快照 + 最后排序元组」，HMAC 防篡改，
快照过期明确报错。分页过程中对数据做插入、删除、修改排序键，同一次遍历不重不漏。

## 1. 工作原理

### 稳定次序：`(排序键, 唯一ID)` 全序

排序键可重复（演示数据里 `score=50` 有 5 行）。排序时先按用户选定的键
（`score` / `name` / `createdAt` / `updatedAt`，asc/desc），键相同再按唯一 `id`
**升序**（与方向无关）兜底。这样行与行之间是全序，翻页用 keyset 定位
「严格大于最后一个元组」，重复键不会导致跳行或重行，也不存在 OFFSET 漂移。

### 版本化结果集（快照）

- 内存 MVCC：每次增删改把版本号 `version++`。
- 不带游标请求首页时，对当前全部数据做**防御性拷贝**生成快照（不可变）。
- 后续每页携带游标，游标签发时的快照 id 决定读哪个历史版本；遍历期间的插入/删除/改键
  对本次遍历完全不可见（见 `MvccStore`、`PaginationService`）。
- 快照有 TTL（默认 60s，每次续用刷新），后台守护线程清理。过期/不存在的快照返回
  **HTTP 410 `SNAPSHOT_EXPIRED`**，要求从首页重新开始，而不是悄悄返回错误数据。

### 游标结构与 MAC 防篡改

线上格式：

```
v1.<urlsafe-base64 payload>.<urlsafe-base64 HMAC-SHA256 tag>
```

payload（JSON）绑定：

| 字段 | 含义 |
|---|---|
| `f` | 查询指纹：`nameContains|category|sort|order|limit` 的规范化串 |
| `s` | 快照 id |
| `v` | 快照版本号（与快照内版本必须一致） |
| `k` | 本页最后一行的排序键值 |
| `id` | 本页最后一行的唯一 id（续页起点） |

`tag = HMAC-SHA256(PAGER_MAC_SECRET, "v1." + payloadBytes)`，常量时间比较。
改任何一个字节、用别的密钥签名（如换密钥后重放、另一台服务签发）都返回
**HTTP 400 `CURSOR_INVALID`**。查询参数（筛选/排序/方向/页大小）与指纹不一致时返回
**HTTP 400 `QUERY_MISMATCH`**，错误体里同时回显游标查询和当前请求，方便排查。

## 2. 环境要求与启动

- **JDK 17+**（验证版本：Temurin 17.0.20.1+1）。不需要 Maven/Gradle，不需要联网。
- 仅用到 `java.base` 模块（HTTP server、`javax.crypto`、`java.net.http`）。

```bash
# 如机器无 JDK，可解压免安装 JDK 后指向它：
# export JAVA_HOME=/path/to/jdk-17

./build.sh          # javac 编译到 build/
./test.sh           # 编译并运行自动化测试（纯 main 程序，无需 JUnit）
./run.sh            # 启动服务，默认 http://localhost:8080
```

环境变量（均可选）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8080` | 监听端口 |
| `PAGER_SNAPSHOT_TTL_MS` | `60000` | 快照存活时间；空闲超过即失效 |
| `PAGER_MAC_SECRET` | 启动时随机生成 | 游标 HMAC 密钥。**生产必须显式设置**；不设置时重启后旧游标全部失效（启动日志有 WARNING） |

可选：`pom.xml` 描述了空依赖集，`mvn compile` 可离线工作；测试仍以 `./test.sh` 为准
（不引入 surefire/JUnit，保持依赖树为空）。依赖锁定见 [`DEPENDENCIES.md`](./DEPENDENCIES.md)。

## 3. HTTP 接口

请求/响应均为 JSON。错误响应统一为：

```json
{ "error": "ERROR_CODE", "message": "human readable", "details": { } }
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/api/items` | 分页查询（见下） |
| POST | `/api/items` | 新建，body：`id,name,category,score`（均必填） |
| GET | `/api/items/{id}` | 查单行（读最新 live 数据） |
| PATCH | `/api/items/{id}` | 部分更新，body 可含 `name`/`category`/`score` |
| DELETE | `/api/items/{id}` | 删除 |

### GET `/api/items` 查询参数

| 参数 | 默认 | 取值 |
|---|---|---|
| `nameContains` | 无 | 名称子串（大小写不敏感） |
| `category` | 无 | 类目精确匹配 |
| `sort` | `score` | `score` / `name` / `createdAt` / `updatedAt` |
| `order` | `asc` | `asc` / `desc` |
| `limit` | `20` | 1–100 |
| `cursor` | 无 | 上一页响应里的 `nextCursor`（URL 编码后传回） |

响应：

```json
{
  "items": [ { "id": "...", "name": "...", "category": "...", "score": 50,
               "createdAt": 1790162290264, "updatedAt": 1790162290264 } ],
  "nextCursor": "v1.eyJ...<signed>...",
  "page": {
    "snapshotId": "bd52…", "snapshotVersion": 23,
    "snapshotCreatedAt": 1790162345450, "snapshotExpiresAt": 1790162405450,
    "snapshotTtlMillis": 60000,
    "sort": "score", "order": "asc", "limit": 5, "count": 5, "hasMore": true,
    "filters": { "nameContains": null, "category": null }
  }
}
```

末页 `nextCursor` 为 `null`、`hasMore=false`。

错误码：

| HTTP | code | 触发 |
|---|---|---|
| 400 | `CURSOR_INVALID` | 游标格式/Base64/HMAC/版本/最后元组校验失败（含伪造、外站密钥） |
| 400 | `QUERY_MISMATCH` | 游标指纹与本次筛选/排序/方向/页大小不一致 |
| 410 | `SNAPSHOT_EXPIRED` | 快照已过期或不存在 |
| 400 | `INVALID_LIMIT` / `INVALID_SORT` / `INVALID_ORDER` | 参数非法 |
| 400 | `MISSING_FIELD` / `INVALID_FIELD` / `UNKNOWN_FIELD` / `EMPTY_PATCH` / `BAD_JSON` | body 校验 |
| 404 | `NOT_FOUND` | 行或路径不存在 |
| 409 | `ID_CONFLICT` | 新建时 id 已存在 |
| 405 | `METHOD_NOT_ALLOWED` | 方法不支持 |
| 413 | `BODY_TOO_LARGE` | body 超 64 KiB |

## 4. 请求样例（curl）

启动后服务自带 23 行演示数据（5 行 `score=50` 制造重复排序键）。

```bash
BASE=http://localhost:8080

# 首页
curl -sS "$BASE/api/items?limit=5"

# 翻页：把上一页 nextCursor URL 编码后传回（推荐 -G/--data-urlencode）
CURSOR='v1.eyJ...上一页的nextCursor...'
curl -sS -G "$BASE/api/items" --data-urlencode "limit=5" --data-urlencode "cursor=$CURSOR"

# 筛选 + 降序 + 换排序键
curl -sS "$BASE/api/items?category=music&sort=name&order=desc&limit=3"
curl -sS "$BASE/api/items?nameContains=alp"

# 增 / 改 / 删（每次改动都会 bump 版本，但不影响进行中的旧快照遍历）
curl -sS -X POST "$BASE/api/items" -H 'Content-Type: application/json' \
  -d '{"id":"x-1","name":"X","category":"books","score":33}'
curl -sS -X PATCH "$BASE/api/items/x-1" -H 'Content-Type: application/json' -d '{"score":34}'
curl -sS -X DELETE "$BASE/api/items/x-1"

# ① 换筛选条件复用旧游标 -> 400 QUERY_MISMATCH
curl -sS -G "$BASE/api/items" --data-urlencode "category=books" \
  --data-urlencode "limit=5" --data-urlencode "cursor=$CURSOR"

# ② 伪造游标（改一个字符）-> 400 CURSOR_INVALID
curl -sS -G "$BASE/api/items" --data-urlencode "limit=5" --data-urlencode "cursor=${CURSOR/x/y}"

# ③ 快照过期（TTL 后）-> 410 SNAPSHOT_EXPIRED
```

或直接运行 `./examples.sh`（脚本会自动完成「遍历中增删改→证明快照稳定→伪造/错用游标」全套演示）。

## 5. 自动化测试

`test/com/example/stablepager/StablePagerTests.java` 是零依赖端到端测试：真实启动 HTTP
服务（随机端口、800ms 短 TTL），用 JDK `HttpClient` 打真实请求。`./test.sh` 退出码非零
即失败。覆盖（共 63 条断言）：

1. **重复排序键稳定**：23 行全量遍历不重不漏；`score=50` 的 5 行严格按 id 排序；
   `limit` 取 1/2/7/23/100 时遍历结果完全一致；降序同样正确。
2. **遍历中突变（验收核心）**：拿到第 1 页后插入 3 行（分数位于区间内/最高/最低）、
   删除 2 行（含尚未翻到的）、修改 2 行排序键（1 行已见、1 行未见），随后继续翻页：
   仍是快照里的原 23 行、无重漏、快照 id 不变；另起首页则看到全部变更且版本号变大。
3. **筛选稳定**：过滤遍历中途插入同筛选行，旧遍历不可见；子串/类目过滤计数正确。
4. **变更查询不能复用游标**：改 category、加 nameContains、换 sort、换 order、改 limit、
   无筛选游标拿去做筛选，全部 400 `QUERY_MISMATCH`。
5. **伪造游标**：垃圾串、错版本前缀、截断、非法 Base64、翻转 payload/tag 字节、
   另一密钥签名，全部 400 `CURSOR_INVALID`；合法签名但快照不存在 → 410；
   合法签名但版本号不符 → 400。
6. **快照过期**：TTL 后续用 → 410 `SNAPSHOT_EXPIRED`；TTL 内续用正常。
7. **CRUD 与参数校验**：201/409/200/404、类型错误、坏 JSON、limit/sort/order 非法、
   405 等。
8. `CursorCodec` 单元级往返与异密钥/垃圾串测试。

实测结果见 [`RESULTS.md`](./RESULTS.md)。

## 6. 目录结构

```
src/com/example/stablepager/
  Main.java              入口（env 配置、种子数据）
  WebRuntime.java        JDK HttpServer 装配、路由、JSON 收发
  ItemController.java    参数校验与请求映射
  PaginationService.java keyset 分页、(sortKey,id) 全序比较、快照读取
  MvccStore.java         MVCC 版本存储 + 快照注册表（TTL/清理）
  QuerySpec.java         查询参数解析、查询指纹、过滤与排序键
  CursorCodec.java       v1 游标编解码与 HMAC-SHA256 校验
  Item.java / Json.java / ApiException.java / SeedData.java
test/…/StablePagerTests.java  端到端测试（main 程序）
build.sh / test.sh / run.sh / examples.sh
pom.xml（可选，空依赖集）  DEPENDENCIES.md（依赖锁定）  RESULTS.md（实测记录）
```

## 7. 设计取舍与边界

- 全量快照拷贝是为「同一快照无重漏」付出的代价；TTL 限制其生命周期，后台线程清理。
  生产可换成追加式列存/多版本数据库（思路不变：游标只存版本与最后元组）。
- HMAC 提供**完整性与来源认证**，不是加密：payload 可被 base64 解码查看（不含敏感筛选
  时可接受）。密钥须经 `PAGER_MAC_SECRET` 注入并妥善保管。
- 并发续用刷新同一快照的 TTL；快照不可变，无需加读锁。
- 仅实现需求内功能，无界面、无鉴权、无持久化（重启数据回到种子集）。
