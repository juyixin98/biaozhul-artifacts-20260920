# 离线 CAN 信号解码服务（DBC 有限子集）

纯后端服务：上传 DBC 文件 → 解析并校验 → 对标准 11-bit CAN 帧做离线解码。
Python 3.12 + FastAPI + SQLite，**不连接任何车辆或 CAN 硬件**。

支持：

- 标准帧（11-bit ID，`0x000..0x7FF`），明确拒绝 29-bit 扩展帧
- Intel（小端，`@1`）与 Motorola（大端，`@0`）字节序，二者严格区分
- 有符号 / 无符号信号（`@N-` / `@N+`），支持二补码负数
- 缩放与偏移：`physical = raw * factor + offset`（Decimal 精确中间运算）
- 单层多路复用（一个 `M` 开关 + 若干 `m<id>` 分支）
- 输出：原始整数 `raw`、物理值 `physical`、信号定义版本 `signal_version`、
  整库版本 `dbc_version`（均为定义内容的 sha256）

明确拒绝：29-bit ID、扩展/嵌套多路复用（`m0M` 等）、非法位宽（0 或 >64）、
信号超出 DLC、同激活集内的信号重叠、`VAL_`/`BA_DEF_`/信号组等子集外语法。
DLC 不足（或不一致）按帧拒绝并写入审计日志。

## 目录结构

```
app/
  dbc.py         DBC 解析 + 语义校验（位宽、DLC、重叠、复用）
  decoder.py     位提取、二补码、缩放、复用分支选择
  versioning.py  信号级 / DBC 级定义版本（sha256）
  bitmap.py      手工可核对的 ASCII 位图
  storage.py     SQLite：DBC 文档 + 解码审计日志
  schemas.py     Pydantic 模型
  main.py        FastAPI 路由
examples/        示例 DBC 与 6 个示例帧（含负数/跨字节/复用/0x7FF）
tests/           63 个测试（手工位图、cantools 穷举交叉、API 端到端）
requirements.txt / requirements.lock
```

## 本地启动

```bash
cd /path/to/P072/a
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock          # 锁定依赖，可复现
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# 若 8000 被占用，换成任意空闲端口，如 --port 8123，下文同步替换
```

SQLite 文件默认在 `data/can_decoder.db`，可用环境变量
`CAN_DECODER_DB=/tmp/x.db` 覆盖。交互式文档：<http://127.0.0.1:8000/docs>。

## 验收命令（端到端，无需车辆）

```bash
# 1) 自动化测试（含与 cantools 44.1.0 的穷举交叉验证）
.venv/bin/python -m pytest

# 2) 上传示例 DBC
curl -s -X POST http://127.0.0.1:8000/dbc \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"powertrain\",\"content\":$(python3 -c 'import json;print(json.dumps(open("examples/powertrain.dbc").read()))')}"

# 3) 跨字节 + 缩放（0x100：EngineSpeed raw=2600 -> 650.0 rpm）
curl -s -X POST http://127.0.0.1:8000/dbc/powertrain/decode \
  -H 'Content-Type: application/json' \
  -d @examples/decode_engine.json

# 4) 有符号负数温度（raw=-32 -> -72.0 degC）
curl -s -X POST http://127.0.0.1:8000/dbc/powertrain/decode \
  -H 'Content-Type: application/json' \
  -d @examples/decode_engine_negative.json

# 5) Motorola 字节序（0x200：VehicleSpeed 0x1234 -> 46.60 km/h）
curl -s -X POST http://127.0.0.1:8000/dbc/powertrain/decode \
  -H 'Content-Type: application/json' \
  -d @examples/decode_body.json

# 6) 多路复用分支（0x400：ServiceId=0 -> Voltage present，Pressure absent）
curl -s -X POST http://127.0.0.1:8000/dbc/powertrain/decode \
  -H 'Content-Type: application/json' \
  -d @examples/decode_mux_voltage.json
curl -s -X POST http://127.0.0.1:8000/dbc/powertrain/decode \
  -H 'Content-Type: application/json' \
  -d @examples/decode_mux_pressure.json

# 7) 最大标准帧 ID 0x7FF + 32 位有符号 Motorola（=-1）
curl -s -X POST http://127.0.0.1:8000/dbc/powertrain/decode \
  -H 'Content-Type: application/json' \
  -d @examples/decode_maxid.json

# 8) 手工可核对位图
curl -s http://127.0.0.1:8000/dbc/powertrain/bitmap/1024

# 9) 非法情形被拒绝（DLC 不足 -> 422；帧 ID 超 0x7FF -> 422）
curl -s -X POST http://127.0.0.1:8000/dbc/powertrain/decode \
  -H 'Content-Type: application/json' \
  -d '{"frame_id":"0x100","data":"280A"}'
curl -s -X POST http://127.0.0.1:8000/dbc/powertrain/decode \
  -H 'Content-Type: application/json' \
  -d '{"frame_id":"0xFFF","data":"0000000000000000"}'
# 上传非法 DBC（29-bit 扩展 ID）-> 400
curl -s -X POST http://127.0.0.1:8000/dbc -H 'Content-Type: application/json' \
  -d '{"name":"bad","content":"BO_ 2147483904 M: 1 A\n SG_ S : 0|1@1+ (1,0) [0|0] \"\" A\n"}'

# 10) 审计日志
curl -s http://127.0.0.1:8000/log
```

