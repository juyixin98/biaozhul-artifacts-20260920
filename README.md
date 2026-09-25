# time-precision-converter

纯后端的时间精度转换服务（Java 21，零第三方运行时依赖）。在秒 / 毫秒 / 微秒 / 纳秒
四种单位之间转换**带单位的整数时间戳**，支持负 epoch（1970 之前的时刻），提供
JSON 输入输出。本项目只做时间/精度规则计算，不包含预约、考勤等业务功能。

## 设计约束（验收语义）

| 约束 | 实现 |
| --- | --- |
| 解析不经过浮点 | 全程 `BigInteger` / `BigDecimal` 精确十进制运算；JSON 数字保留原始字面量；小数秒文本按位解析 |
| 溢出不可静默截断 | 结果超出有符号 64 位范围时返回 `OVERFLOW` 错误（HTTP 422），绝不回绕 |
| 舍入模式明确 | 8 种 `java.math.RoundingMode` 语义；缺省 `UNNECESSARY`——有损转换未显式指定舍入时直接报错 |
| 非法精度 | 小数秒文本超过 9 位（纳秒以下）返回 `INVALID_PRECISION` |
| 往返无损 | 仅在目标单位可精确表示时保证：`parse(format(v)) == v`（有测试覆盖，含 `Long.MIN_VALUE`） |
| 时区数据库版本 | 每个响应的 `meta.tzdbVersion` 中记录（当前运行时实测为 **2026b**） |

## 单位与舍入

- 单位：`SECONDS`、`MILLISECONDS`、`MICROSECONDS`、`NANOSECONDS`（大小写不敏感，
  支持 `s`/`ms`/`us`/`ns` 等别名）。
- 舍入模式：`FLOOR`（向负无穷）、`CEILING`（向正无穷）、`DOWN`（向零截断）、
  `UP`（远离零）、`HALF_UP`、`HALF_DOWN`、`HALF_EVEN`、`UNNECESSARY`（默认，不精确即报错）。
- 负数取整示例：`-1500 ms → s` 时，`FLOOR = -2`，`DOWN = -1`，`HALF_UP = -2`，
  `HALF_DOWN = -1`，`HALF_EVEN = -2`。

## 构建、测试、运行

```bash
mvn test                                   # 运行全部自动化测试
mvn package                                # 产出可执行 jar
java -jar target/time-precision-converter-1.0.0.jar [端口]   # 默认 8080，也可用环境变量 PORT
./samples/requests.sh 8080                 # 依次发送全部请求样例
```

## HTTP 接口

所有响应都带 `meta`（`tzdbVersion`、`zoneCount`、`javaVersion`）。时间戳结果以
**字符串**返回，保证完整 64 位精度不被 JSON 数字精度（2^53）截断。

### POST /convert — 整数时间戳单位换算

```json
{"value":"-1500","fromUnit":"MILLISECONDS","toUnit":"SECONDS","rounding":"FLOOR"}
```

- `value`：整数字符串或整数 JSON 字面量（拒绝小数/科学计数法字面量）
- `rounding`：可选，默认 `UNNECESSARY`

成功：`{"ok":true,"result":"-2","resultUnit":"SECONDS","meta":{...}}`

### POST /parse — 小数秒文本 → 整数时间戳

```json
{"text":"123.456789012","toUnit":"NANOSECONDS"}
```

- `text`：`[+-]?整数[.小数]`，小数最多 9 位（纳秒），超过报 `INVALID_PRECISION`

成功：`{"ok":true,"result":"123456789012","resultUnit":"NANOSECONDS","meta":{...}}`

### POST /format — 整数时间戳 → 小数秒文本（/parse 的逆运算）

```json
{"value":"-1500","unit":"MILLISECONDS"}
```

成功：`{"ok":true,"text":"-1.5","meta":{...}}`

### GET /meta — 运行时元数据

```json
{"ok":true,"meta":{"tzdbVersion":"2026b","zoneCount":604,"javaVersion":"21.0.12.1"}}
```

### 错误响应

