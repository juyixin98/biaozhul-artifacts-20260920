# 查询游标稳定分页（Java + JDK HttpServer）

纯后端、零第三方依赖的版本化结果集（snapshot）游标分页服务。排序键可重复，
以唯一 `id` 作为最终决胜键形成全序；游标绑定**查询、快照、最后排序元组**，
用 HMAC-SHA256 防篡改；快照过期返回明确的 `410 snapshot_expired`。

- 语言/运行时：Java 17（仅用 JDK 自带类，含 `com.sun.net.httpserver.HttpServer`、`javax.crypto`）
- 第三方依赖：**无**（JSON 解析/序列化、测试框架均为项目内极简实现）
- 构建方式：`javac` 直接编译，无需 Maven/Gradle

## 目录结构

```
src/com/example/paginate/
  Main.java                    启动入口（PORT / SNAPSHOT_TTL / CURSOR_SECRET 环境变量）
  model/Item.java              业务记录（id 唯一，name/score 为可重复排序键）
  store/ItemStore.java         内存存储，整体替换式更新保证快照不可变
  snapshot/Snapshot.java       快照记录（id/version/createdAt/items）
  snapshot/SnapshotManager.java 快照注册表：TTL 过期判定、惰性清理
  query/PageQuery.java         查询定义、查询指纹、(排序键,id) 全序比较器
  cursor/CursorPayload.java    游标负载
  cursor/CursorService.java    游标 HMAC 签发/校验
  page/PaginationService.java  分页核心：首页建快照，后续页按锚点严格推进
  web/HttpServerApp.java       路由与装配
  web/ItemsResource.java       GET /api/items
  web/AdminResource.java       增删改接口（供分页途中制造变更）
  web/HttpSupport.java / QueryParams.java / ApiException.java
  json/Json.java               零依赖 JSON
test/com/example/paginate/     24 个自动化测试（9 单元 + 15 HTTP 端到端）
scripts/
  build.sh                     编译
  test.sh                      编译并运行全部测试
  run.sh                       启动服务
  demo.sh                      curl 端到端演示（输出 demo-output.txt）
  verify.py                    Python 标准库验收脚本（输出 verify-output.txt）
test-output.txt                自动化测试实际运行输出（已存档）
demo-output.txt                curl 演示实际运行输出（已存档）
verify-output.txt              验收脚本实际运行输出（已存档）
```

## 依赖（已锁定）

| 依赖 | 版本 | 来源 |
|---|---|---|
| JDK | Eclipse Temurin 17.0.20.1+1 (x64 linux) | Adoptium API `latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse` |

本次运行所用 tarball 的 SHA-256（`/tmp/jdk.tar.gz`，Adoptium 官方制品）：

```
3808d1d15e3ec6bd5b84057fb5d84c33d8a1536a258146bcea2e603fc726e08e
```

除 JDK 外**没有任何需要锁定的第三方库**：编译、运行、测试均只用 `java.base`。
> 本环境无 root 权限、系统未装 JDK，因此把 Temurin 解压到了 `~/jdk17`。
> 任意 JDK 17+（Oracle/OpenJDK/Temurin/Zulu 等）均可，下面命令用 `JAVA_HOME` 指向它。

## 启动命令

