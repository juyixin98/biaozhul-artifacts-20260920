# 区间重叠索引服务（Interval Overlap Index）

纯后端 HTTP 服务：存储整数端点的**左闭右开**区间 `[start, end)`，支持插入、
按 id 删除、交集查询、指定时刻覆盖计数。核心索引是**增强红黑树**（子树维护
`size` 与**最大端点 `maxEnd`**），全部基于 JDK 标准库，**零第三方依赖、零界面**。

## 1. 环境要求与依赖

| 项 | 要求 |
|---|---|
| JDK | **17+**（验证于 Eclipse Temurin `17.0.20.1+1`，见 `dependencies.lock`） |
| 第三方依赖 | **无**（不使用 Maven/Gradle，不下载任何 jar） |
| 用到的 JDK 模块 | `java.base`、`jdk.httpserver`（内置 `com.sun.net.httpserver.HttpServer`） |
| Shell | bash（仅构建脚本需要；命令也可直接手敲） |

设置 JDK（按需）：

```bash
export JAVA_HOME=/path/to/jdk-17
$JAVA_HOME/bin/java -version
```

依赖"锁定"见 [`dependencies.lock`](./dependencies.lock)：唯一被锁定的组件是
JDK 17，外部依赖清单为空。

## 2. 构建与启动

```bash
./build.sh                 # 编译 -> build/classes、build/test
./run.sh 8080              # 启动服务（端口可省略，默认 8080；也支持 PORT 环境变量）
```

等价的手工命令：

```bash
javac -encoding UTF-8 -d build/classes $(find src/main/java -name '*.java')
java -cp build/classes com.example.intervalindex.http.IntervalServer 8080
```

启动成功输出：`Interval index service listening on http://localhost:8080`。

## 3. 运行自动化测试

```bash
./test.sh
```

测试为纯 JDK 自包含套件（无 JUnit），包含：

- `IntervalIndexTest`：插入/删除/重复区间/嵌套/相邻端点/半开覆盖语义/非法区间拒绝；
  **随机差分测试**（万余次操作，每一步都把增强树结果与暴力扫描逐条对照：
  size、相交集合、覆盖计数、`maxEnd` 与红黑不变量）；单调插入的形状压力测试。
- `JsonTest`：内置 JSON 解析器往返与非法输入拒绝。
- `HttpApiTest`：启动真实 HTTP 服务，用 JDK `HttpClient` 做端到端测试。

退出码 0 表示全部通过。

## 4. HTTP 接口

所有请求/响应均为 `application/json; charset=utf-8`。区间端点为 64 位整数，
统一**左闭右开** `[start, end)`，要求 `start < end`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/intervals` | 插入区间，body `{"start":1,"end":5}`，返回分配的唯一 `id`（201） |
| `DELETE` | `/intervals/{id}` | 按 id 删除；成功 200，不存在/重复删除 404 |
| `GET` | `/intervals?start=&end=` | 返回所有与查询区间 `[start,end)` 相交的区间 |
| `GET` | `/intervals/count?at=t` | 返回覆盖时刻 `t`（`start <= t < end`）的区间数量 |
| `GET` | `/intervals/all` | 中序导出全部区间 |
| `GET` | `/health` | 健康检查与当前 size |

错误响应统一为 `{"error": "...", "status": 4xx}`：

- 空区间 `[x,x)`、逆序区间（`start >= end`）：插入与查询都返回 **400**；
- id 非数字：**400**；删除不存在的 id：**404**；未知路由：**404**。

### 请求样例（curl）

```bash
# 插入（嵌套 / 相邻 / 重复）
curl -s -X POST localhost:8080/intervals -H 'Content-Type: application/json' \
     -d '{"start":0,"end":100}'        # {"id":1,"start":0,"end":100}
curl -s -X POST localhost:8080/intervals -H 'Content-Type: application/json' \
     -d '{"start":10,"end":90}'
curl -s -X POST localhost:8080/intervals -H 'Content-Type: application/json' \
     -d '{"start":-10,"end":0}'        # 与 [0,100) 端点相邻，不重叠
curl -s -X POST localhost:8080/intervals -H 'Content-Type: application/json' \
     -d '{"start":40,"end":60}'
curl -s -X POST localhost:8080/intervals -H 'Content-Type: application/json' \
     -d '{"start":40,"end":60}'        # 重复区间：允许，得到新 id

# 交集查询
curl -s 'localhost:8080/intervals?start=45&end=46'
# {"query":{"start":45,"end":46},"count":4,"intervals":[ ... 嵌套三层+重复 ... ]}