```json
{"ok":false,"error":{"code":"OVERFLOW","message":"result 9223372036854775807000000000 does not fit in a signed 64-bit integer"},"meta":{...}}
```

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `BAD_REQUEST` | 400 | JSON 格式错误、缺字段、字段类型错误 |
| `INVALID_UNIT` | 422 | 未知时间单位 |
| `INVALID_VALUE` | 422 | 非法整数/文本字面量 |
| `INVALID_PRECISION` | 422 | 小数位超过纳秒（>9 位） |
| `ROUNDING_NECESSARY` | 422 | 默认 `UNNECESSARY` 下转换不精确 |
| `OVERFLOW` | 422 | 结果超出有符号 64 位范围 |
| `METHOD_NOT_ALLOWED` | 405 | HTTP 方法错误 |
| `PAYLOAD_TOO_LARGE` | 413 | 请求体超过 64 KiB |

## 请求样例

`samples/` 目录下是固定的本地测试数据，`samples/requests.sh` 会依次发送：

| 文件 | 说明 |
| --- | --- |
| `convert-floor.request.json` | 负数取整：`-1500 ms → s, FLOOR` → `-2` |
| `convert-overflow.request.json` | `Long.MAX_VALUE s → ns` → `OVERFLOW` |
| `parse-fractional.request.json` | `123.456789012 s → ns` → `123456789012` |
| `parse-invalid-precision.request.json` | 10 位小数 → `INVALID_PRECISION` |
| `format.request.json` | `-1500 ms` → `"-1.5"` |

## 实测记录（2026-09-25，本机实际执行）

环境：OpenJDK 21.0.12.1，Maven 3.8.7，Linux 6.8.0-90-generic，tzdb **2026b**。

- `mvn test` → **Tests run: 54, Failures: 0, Errors: 0, Skipped: 0 — BUILD SUCCESS**
  （5 个测试类：TimeConverterTest 20、ServerIntegrationTest 16、
  DecimalSecondsParserTest 7、JsonParserTest 6、DecimalSecondsFormatterTest 5）
- `mvn package` → `target/time-precision-converter-1.0.0.jar`
- `java -jar target/time-precision-converter-1.0.0.jar 18081` 启动后执行
  `./samples/requests.sh 18081`，6 个样例请求全部返回预期结果（输出见上表；
  溢出/非法精度均返回 422 + 对应错误码，无静默截断）。
- 未通过项：无。（开发过程中曾有 3 处测试期望值笔误被测试捕获并修正，
  实现代码逻辑未发现问题。）

## 测试覆盖的验收点

- **负数取整**：`TimeConverterTest` 覆盖 8 种舍入模式在 ±1500ms、±2500ms 等
  边界值下的行为（含 `HALF_EVEN` 的偶数取舍）。
- **极限整数**：`Long.MAX_VALUE` / `Long.MIN_VALUE` 的恒等转换、上溢
  （s→ns、ms→µs）、可表示的最大秒数（`Long.MAX_VALUE/1e9`）成功转换。
- **带小数秒文本**：`DecimalSecondsParserTest` 覆盖补零、负小数（`-0.5`）、
  直接解析到粗粒度单位并舍入。
- **非法精度**：10 位小数在单元测试与 HTTP 集成测试中均被拒绝。
- **往返无损**：`DecimalSecondsFormatterTest` 对含 `Long.MIN_VALUE` 在内的一组
  纳秒值验证 `parse(format(v)) == v`；`ServerIntegrationTest` 通过
  `/parse` + `/format` 完成 HTTP 层往返。
- **溢出**：单元与集成测试均验证返回 `OVERFLOW` 而非截断。

## 项目结构

```
src/main/java/dev/timeprecision/
  TimeUnit.java                单位枚举（纳秒倍数、别名解析）
  TimeConverter.java           核心换算（BigInteger/BigDecimal，溢出检查）
  DecimalSecondsParser.java    小数秒文本 → 纳秒（纯十进制，无浮点）
  DecimalSecondsFormatter.java 纳秒 → 小数秒文本（parse 的逆运算）
  ConversionException.java     领域异常（携带 ErrorCode）
  ErrorCode.java               稳定错误码
  Meta.java                    tzdb 版本等运行时元数据
  Main.java                    入口（端口参数）
  json/                        极简 JSON 模型/解析器/序列化器（数字保留原始字面量）
  http/                        基于 JDK 内置 HttpServer 的接口层
src/test/java/...              54 个自动化测试（JUnit 5）
samples/                       固定请求样例 + requests.sh
```
