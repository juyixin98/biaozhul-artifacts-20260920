# ATMS 教学后端

基于假设的真值维护系统（ATMS, de Kleer 风格）教学实现：FastAPI 查询接口 + SQLite 证据存储 + 独立推理内核。

## 模块划分

| 模块 | 职责 |
|------|------|
| `app/core/` | 推理内核：环境（假设集合）、标签（子集极小、无 nogood 超集）、传播队列、预算控制、撤销重建 |
| `app/rules/` | 规则语言：Horn 规则 `A, B => C` 的 schema 与校验；`⊥` 为矛盾节点 |
| `app/store/` | 证据存储：SQLite 中的会话、操作日志（可重放重建）、诊断记录 |
| `app/api/` | 查询接口：FastAPI 路由、请求标识、诊断写入 |
| `app/config.py` | 预算与服务配置（`ATMS_*` 环境变量可覆盖） |
| `tests/` | 单元测试、集成测试、独立暴力枚举 oracle（`tests/oracle.py`，与内核零共享代码） |

## 行为契约

1. **子集极小标签**：标签中不保留任何环境的超集；矛盾环境（nogood）及其超集永远不会成为有效支持。
2. **撤销不误删**：撤销假设后通过重放（前提 + 现存假设 + 规则）重建标签，仍被其他环境支持的结论保留。
3. **传播终止与预算**：`max_envs_per_label` / `max_propagation_steps` / `max_combinations_per_rule` 三项硬预算，传播为有界工作队列循环，必然终止。
4. **超限只报不完整**：预算超限时置 `incomplete` 并记录原因；查询状态区分 `supported` / `supported-partial` / `unsupported` / `unknown`——空标签在不完整时报告 `unknown`，绝不把未生成的环境当作不存在。

## 快速开始

```bash
pip install -r requirements.txt

# 运行全部测试（单元 + 集成 + oracle 对照）
python -m pytest -v
# 实测结论（2026-09-27，Python 3.12.3 / pytest 9.1.1）：37 passed in 0.90s

# 启动服务（SQLite 文件默认 ./atms.db，可用 ATMS_DB_PATH 覆盖）
uvicorn app.main:app --port 8000
```

## 样例会话

`examples/sample_session.json` 含共享前提 `P`、互斥假设 `A`/`B`、结论 `C` 的两条独立证明。手算期望：`label(C) = [[A],[B]]`，`nogoods = [[A,B]]`。

```bash
python -m scripts.load_sample     # 打印 label(C) 与 nogoods 并核对手算期望
# 实测输出：
#   label(C) = [['A'], ['B']]
#   nogoods  = [['A', 'B']]
#   matches hand-computed expectation: True
```

或用 curl 走完整 API：

```bash
curl -X POST localhost:8000/sessions -H 'content-type: application/json' -d '{"session_id":"demo"}'
curl -X POST localhost:8000/sessions/demo/premises    -H 'content-type: application/json' -d '{"node":"P"}'
curl -X POST localhost:8000/sessions/demo/assumptions -H 'content-type: application/json' -d '{"name":"A"}'
curl -X POST localhost:8000/sessions/demo/assumptions -H 'content-type: application/json' -d '{"name":"B"}'
curl -X POST localhost:8000/sessions/demo/rules -H 'content-type: application/json' -d '{"rule_id":"r1","antecedents":["P","A"],"consequent":"C"}'
curl -X POST localhost:8000/sessions/demo/rules -H 'content-type: application/json' -d '{"rule_id":"r2","antecedents":["P","B"],"consequent":"C"}'
curl -X POST localhost:8000/sessions/demo/rules -H 'content-type: application/json' -d '{"rule_id":"rx","antecedents":["A","B"],"consequent":"⊥"}'
curl localhost:8000/sessions/demo/nodes/C/label
# => {"environments":[["A"],["B"]],"status":"supported","complete":true,...}
curl -X DELETE localhost:8000/sessions/demo/assumptions/A
curl localhost:8000/sessions/demo/nodes/C/label
# => {"environments":[["B"]],...}   -- 撤销 A 后 C 仍由 {B} 支持
```

## API 一览

| 方法/路径 | 说明 |
|-----------|------|
| `POST /sessions` | 创建会话（可带 `budget` 覆盖） |
| `POST /sessions/{id}/assumptions` | 加入并激活假设 |
| `DELETE /sessions/{id}/assumptions/{name}` | 撤销假设（触发重建） |
| `POST /sessions/{id}/premises` | 断言前提（空环境支持） |
| `POST /sessions/{id}/rules` | 添加规则 |
| `GET /sessions/{id}/nodes/{node}/label` | 查询标签 + 完整性状态 |
| `GET /sessions/{id}/nogoods` | 当前极小 nogood 集 |
| `GET /sessions/{id}/diagnostics` | 诊断记录（含请求标识、接受/拒绝原因、关键状态） |

每个响应带 `X-Request-Id` 头；诊断记录默认对符号名做哈希脱敏（设 `ATMS_LOG_SYMBOL_NAMES=1` 才记录明文）。

## 验证方法

- `tests/unit/test_engine_oracle.py`：内核结果与**独立暴力枚举 oracle**（枚举全部假设子集 + 朴素前向链，零共享代码）逐节点对照，并另附手算标签双锚点；同时对照撤销一个假设后的结果。
- `tests/unit/test_engine_nogood.py`：互斥假设、nogood 超集过滤、发现 nogood 后清除既有标签中的失效支持。
- `tests/unit/test_engine_retract.py`：撤销后共享结论保留、依赖该假设的 nogood 消失、重新假设后恢复。
- `tests/unit/test_engine_budget.py`：三类预算各自触发 `incomplete` 的具体原因，以及空标签在不完整时报告 `unknown` 而非 `unsupported`。
- `tests/integration/test_api.py`：TestClient 全流程，断言具体 JSON、请求标识回显、诊断记录内容、会话从操作日志重放后标签一致。
