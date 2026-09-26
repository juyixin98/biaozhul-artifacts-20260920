# version-interval-coverage（版本区间覆盖）

纯后端 Java 服务：对**带优先级的版本规则**在整数轴上做区间叠加计算，输出**不重叠的最终生效片段**，每个片段保留来源版本与规则。无前端、无数据库、无预约/考勤业务逻辑。

## 语义定义

- 轴为 `long`，区间为**半开区间 `[start, end)`**。时间规则把轴当作 epoch 秒使用即可；版本规则可用任意有序整数轴。
- 每个**版本**有一个 `priority`（整数，越大优先级越高）；每条**规则**属于一个版本，声明一个区间。
- 叠加规则：任意点上，覆盖该点的规则中**优先级最高者生效**。
- **相同优先级的区间重叠属于冲突，写入时直接拒绝**（HTTP 409），不做静默裁决。端点相接（如 `[0,6)` 与 `[6,10)`）不算重叠。
- `compute` 输出按起点升序、互不重叠的片段；来自同一规则的相邻片段会被合并；每个片段带 `versionId` / `ruleId` / `label`。
- 删除版本会级联删除其规则，之后 `compute` 重新计算（被覆盖的低优先级片段自动恢复）。
- 状态保存在内存中的不可变 `RuleStore`，每次写入整体换入新对象，不做原地修改。

## 时区数据库版本

服务本身在纯整数轴上计算、不涉及时区换算；但当调用方把瞬时时间映射到轴上时，结果依赖 JVM 内置的 tzdb，因此 `GET /api/meta` 会记录当前时区数据库版本。本机实测为 **`2026b`**（OpenJDK 21.0.12.1）。

## 构建与运行

```bash
mvn test                # 运行自动化测试
mvn package -DskipTests # 打包
# 启动（依赖 jackson，需带依赖 classpath；端口默认 8080）
java -cp "target/version-interval-coverage-1.0.0.jar:\
$HOME/.m2/repository/com/fasterxml/jackson/core/jackson-databind/2.17.2/jackson-databind-2.17.2.jar:\
$HOME/.m2/repository/com/fasterxml/jackson/core/jackson-core/2.17.2/jackson-core-2.17.2.jar:\
$HOME/.m2/repository/com/fasterxml/jackson/core/jackson-annotations/2.17.2/jackson-annotations-2.17.2.jar" \
  com.example.vic.Main 8080
```

## API（JSON 输入输出，统一信封 `{success, data, error}`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | tzdb 版本、Java 版本、轴语义 |
| GET | `/api/state` | 当前全部版本与规则 |
| PUT | `/api/versions` | 注册/更新版本 `{"id":"v1","priority":1}` |
| DELETE | `/api/versions/{id}` | 删除版本（级联删规则） |
| PUT | `/api/rules` | 新增规则 `{"id":"r1","versionId":"v1","start":0,"end":12,"label":"..."}` |
| DELETE | `/api/rules/{id}` | 删除单条规则 |
| POST | `/api/compute` | 计算最终生效片段 |
| POST | `/api/query` | 单点查询 `{"point":5}` |

错误码：400 参数/JSON 非法，404 版本或规则不存在，409 同优先级冲突。

## 请求样例

`examples/requests.sh` 是可执行的完整验收脚本（`./examples/requests.sh http://localhost:8080`）；`examples/*.json` 是实测时的真实响应：

- `01-meta.json` — 元信息（tzdb 2026b）
- `02-compute.json` — 三层嵌套叠加 + 端点相接后的 6 个生效片段
- `03-query-p5.json` / `04-query-p11.json` — 单点查询
- `05-conflict.txt` — 同优先级冲突返回 409
- `06-recompute-after-delete.json` — 删除 v2 后重新计算，被覆盖的 v1 片段恢复

## 自动化测试

`src/test/java/com/example/vic/`：

- `CoverageEngineTest`（12 个用例）：**小整数轴 0..12 逐点参考比对**（每个点用独立暴力扫描参考实现核对 `compute` 结果，并校验片段互不重叠）、端点相接、完全覆盖、三层嵌套、删除版本重算、删除规则留空、同优先级冲突拒绝（同版本与跨版本）、重复规则 id、非法区间、不可变性。
- `ApiServerTest`（6 个用例）：HTTP 端到端的叠加/查询/冲突 409/删除重算/404/400。

## 实测记录（2026-09-25，本机）

| 命令 | 结果 |
|---|---|
| `mvn -q -B test`（首次） | **失败**：默认 maven-compiler-plugin 3.1 不支持 `release`（Source option 5 报错） |
| 在 pom.xml 固定 compiler 插件 3.13.0 后 `mvn -q -B test` | **通过**：CoverageEngineTest 12/12，ApiServerTest 6/6，共 18 个用例 0 失败 |
| `mvn -q -B package -DskipTests` | 通过，产出 `target/version-interval-coverage-1.0.0.jar` |
| `java ... com.example.vic.Main 18080` | **失败**：端口被占用（BindException），改用 18099 成功 |
| `examples/requests.sh`（对 18099 实跑） | 全部符合预期；输出已保存到 `examples/*.json` |

未通过项：无遗留；上述两处失败均已修复/规避后复跑通过。

## 项目结构

```
src/main/java/com/example/vic/
├── Main.java                  # 入口
├── domain/                    # Interval / VersionDef / RuleDef / Segment（不可变 record）
├── store/                     # RuleStore（不可变存储 + 写入校验）、ConflictException、NotFoundException
├── engine/                    # CoverageEngine（叠加计算、单点查询）、MetaInfo（tzdb 版本）
└── http/ApiServer.java        # JDK 内置 HttpServer + Jackson 的 JSON API
```
