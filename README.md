# time-precision-converter

时间精度转换纯后端服务（Java 21，零第三方依赖）。在秒 / 毫秒 / 微秒 / 纳秒之间转换
**带单位的整数时间戳**，支持负 epoch（1970 年之前），舍入模式显式指定，溢出报错而
非静默截断，全程不经过浮点运算。

- 时区数据库版本：**2026b**（运行时由 `GET /meta` 如实上报；本服务不做时区换算，
  仅记录版本。本服务不是预约或考勤系统。）
- 输入输出：JSON over HTTP（JDK 内置 `com.sun.net.httpserver`）。
- 整数范围：结果必须在有符号 64 位整数（int64）范围内，超出返回 `OVERFLOW` 错误。
- 测试数据：`data/cases/*.json` 为本地固定测试数据，由测试套件逐条执行。

## 构建与运行

```bash
./build.sh          # 编译到 build/（仅需 javac，无外部依赖）
./test.sh           # 编译并运行全部自动化测试
./run-server.sh     # 启动服务，默认端口 8080（可传参改端口）
```

## API

### POST /convert — 整数时间戳单位换算

请求（`value` 用字符串承载，避免客户端 JSON 浮点精度问题；`rounding` 可省略，
省略等价于 `UNNECESSARY`，即不精确时报错）：

```json
{"value": "-1500", "fromUnit": "MILLISECOND", "toUnit": "SECOND", "rounding": "FLOOR"}
```

响应：

```json
{"ok": true, "result": "-2", "unit": "SECOND", "exact": false}
```

`result` 以字符串返回，保证 int64 全范围不丢精度；`exact` 表示本次换算是否无损。

### POST /parse — 文本解析为整数时间戳

十进制小数文本（含负小数秒，不支持科学计数法）：

```json
{"text": "-1.2345", "format": "DECIMAL", "unit": "SECOND", "toUnit": "MILLISECOND", "rounding": "FLOOR"}
→ {"ok": true, "result": "-1235", "unit": "MILLISECOND", "exact": false}
```

ISO-8601 瞬间（支持负 epoch、任意位数小数秒、时区偏移）：

```json
{"text": "1969-12-31T23:59:59.5Z", "format": "ISO_INSTANT", "toUnit": "MILLISECOND"}
→ {"ok": true, "result": "-500", "unit": "MILLISECOND", "exact": true}
```

小数位数超过目标单位精度时按 `rounding` 舍入；未给舍入模式则返回 `INEXACT`。

### GET /meta — 能力与版本

```json
{"ok": true, "service": "time-precision-converter", "tzdbVersion": "2026b",
 "units": ["SECOND", "MILLISECOND", "MICROSECOND", "NANOSECOND"],
 "roundingModes": ["UP", "DOWN", "CEILING", "FLOOR", "HALF_UP", "HALF_EVEN", "UNNECESSARY"],
 "integerRange": "int64 (results outside this range return OVERFLOW)"}
```

## 单位与舍入

- 单位：`SECOND`、`MILLISECOND`、`MICROSECOND`、`NANOSECOND`（另接受 `s/ms/us/ns` 等别名）。
  皮秒等更细精度**不支持**，请求返回 `INVALID_UNIT`。
- 舍入模式（语义对负数同样严格成立）：
  | 模式 | 含义 |
  |---|---|
  | `UP` | 远离零 |
  | `DOWN` | 趋向零（截断） |
  | `CEILING` | 趋向 +∞ |
  | `FLOOR` | 趋向 −∞ |
  | `HALF_UP` | 最近值，平局远离零 |
  | `HALF_EVEN` | 最近值，平局取偶 |
  | `UNNECESSARY` | 要求精确，否则报 `INEXACT`（缺省行为） |

## 错误码

统一错误信封：`{"ok": false, "error": {"code": "...", "message": "..."}}`，HTTP 400。

`BAD_REQUEST` · `INVALID_UNIT` · `INVALID_ROUNDING` · `INVALID_VALUE` ·
`INVALID_TEXT` · `INEXACT` · `OVERFLOW` · `METHOD_NOT_ALLOWED` · `INTERNAL`

## 设计要点

- **不经过浮点**：JSON 数字按原始文本解析（自研零依赖 JSON 解析器），所有换算用
  `BigInteger` 完成；ISO 文本的整秒部分用 `java.time`（整数运算）求 epoch 秒。
- **溢出不静默截断**：结果超出 int64 时返回 `OVERFLOW`（如 `Long.MAX_VALUE` 秒转纳秒）。
- **往返无损仅在可表示条件下成立**：例如 秒→纳秒→秒 在 int64 范围内无损；
  纳秒→微秒→纳秒 会丢失亚微秒部分（测试中有明确记录）。
- 负数取整按模式语义严格执行，例如 `-1500 ms → s`：`FLOOR=-2`、`CEILING=-1`、
  `DOWN=-1`、`UP=-2`、`HALF_UP=-2`、`HALF_EVEN=-2`。

## 请求样例

见 `samples/convert-request.json`、`samples/parse-request.json`。实测（2026-09-25）：

```bash
curl -s -X POST --data @samples/convert-request.json http://127.0.0.1:8080/convert
# {"ok":true,"result":"-2","unit":"SECOND","exact":false}

curl -s -X POST --data @samples/parse-request.json http://127.0.0.1:8080/parse
# {"ok":true,"result":"-500","unit":"MILLISECOND","exact":true}

curl -s -X POST -d '{"value":"9223372036854775807","fromUnit":"SECOND","toUnit":"NANOSECOND"}' \
  http://127.0.0.1:8080/convert
# HTTP 400 {"ok":false,"error":{"code":"OVERFLOW","message":"result 9223372036854775807000000000 does not fit in a signed 64-bit integer"}}
```

## 测试

`./test.sh` 运行全部测试：JSON 解析、核心换算（负数取整 / 极限整数 / 溢出）、
文本解析（小数秒 / ISO-8601 / 非法精度）、`data/cases/` 固定数据用例、真实 HTTP 集成测试。

最近一次实际运行（2026-09-25，OpenJDK 21.0.12.1，tzdb 2026b）：

```
passed: 90, failed: 0
```

未通过项：无。

## 目录结构

```
src/main/java/com/timeconv/     核心：Unit / Rounding / TimeConverter / TextParser / ConvertService
src/main/java/com/timeconv/json/      零依赖 JSON（数字保原文，不经浮点）
src/main/java/com/timeconv/http/      JDK 内置 HTTP 服务
src/test/java/com/timeconv/     测试（自研极简 harness，TestMain 入口）
data/cases/                     本地固定测试数据（convert / parse 用例）
samples/                        请求样例
build.sh test.sh run-server.sh  构建 / 测试 / 启动脚本
```