```bash
# 1) 编译（需要 javac；本机示例：export JAVA_HOME=$HOME/jdk17）
scripts/build.sh

# 2) 启动（默认端口 8080，快照 TTL 300 秒）
scripts/run.sh                # 等价：PORT=8080 SNAPSHOT_TTL=300 java -cp build/classes com.example.paginate.Main
# 或自定义：scripts/run.sh 18099 3
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 8080 | 监听端口，0 表示系统分配 |
| `SNAPSHOT_TTL` | 300 | 快照存活秒数；过期后游标报 410 |
| `CURSOR_SECRET` | 每次启动随机 | 游标 HMAC 密钥；不固定时重启后旧游标全部失效（测试/演示用固定值） |

服务启动时自带 57 条种子数据（19 个名字 × 3 个分类，名字与分数刻意大量重复）。

## HTTP 接口与请求样例

### 1. 分页查询 `GET /api/items`

查询参数：

| 参数 | 取值 | 默认 |
|---|---|---|
| `sort` | `name_asc` / `name_desc` / `score_asc` / `score_desc` | `name_asc` |
| `category` | 精确筛选 `books`/`music`/`movies` | 不过滤 |
| `q` | name 子串（大小写不敏感） | 不过滤 |
| `pageSize` | 1..100 | 10 |
| `cursor` | 上一页返回的 `nextCursor`，原样回传 | 无（首页） |

**第一页（建立快照）：**

```bash
curl -sS 'http://localhost:8080/api/items?sort=name_asc&pageSize=5'
```

响应（节选）：

```json
{
  "items": [
    {"id": 1,  "name": "alpha", "category": "books",  "score": 0},
    {"id": 20, "name": "alpha", "category": "music",  "score": 50},
    {"id": 39, "name": "alpha", "category": "movies", "score": 40},
    {"id": 2,  "name": "beta",  "category": "books",  "score": 10},
    {"id": 21, "name": "beta",  "category": "music",  "score": 0}
  ],
  "nextCursor": "eyJ2Ijox...（不透明字符串，下次原样回传）",
  "hasMore": true,
  "snapshotId": "8ba228dbbcb904389b126443",
  "snapshotVersion": 1,
  "pageSize": 5,
  "count": 5,
  "query": {"sort": "name_asc", "category": null, "q": null, "pageSize": 5}
}
```

注意重名 `alpha` 的三条按唯一 `id`（1 < 20 < 39）决胜，顺序确定。

**下一页（复用同一快照）：**

```bash
curl -sS --get 'http://localhost:8080/api/items' \
  --data-urlencode 'sort=name_asc' \
  --data-urlencode 'pageSize=5' \
  --data-urlencode 'cursor=eyJ2Ijox...'
```

末页 `"nextCursor": null, "hasMore": false`。游标推进是无状态的，
同一游标重复请求返回相同的下一页，不会"消费"游标。

**带筛选：**

```bash
curl -sS 'http://localhost:8080/api/items?sort=score_desc&category=books&q=a&pageSize=3'
```

### 2. 数据变更接口（用于在分页过程中制造插入/删除/改排序键）

```bash
# 插入（可不传 id 自动分配；category 默认 default；score 默认 0）
curl -sS -X POST 'http://localhost:8080/api/admin/items' \
  -H 'Content-Type: application/json' \
  -d '{"name":"zzz-new","category":"books","score":999}'

# 修改（PATCH 语义：只更新提供的字段；改 name/score 即改排序键）
curl -sS -X PATCH 'http://localhost:8080/api/admin/items/50' \
  -H 'Content-Type: application/json' \
  -d '{"name":"aaa-moved","score":5}'

# 删除
curl -sS -X DELETE 'http://localhost:8080/api/admin/items/10'

