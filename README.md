# CAN 信号解码服务（DBC 有限子集 · 纯后端）

一个**完全离线**的 CAN 报文解码服务：上传一份 DBC，POST 原始 CAN 帧，返回每个信号的
**原始整数（raw）**、**物理值（value）**以及**信号定义版本（definition_version）**。

技术栈：Python 3.12 · FastAPI · SQLite（标准库 `sqlite3`）· 无前端 · **不连接车辆 / CAN 硬件**。

---

## 1. 支持的 DBC 子集（白名单，越界即拒绝）

| 能力 | 支持情况 |
|---|---|
| 标准帧（11-bit ID，`0x000..0x7FF`，含最大 ID 0x7FF） | ✅ |
| Intel / little-endian（`@1`）与 Motorola / big-endian（`@0`） | ✅ 二者严格区分，**绝不混用** |
| 有符号信号（二进制补码，`@0-` / `@1-`） | ✅ |
| 缩放与偏移 `value = raw * factor + offset`（Decimal 精确计算） | ✅ |
| 单层多路复用（switch `M`/`M0`，分支 `mN`，N≥1） | ✅ |
| `VERSION / NS_ / BS_ / BU_ / BO_ / SG_ / CM_（可跨行注释）` | ✅ |

**明确拒绝**（返回 `422` 与稳定错误码，绝不含糊忽略）：

- 扩展帧 ID（`BO_` id ≥ `0x80000000`，或超过 `0x7FF`）、DLC > 8
- 位宽非法（`< 1` 或 `> 64`）、信号起点/宽度超出报文 DLC（**DLC 不足**）
- 信号位重叠（同一复用分支内、plain 与任意信号、switch 与任意信号；
  不同 `mN` 分支之间允许重叠）
- 扩展（多级）复用标记，如 `m1m0`、`m0M`；分支无 switch；多个 switch；
  分支值超出 switch 位宽
- 浮点信号（`SIG_VALTYPE_`）、值表（`VAL_TABLE_`）、属性（`BA_*`）等本子集之外的任何语句
- 语法损坏的 `BO_` / `SG_`、重复帧 ID / 信号名、缺分号注释等

### 字节序与 DBC 位编号（不能忽略的差异）

DBC 采用“锯齿”位编号：字节 `b` 内位号为 `8b + i`，`i=0` 是该字节 **LSB**。

- **Intel（`@1`）**：起始位 = 信号 LSB，位按 S, S+1, S+2 … 递增。
- **Motorola（`@0`）**：起始位 = 信号 MSB，字节内向低位走到 0，随后 **+15 跳转**
  到下一字节的 MSB（位 7）。

例如同样占用前两个字节，`[0x12, 0x34]`：

- `7|16@0`（Motorola）→ `0x1234`
- `0|16@1`（Intel）  → `0x3412`

手工可核对位图见 `tests/test_bitops.py`（每个跨字节用例都画了位图）与
`app/bitops.py` 顶部模块说明。

---

## 2. 输出内容

每个解码信号同时给出：

- `raw`：线上的**无符号原始整数**（即使是 signed 信号也给出补码位模式，便于核对）
- `value`：套用 `factor/offset` 后的**有符号物理值**（float）
- `length / byte_order / signed / unit / mux_kind / mux_value`：定义回显
- `definition_version`：该信号所用 DBC 的 **SHA-256 语义指纹**（64 hex）

帧级还返回 `mux`（当前复用值）、`data_hex`、`dlc`。

### 版本与完整性（真实密码学操作）

- `definition_version = SHA256( canonical-json( 解析后的DBC ) )`：只改注释/空白
  不改变版本，改任何 factor/位定义都会改变版本。
- 每份定义另返回 `signature_sha256 = HMAC-SHA256(version_id, key)`，
  可 `POST /verify` 校验。密钥来自环境变量 `CANDECODE_HMAC_KEY`（≥16 字节）；
  未设置时生成 32 字节随机密钥**持久化在 SQLite `meta` 表**，重启后签名保持一致。
- 解码时若库内 DBC 内容与其指纹不符（被篡改），拒绝解码（`fingerprint_mismatch`）。

---

## 3. 本地启动

```bash
cd P072/b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # 或严格复现：pip install -r requirements-lock.txt

# 可选：指定 HMAC 密钥与数据库位置
# export CANDECODE_HMAC_KEY="change-me-to-a-long-random-secret"
# export CANDECODE_DB="$PWD/candecode.db"

uvicorn app.main:app --host 127.0.0.1 --port 8000
# 等价：python -m app
```

打开交互文档：<http://127.0.0.1:8000/docs>（Swagger UI，纯后端自带）。
健康检查：`GET /health`。

---

## 4. 验收命令（另开一个终端）

```bash
source .venv/bin/activate

# (1) 自动化测试：94 个用例，含手工位图单测 + cantools 随机交叉验证
pytest -q

# (2) 端到端示例：上传示例 DBC，逐条解码 examples/payloads.jsonl 并断言结果
python examples/run_examples.py
```

### 手工 curl 走查