# 相邻语义：[0,10) 与 [-10,0) 不相交（0 在后者是开端）
curl -s 'localhost:8080/intervals?start=0&end=10'      # count=1（只命中 [0,100)）

# 指定时刻覆盖计数
curl -s 'localhost:8080/intervals/count?at=50'         # {"at":50,"count":4}
curl -s 'localhost:8080/intervals/count?at=100'        # {"at":100,"count":1}

# 删除 / 错误处理
curl -s -X DELETE localhost:8080/intervals/2           # {"deleted":true,...}
curl -s -X POST localhost:8080/intervals -H 'Content-Type: application/json' \
     -d '{"start":5,"end":5}'                          # 400 空区间
curl -s -X POST localhost:8080/intervals -H 'Content-Type: application/json' \
     -d '{"start":9,"end":3}'                          # 400 逆序区间
```

可直接运行的脚本：[`examples/requests.sh`](./examples/requests.sh)
（先 `./run.sh 8080`，再 `bash examples/requests.sh`）。

## 5. 设计说明

### 半开整数区间语义

- 区间 `[s,e)` 包含 `s` 而不包含 `e`。
- 两区间相交 ⇔ `a.start < b.end && a.end > b.start`。
- 相邻区间 `[1,5)`、`[5,9)` **不相交**；时刻 5 只被后者覆盖。
- 空区间与逆序区间（`start >= end`）在记录层、索引层、HTTP 层三处统一拒绝。

### 增强平衡树

- BST 键为 `(start, end, id)` 三元组：相同 `start` 用 `end` 排，再用服务端
  唯一 `id` 排，因此允许完全重复的区间。
- 每个节点额外维护：
  - `size`：子树节点总数；
  - `maxEnd`：子树内最大右端点。
- 旋转与插入/删除修复后沿祖先链 `pull` 重算，更新代价 O(log n)。
- 交集查询借助 `maxEnd` 剪枝：子树 `maxEnd <= lo` 时整棵跳过；覆盖计数查询
  利用 BST 起点序 + `maxEnd` 跳过不可能覆盖的子树。
- 删除按 id 经哈希表定位节点（O(1) 平均），再走红黑树 CLRS 删除流程。
- 复杂度：插入/删除 O(log n)；交集查询 O(log n + k)（k 为命中数，含剪枝）；
  覆盖计数最坏 O(n)（全部覆盖的点必然需要统计 n），平均经剪枝显著更少。

### 并发与持久化

- 索引公开方法全部 `synchronized`；HTTP 层使用 8 个守护线程的固定池。
- 数据仅存于内存，重启清空（本任务未要求持久化，见"未完成项"）。

## 6. 目录结构

```
src/main/java/com/example/intervalindex/
  core/Interval.java          # 半开区间 record，构造即校验 start<end
  core/IntervalIndex.java     # 增强红黑树（size + maxEnd），含包内结构自检
  json/Json.java              # 零依赖 JSON 解析/序列化
  http/IntervalServer.java    # JDK HttpServer 路由与参数校验
src/test/java/...             # 自包含测试（Asserts + TestRunner）
build.sh test.sh run.sh       # 构建 / 测试 / 启动脚本
dependencies.lock             # 依赖锁定（JDK 17，无第三方依赖）
examples/requests.sh          # curl 样例
```

## 7. 实际运行结果（2026-09-23 如实记录）

- JDK：Temurin `17.0.20.1+1`，主源码与测试源码 `javac -Xlint:all` **零警告**。
- `./test.sh`：3 个套件全部通过，JVM 退出码 0（输出见
  [`docs/TEST_RESULTS.txt`](./docs/TEST_RESULTS.txt)）。
  - 随机差分测试两轮（种子 20260923 / 6000 操作，种子 42 / 4000 操作），
    每一步与暴力扫描一致；另含 300 个随机区间乱序删空与 2000 个单调插入压力测试。
- 真实启动服务（端口 18091）手工请求验证：嵌套、相邻、重复、交集、
  覆盖计数、删除、400/404 错误路径均符合预期（记录见
  [`docs/TEST_RESULTS.txt`](./docs/TEST_RESULTS.txt)）。

## 8. 未完成项 / 已知限制

- **无持久化**：数据只在内存，进程重启即丢失。
- **无鉴权 / 限流**：服务默认监听所有网卡，适合本地/内网验证，未做 TLS 与认证。
- 覆盖计数为 O(树访问规模)；如需严格 O(log n) 可再维护按端点排序的差分结构，
  当前任务范围内未实现。
- 内存中 id 自增，长期运行未做 id 回收/上限处理（long 空间充足）。
