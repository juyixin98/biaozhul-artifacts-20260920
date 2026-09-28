# 复现文档

环境：Linux x86_64，Python 3.12.3。所有依赖为本地安装，无需外部账号。

## 1. 一次性搭建

```bash
cd <repo>
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt        # 或严格复现：-r requirements-lock.txt
```

## 2. 运行测试（含独立 oracle 差分与随机器）

```bash
python -m pytest tests/ --cov=strips_planner --cov-report=term-missing
```

实测结果：**108 passed**，总覆盖率 **90%**（`.coveragerc` 设 80% 门禁）。

关键测试文件：

| 文件 | 断言内容 |
|---|---|
| `tests/test_parser_validation.py` | 12 个用例：每种畸形输入对应稳定 code |
| `tests/test_semantics.py` | 同一前态判定、不可变后继、冲突明细、增删集合规则 |
| `tests/test_search.py` | 各夹具确切判定/代价/步数；四类界限；去重；轨迹字段 |
| `tests/test_executor.py` | 逐步重放、失败定位到步骤、目标缺口 |
| `tests/test_evidence_pipeline.py` | 证据落库、四类状态可区分、重放判定一致、run_id 唯一 |
| `tests/test_http_api.py` | 全部 HTTP 状态码与信封 |
| `tests/test_reference_oracle.py` | **独立参考实现**：6 夹具 + 40 组随机实例差分 |

> 覆盖率插桩会拖慢搜索。oracle 差分用例因此把墙钟界限放宽到 120s
> （见 `_service_search`），专门的界限测试仍用极小界限触发 `unknown`。

## 3. 启动服务并真实调用

```bash
python -m strips_planner.service                       # 127.0.0.1:8000
bash examples/curl_examples.sh                         # curl 调用集
python examples/python_client.py                       # Python 客户端
```

一键在隔离端口/数据库录制全套正常与异常结果：

```bash
bash scripts/serve_and_record.sh                       # 输出到 docs/results/
```

## 4. 已留存的真实运行结果（可复核）

`docs/results/` 保存了对真实 HTTP 服务调用的响应快照（含 `run_id`，
可在 `data/evidence.db` 中用 `GET /runs/{run_id}` 复核）。判定与代价是
确定性的；展开计数随算法/机器略有差异属正常。

| 快照 | 场景 | HTTP | 关键断言（实测） |
|---|---|---|---|
| `01_solvable_resource_ops` | 资源域可解（UCS 最优） | 200 | `solved`，cost **8**，6 步：move→press→move→heat→move→ship，独立验证 `valid=true` |
| `02_unsolvable_missing_pred` | 无动作产生目标谓词 | 200 | `unsolvable`，穷尽展开 8960，无计划 |
| `03_unsolvable_sealed_dock` | 负条件封锁 dock | 200 | `unsolvable`，穷尽展开 2304 |
| `04_cycles_power` | 含开合电源循环动作 | 200 | `solved`，cost 3，2 步，去重后仅展开 **3** 个状态 |
| `05a_cost_paths_bfs` | 廉价3跳 vs 昂贵直达 | 200 | BFS 选**步数最少**的 teleport：1 步、cost **5** |
| `05b_cost_paths_ucs` | 同上 | 200 | UCS 选**代价最优**：3 次 walk、cost **3** |
| `05c_cost_paths_astar_hadd` | 同上 | 200 | 同样得 3，但 `optimal_guarantee=false`（h_add 不可采纳） |
| `06_unknown_node_limit` | 节点界限=50 | 200 | `unknown`，`reason=node_limit`，expanded=50 |
| `07_unknown_depth_limit` | 深度界限=2，目标需 6 步 | 200 | `unknown`，`reason=depth_limit` |
| `08_input_error_unbound_var` | 效果含未绑定变量 | 422 | `input_error / UNBOUND_VARIABLE`，details 指到 `action move.add` |
| `09_input_error_add_del_conflict` | 同原子既增又删 | 422 | `input_error / EFFECT_ADD_DELETE_CONFLICT` |
| `10_resource_exhausted_grounding` | grounding 界限=5 | 507 | `resource_exhausted / GROUNDING_LIMIT`，details 给出估计实例数 12 |
| `11_malformed_json` | 请求体非 JSON | 422 | `input_error / REQUEST_MALFORMED` |
| `12_solvable_steps` | 逐步执行证据 | 200 | 6 步，每步含前态/后态；首步后 `at(w1, press)` |
| `13_solvable_trace` | 搜索中间状态 | 200 | 至多 500 条，首条 g=0，含 h/f/state_hash |
| `14_run_record` | unknown 运行的落库记录 | 200 | `result_status=unknown, reason=node_limit` |
| `15_replay` | 用存证请求重放 | 200 | 新 run_id，判定 `solved`、cost 8 与首次一致 |
| `16_runs_list` | 运行列表 | 200 | 含域/问题名、判定、代价 |

## 5. 用 run_id 重放单个问题

每次失败都可凭响应中的 `run_id` 回到证据库：

```bash
# 原始请求体（可直接改选项后重放）
curl -s http://127.0.0.1:8000/runs/run-20260928T010400-36229da9 | jq .request_json
# 逐步状态与搜索轨迹
curl -s http://127.0.0.1:8000/runs/<id>/steps | jq
curl -s http://127.0.0.1:8000/runs/<id>/trace | jq
# 以新运行编号确定性重放
curl -s -X POST http://127.0.0.1:8000/runs/<id>/replay | jq
```

日志同时写入 `logs/planner.log`（运行编号、grounding 动作数、搜索判定与
验证结果），SQLite 库存于 `PLANNER_DB`（默认 `data/evidence.db`）。

## 6. 独立参考实现如何避免“自测自”

`tests/test_reference_oracle.py` 中的 `IndependentOracle`：

- 自己用正则解析同一 JSON 语言（不复用 `parser.py`）；
- 用 `itertools.permutations` 自行 grounding（不复用 `grounding.py`）；
- 把每个基原子映射成一个整数位，状态用**位掩码**，后继用位运算；
- 用自己实现的穷举 **Dijkstra**（最优代价）与 **BFS**（最少跳数）求答案。

被测内核只允许与它一致：判定、最优代价、计划可执行性、`h_max` 不高估
代价（可采纳性）在 6 个夹具与 40 个随机实例上逐一断言。此外，oracle
自己产出的计划也交给服务的执行器重放（双向交叉验证）。