```bash
# 上传 DBC，拿到 version_id / signature
curl -s -X POST 127.0.0.1:8000/dbc -H 'Content-Type: application/json' \
  -d "{\"dbc\": $(python -c 'import json;print(json.dumps(open("examples/demo.dbc").read()))')}" | python -m json.tool

# 解码一帧（跨字节 Intel + 有符号 + 缩放）
curl -s -X POST 127.0.0.1:8000/decode -H 'Content-Type: application/json' \
  -d '{"frame_id":256,"data":"34 12 D8 64 00 00 00 00"}' | python -m json.tool

# 复用分支 mux=2（含 Motorola 跨字节信号 LineVoltage）
curl -s -X POST 127.0.0.1:8000/decode -H 'Content-Type: application/json' \
  -d '{"frame_id":300,"data":"02 00 00 00 06 46 E0 00"}' | python -m json.tool

# 最大标准帧 ID 0x7FF，含 Motorola 16 位与负物理值
curl -s -X POST 127.0.0.1:8000/decode -H 'Content-Type: application/json' \
  -d '{"frame_id":2047,"data":"12 34 FC 18 60 09 00 00"}' | python -m json.tool

# DLC 不足 -> 422 dlc_too_short
curl -s -X POST 127.0.0.1:8000/decode -H 'Content-Type: application/json' \
  -d '{"frame_id":300,"data":"01 00 40 00 00 00 00"}' | python -m json.tool

# 历史记录
curl -s "127.0.0.1:8000/frames?limit=10" | python -m json.tool
```

---

## 5. HTTP 接口

| 方法 路径 | 说明 |
|---|---|
| `GET  /health` | 存活检查 |
| `POST /dbc` | 上传/校验 DBC 文本；幂等（相同语义返回相同 version_id，`created=false`） |
| `GET  /definitions/{version_id}` | 查询定义元数据 |
| `POST /decode` | 解码一帧；`data` 支持 hex 字符串（`"34 12 ..."` / `"0x..."`）或整数数组；可带 `version_id` 钉住版本、`dlc`、`persist` |
| `POST /verify` | 校验某 version_id 的 HMAC-SHA256 签名 |
| `GET  /frames?version_id=&frame_id=&limit=` | 已持久化的解码历史（SQLite） |

错误统一为 `{"error":{"code":...,"message":...}}`，例如
`dbc_syntax_error` / `dbc_layout_error` / `dlc_too_short` / `unknown_message` /
`bad_hex` / `fingerprint_mismatch`。

---

## 6. 示例输入（手工核对答案）

`examples/demo.dbc` 定义 3 个报文；`examples/payloads.jsonl` 每行一帧，
`_expect`/`_expect_error`/`_expect_http_status` 为内置断言（由 `run_examples.py` 消费）：

| 帧 | data | 关键结果 |
|---|---|---|
| `0x100` EngineStatus | `34 12 D8 64 00 00 00 00` | EngineSpeed raw=4660→**1165.0 rpm**；EngineTemp raw=216（补码 −40）→**−80 °C**；Throttle raw=100→**50.0 %** |
| `0x12C` BrakeMux mux=1 | `01 00 40 06 00 00 00 00` | 仅分支1：BrakePress raw=1600→**160.0 kPa**，PedalPos=0 |
| `0x12C` BrakeMux mux=2 | `02 00 00 00 06 46 E0 00` | 仅分支2：LineVoltage（Motorola `47\|12@0`）raw=1134→**11.34 V**，ErrorCode=6 |
| `0x7FF` MaxIdMotorola | `12 34 FC 18 60 09 00 00` | BigCounter（Moto）=**4660**；BigSigned raw=64536→**−1000**；CrossIntel raw=2400→**190.0 deg** |
| `0x12C`（7 字节） | `01 00 40 00 00 00 00` | **422 dlc_too_short** |

---

## 7. 测试与交叉验证策略

- `tests/test_bitops.py`：Intel/Motorola 位走查（含 `+15` 跳转）、手工位图跨字节、
  全 8 字节、有符号边界（−1/−40/−128/−1000/1-bit signed）、DLC 越界。
- `tests/test_dbc_parser.py`：白名单接受 + 全部拒绝路径（扩展帧、非法位宽、
  重叠、扩展复用、DLC 等 30 例）。
- `tests/test_decoder.py`：raw→signed→物理值、复用分支选择、DLC 强制。
- `tests/test_api.py`：端到端 HTTP + SQLite 持久化、版本幂等、篡改指纹、
  HMAC 验签与密钥跨“重启”、错误码映射。
- `tests/test_cantools_crosscheck.py`：用成熟库 **cantools 44.1.0** 交叉比对——
  对 Intel/Motorola × signed/unsigned 各随机生成数百个合法布局、用 cantools
  `encode(..., scaling=False)` 造帧，逐位要求本服务的 raw 与物理值一致；
  并要求 cantools strict 模式能解析 `examples/demo.dbc` 且各示例帧解码一致。

> cantools 仅为测试依赖，应用运行时**不导入**它。

---

## 8. 目录结构

```
app/
  bitops.py    DBC 锯齿位编号的位提取 / 符号扩展 / 位图渲染（无第三方库，可审计）
  dbc.py       DBC 子集词法解析 + 布局/重叠/复用校验（白名单拒绝）
  decoder.py   raw、二进制补码、Decimal 缩放、复用分支选择、DLC 强制
  crypto.py    SHA-256 语义指纹、HMAC-SHA256 签名/校验
  storage.py   SQLite：definitions / frames / decoded_signals / meta
  main.py      FastAPI 路由、Pydantic 模型、错误映射、DBC 缓存
examples/      demo.dbc、payloads.jsonl、run_examples.py
tests/         上述五组测试
requirements.txt / requirements-lock.txt
```

## 9. 安全与范围说明

- 纯计算服务，无任何车辆/套接字/CAN 驱动连接；只处理用户显式提交的 DBC 与帧。
- 所有计算（位运算、缩放、SHA-256、HMAC）均真实执行，无占位实现。
- 该 DBC 子集刻意很小：遇到子集外语法是**报错**而非“尽力解析”，以免给出看似正确、
  实则错误的解码结果。
