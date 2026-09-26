# version-interval-coverage(版本区间覆盖)

纯后端 Java 服务:按**带优先级的版本**对区间规则进行叠加与查询,输出**不重叠的最终生效片段**,每个片段保留其**来源版本**。无前端、无预约/考勤业务逻辑。

- 轴类型:`long` 整数轴。表示时间时直接使用 **epoch 秒(UTC)**。
- 区间语义:**半开区间 `[start, end)`**,因此端点相接(`[0,5)` 与 `[5,10)`)不算重叠。
- 叠加规则:同一位置由**优先级(priority)最高**的版本的规则生效。
- 冲突规则:**相同优先级**的区间发生重叠时,整个写入被拒绝(HTTP 409),不做静默合并;同一版本内部区间自重叠同样拒绝(HTTP 400)。
- 删除某版本后,覆盖结果基于当前版本集合**重新计算**,被遮蔽的低优先级规则自动恢复。
- 时区数据库版本通过 `GET /api/meta` 记录与暴露(见下文实测为 `2026b`)。

## 构建与运行

环境要求:JDK 21+(开发实测 OpenJDK 21.0.12.1)、Maven 3。

```bash
# 运行自动化测试
mvn test

# 打包
mvn package -DskipTests
mvn dependency:build-classpath -Dmdep.outputFile=target/cp.txt

# 启动服务(默认端口 8080,可用 PORT 环境变量覆盖)
PORT=8080 java -cp "target/version-interval-coverage-1.0.0.jar:$(cat target/cp.txt)" com.example.vercov.Main
```

## HTTP API(JSON 输入输出)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/meta` | 服务信息、区间语义、**tzdb 版本**、Java 版本 |
| GET | `/api/versions` | 列出全部版本 |
| POST | `/api/versions` | 注册版本,体:`{"id","priority","intervals":[{"start","end","label"?}]}` |
| DELETE | `/api/versions/{id}` | 删除版本(覆盖结果随之重算);不存在返回 404 |
| GET | `/api/coverage?from=&to=` | `[from,to)` 内不重叠的最终生效片段(含 `versionId` 来源) |
| GET | `/api/point?at=` | 单点生效片段;无覆盖返回 `null` |

错误格式:`{"error": <code>, "message": <detail>}`;状态码:400 参数非法 / 409 同优先级冲突或版本 id 重复 / 404 版本不存在。

## 请求样例

`samples/` 目录内含固定测试数据与脚本:

- `01-create-base.json` — 优先级 1,`[0,10)`
- `02-create-patch.json` — 优先级 2,`[3,6)`(叠加后切出 3 段)
- `03-create-conflict.json` — 优先级 2,`[5,8)`(与 patch-v2 同优先级重叠 → 409)
- `04-create-time-axis.json` — 时间轴示例,2026-01-01 UTC 日的 epoch 秒区间
- `run_samples.sh` — 依次执行上述场景:`BASE_URL=http://localhost:8080 bash samples/run_samples.sh`
- `sample-run-output.txt` — 本次实测的完整输出(见下)

## 自动化测试与验收

`mvn test` 共 **20 个测试,全部通过**(实测,无未通过项):

- `CoverageEngineTest`(8)— 端点相接不冲突、同优先级重叠拒绝、完全覆盖、部分叠加切三段、查询范围裁剪与空洞、相邻同版本合并、空集、非法区间。
- `ReferenceModelTest`(2)— **小整数轴逐点参考模型**:固定种子随机生成 200 组版本(轴 0..24),对每个整数点用暴力扫描选出最高优先级规则,与引擎输出逐点比对;另含端点相接/完全覆盖/三层嵌套/孤岛 4 个手工场景。
- `VersionStoreTest`(6)— **删除版本后重新计算**并恢复被遮蔽规则、冲突写入的原子性、id 重复拒绝、点查询、自重叠与非法区间校验。
- `HttpApiTest`(4)— HTTP 全生命周期(meta/创建/覆盖/点查/409/删除后重算/404)、非法输入 400、epoch 秒时间轴与排他端点。

## 实测记录(如实)

| 命令 | 结果 |
|------|------|
| `mvn test`(首次) | 失败:默认 maven-compiler-plugin 3.1 不支持 `release` 配置 → 在 pom.xml 固定 3.13.0 后通过 |
| `mvn test`(第二次) | 编译错误:`HttpApi.point` 中 `Optional.map` 方法引用类型不匹配 → 改为显式 `(JsonNode)`  lambda 后通过 |
| `mvn test`(最终) | **Tests run: 20, Failures: 0, Errors: 0, Skipped: 0 — BUILD SUCCESS** |
| `java ... Main`(PORT 默认 8080) | 失败:`BindException: Address already in use`,本机 8080 被另一 Java 进程(pid 18689)占用 → 改用 `PORT=18080` 启动成功 |
| `bash samples/run_samples.sh` | 首次录制输出文件前半部分丢失:脚本中 `curl -o /dev/stderr` 会以 O_TRUNC 打开重定向目标文件并截断 → 改为 `-w '\nHTTP %{http_code}\n'` 输出到 stdout 后修复 |
| `bash samples/run_samples.sh`(最终,PORT=18082) | 全部 11 步符合预期,完整输出见 `samples/sample-run-output.txt` |

实测环境关键事实(来自 `GET /api/meta`):

```json
{
  "tzdbVersion" : "2026b",
  "availableZoneCount" : 604,
  "javaVersion" : "21.0.12.1"
}
```

关键实测结果(完整报文见 `samples/sample-run-output.txt`):

1. base-v1 `[0,10)` + patch-v2 `[3,6)` → 覆盖输出 3 段不重叠片段:`[0,3)@base-v1`、`[3,6)@patch-v2`、`[6,10)@base-v1`,均含来源 `versionId`。
2. 同优先级冲突写入 clash-v3 → **HTTP 409**,错误信息指明冲突双方。
3. `DELETE /api/versions/patch-v2` 后重算 → 单段 `[0,10)@base-v1`。
4. 时间轴:点 `1767225600`(2026-01-01T00:00:00Z)命中 `utc-day-2026-01-01`;排他端点 `1767312000` 返回 `null`。

未通过项:无(最终运行全部通过;过程中两次编译/启动失败已修复并记录于上表)。

## 项目结构

```
src/main/java/com/example/vercov/
├── Main.java                  # 入口,读取 PORT 启动 HTTP 服务
├── api/HttpApi.java           # JSON/HTTP 层(JDK 内置 HttpServer + Jackson)
├── engine/CoverageEngine.java # 区间叠加核心:边界切分 + 最高优先级选取 + 相邻合并
├── engine/ConflictException.java
├── model/IntervalRule.java    # 半开区间 [start,end)
├── model/Version.java         # 版本:id + 优先级 + 区间集(校验自重叠)
├── model/EffectiveSegment.java# 输出片段:保留来源 versionId/priority/label
├── meta/TzdbInfo.java         # JVM 内置 IANA tzdb 版本
└── store/VersionStore.java    # 线程安全内存仓库,查询时实时重算
src/test/java/...              # 20 个 JUnit 5 测试
samples/                       # 固定测试数据、运行脚本、实测输出
```