# 其它
curl -sS 'http://localhost:8080/api/admin/items/50'   # 查单条
curl -sS 'http://localhost:8080/api/admin/stats'      # {"total":57}
curl -sS 'http://localhost:8080/healthz'
```

### 错误响应

统一形如 `{"error":"<code>","message":"..."}`：

| 场景 | HTTP | error |
|---|---|---|
| 游标被篡改/伪造/密钥不符/缺 MAC | 403 | `cursor_invalid` |
| 游标属于另一个查询（改了 sort/category/q/pageSize） | 400 | `cursor_query_mismatch` |
| 快照已过 TTL | **410** | `snapshot_expired` |
| 快照 id 不存在（被清理/编造） | 404 | `snapshot_not_found` |
| 游标版本不支持 / 参数非法 | 400 | `cursor_version_unsupported` / `invalid_sort` / `invalid_page_size` 等 |
| 记录不存在 | 404 | `item_not_found` |
| 指定 id 冲突 | 409 | `id_conflict` |

## 设计要点（为什么能稳定）

1. **全序排序**：所有排序都是 `(排序键, id)` 复合比较，排序键重复时唯一 id 决胜，
   不存在并列，因此锚点比较无歧义。
2. **版本化快照**：每次第一页把当前全量数据做不可变拷贝（更新采用"整体替换"，
   快照持有的旧对象永不被原地修改）。翻页期间对活数据的插入/删除/改排序键
   都不影响已建快照——同一快照遍历**不重不漏**，新记录不会从后续页"冒出来"。
3. **游标三元绑定**：
   - 查询指纹（`sort|category|q|pageSize` 的 SHA-256 截断），换任一参数 → `cursor_query_mismatch`；
   - 快照 id，过期 → 410、不存在 → 404，绝不静默回退到最新数据；
   - 最后排序元组 `(排序键值, id)`，下一页只取严格排在该元组之后的记录。
4. **MAC 防篡改**：游标为 `base64url(JSON负载).base64url(HMAC-SHA256(负载))`，
   先验签后解码，常量时间比较；改任意字节/换密钥签名均 403。
5. **快照清理**：数量超阈值才惰性清扫，过期记录保留一个"墓碑窗口"（2×TTL），
   使过期访问在窗口内稳定得到 410 而不是 404。

## 自动化测试

```bash
scripts/test.sh
```

- 9 个直接单元测试：快照 TTL 边界（410）、不存在（404）、MAC 改字符/换密钥、
  查询指纹敏感性、重复键 id 决胜、快照与写入隔离、参数校验。
- 15 个真实 HTTP 端到端测试（JDK HttpClient，服务 TTL=1s）：四种排序全量翻页不重不漏、
  翻页**途中插入/删除/改排序键**、换筛选/排序/页大小复用游标被拒、
  四类游标伪造被拒、过期 410、CRUD、组合筛选、游标可安全重复使用等。

**实际运行结果（2026-09-23，Temurin 17.0.20.1）：24/24 全部通过**，完整输出见 `test-output.txt`。

## 验收场景复现（对应任务的验收要求）

```bash
# 方式一：curl 演示（自动起停服务，含分页途中增删改/篡改/过期）
scripts/demo.sh                 # 输出存到 demo-output.txt

# 方式二：带断言的 Python 标准库验收脚本（需先自己起服务）
PORT=18099 SNAPSHOT_TTL=3 scripts/run.sh 18099 3 &
python3 scripts/verify.py http://localhost:18099 3   # 输出见 verify-output.txt
```

**实际运行结果（2026-09-23）：22/22 全部通过**（`verify-output.txt`），覆盖：

- 翻到第 2 页时插入 1 条、删除 1 条、把 1 条记录改名到最前；
  旧快照仍遍历出建快照时的全部 57 条、无重复 id、新记录不混入、被删/被改记录仍按旧快照出现；
- 新快照：净条数正确、不含已删记录、包含新记录、改名记录排到首位、snapshotId 已换；
- 换 category / sort / pageSize 复用游标 → 均 400 `cursor_query_mismatch`；
- 翻转游标 1 个字符、完全伪造 → 均 403 `cursor_invalid`；
- 等待 TTL+1 秒 → 410 `snapshot_expired`。

## 已知限制 / 未完成项（如实说明）

- **内存存储**：数据和快照都在单进程内存中，重启即清空；非持久化、不支持多实例。
  生产形态应由数据库的 MVCC/事务快照提供版本化能力，游标只需携带快照版本号而非拷贝全量。
- **全量快照拷贝 + 全量过滤排序**：实现优先保证正确性，未做键集（keyset）下推、
  索引或增量快照；超阈值才惰性清理，超大结果集不适用。
- **游标签名只防篡改，不防泄露**：持有游标者可在 TTL 内继续读该快照（数据不敏感，未做用户授权）。
- **无并发游标写入**：HTTP 线程池为 8，存储写操作加锁；快照拷贝与写入的隔离已覆盖，
  但未做高并发压测。
- 时间戳、分数用 long；JSON 仅支持本项目需要的类型子集；查询参数只取最后一个同名值。
- `snapshot_not_found` 在端到端层以"过期但仍保留墓碑窗口 → 410"和单元测试的直接 404 覆盖；
  游标中编造快照 id 在真实流程里会先被 MAC 拦住（无法在不知密钥时构造合法签名），无法到达 404 分支。