> 提示：在 Claude Code 中可输入 `! <命令>` 直接在本会话执行 shell。

## 位编号与提取规则（可手工核对）

DBC 位号 `n` 位于第 `n//8` 字节的第 `n%8` 位（0 = 该字节 LSB）。

- **Intel**：信号占连续升号位区间；MSB→LSB 提取顺序为
  `start+length-1, start+length-2, …, start`。
- **Motorola**：`start` 即 MSB。从该位起，若当前位在字节左缘
  （`n%8==0`），下一位跳到 `n+15`；否则下一位为 `n-1`，共取 `length` 位。

示例（Motorola `start=7, length=16`）提取顺序：
`7,6,5,4,3,2,1,0, 15,14,13,12,11,10,9,8`。

该规则已对 cantools 44.1.0 做穷举验证：全部 4096 种
`start∈[0,63] × length∈[1,64]` 布局，两种字节序、有/无符号均一致
（2080 种合法，2016 种被双方同时拒绝），并对 12000+ 随机帧逐位比对。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/dbc` | 以 JSON 文本创建/替换 DBC（`{"name","content","replace"}`） |
| POST | `/dbc/upload` | multipart 上传 `.dbc` 文件 |
| GET | `/dbc` | 列出 DBC（含版本与消息数） |
| GET | `/dbc/{name}` | DBC 详情与消息清单 |
| DELETE | `/dbc/{name}` | 删除 DBC |
| GET | `/dbc/{name}/bitmap/{frame_id}` | 手工可核对 ASCII 位图 |
| POST | `/dbc/{name}/decode` | 解码一帧 |
| GET | `/log?limit=50` | 最近解码尝试（含失败原因） |

`decode` 请求体：`{"frame_id": "0x100"(十进制或0x十六进制), "data": "28 0A ..."(hex)}`。

## 测试策略

- `tests/test_bitmaps.py`：纸张级位轨迹与手工数值（跨字节、负数）。
- `tests/test_dbc_parser.py`：合法子集与全部拒绝路径。
- `tests/test_decoder.py`：缩放、负数、复用分支、DLC、最大帧 ID、版本变化。
- `tests/test_cross_cantools.py`：与成熟库 cantools 的**穷举**布局一致性、
  300 帧随机缩放比对、复用消息与 0x7FF 帧比对。
- `tests/test_api.py`：HTTP 端到端（成功与各类 4xx、审计日志）。

## 设计边界

- 不做 CAN FD（DLC>8）、不做浮点信号（`f`/`g` 类型）、枚举值表、
  信号组、属性、扩展多路复用、29-bit ID——遇到即明确报错。
- 解码严格按 DBC 声明的 DLC 校验字节数；可在请求中传
  `strict_dlc:false` 允许更长，但更短始终拒绝。
- 未被当前复用值选中的分支信号返回 `present:false`，不读取其字节
  （那些字节可能属于其它分支）。
- 复用开关值没有任何已定义分支时，按错误拒绝（与 cantools 一致）。
