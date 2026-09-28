# 梯度分桶与归约教学运行时（bucket-sync teaching runtime）

一个**纯本地**的多进程同步数据并行教学运行时：FastAPI 控制面 + NumPy
计算，演示梯度分桶（gradient bucketing）、按真实样本数加权的全归约
（all-reduce）、桶世代屏障（generation barrier）与失联拒绝。所有数据均为
固定随机种子生成的合成夹具，无外部账号、无网络依赖。

## 1. 要防住的四类"边界悄悄算错"

| 风险 | 实现手段 | 对应测试 |
|---|---|---|
| 同一轮参数顺序/桶布局漂移，未用参数错位 | `ParameterGraph` 构造后冻结；`BucketLayout` 只由图+桶大小推导一次；非训练参数保留槽位作为**显式零占位**，布局不收缩 | `tests/test_bucketing.py` |
| 不等长批被"平均的平均"错误加权 | worker 上报的是本地批梯度**和**与真实 `n_samples`；归约 `Σ sum_gradient_w / Σ n_w`，分母只进一次样本数 | `test_result_equals_single_process_joint_batch`、`test_weighted_result_differs_from_naive_average_of_averages` |
| 桶完成即提前更新权重，后续梯度读到新权重 | 桶完成只记录数据；权重在 `commit` 时整体交换一次；每个提交必须携带本轮 `base_token`（世代+权重哈希），世代不符直接拒绝 | `test_completed_buckets_do_not_move_weights_before_commit`、`test_stale_base_token_is_rejected` |
| 工作者失联仍提交部分结果 | 进程退出/心跳超时立即把该轮标记为 `aborted`；迟到提交返回 `round_aborted`；提交结果为"拒绝"而非静默丢弃 | `test_worker_loss_aborts_round_and_rejects_late_submissions`、`test_worker_crash_aborts_round_then_survivors_continue_correctly`、`test_hung_worker_is_detected_via_missing_heartbeats` |

缺梯度不能伪装成零梯度：每个桶段附带显式布尔 `present_mask`。
- **严格模式（默认）**：任一活跃 worker 未覆盖某可训练槽位 → `commit`
  返回 `undecided / missing_gradient`，权重绝不更新；
- **部分覆盖模式**：仅教学演示用，按槽位用实际覆盖该槽位的 worker 样本数
  归一化（证据中给出每个桶的覆盖人数与最小样本权重）。

## 2. 算法假设

1. 教学模型为线性回归 `y = x @ w + b` + 均方误差；所有计算 float64。
2. 同步 BSP 语义：一轮内所有活跃 worker 从同一组基线权重出发；提交携带
   `base_token = hash(generation, params)`，不同基线的梯度一律拒绝。
3. worker 上报本地批的梯度**和**（不是均值），归约端除以联合样本总数，
   因而与"单进程把所有分片拼成一个大批"在数学上**逐位等价**（测试容差
   1e-9～1e-10，实际误差为 0）。
4. 桶只是扁平梯度向量上的连续定长切片；桶之间相互独立，完成顺序不影响
   结果（用正序/逆序/乱序三种顺序断言哈希一致）。
5. 失联判定：worker 子进程退出（driver 直接观察）或心跳超时（默认 1s）。
   失联即中止当前轮；下一轮只包含仍存活的 worker。
6. 非训练参数在布局中保留固定槽位，提交必须为零、掩码必须声明不覆盖。

## 3. 模块关系（张量类型 / 计算图 / 训练状态 / 数值验证分离）

```
tensor_types.py  TensorSpec：形状/dtype/有限性边界（不可变值对象）
graph.py         ParameterGraph / ParameterNode：冻结的有序参数图
bucketing.py     BucketLayout：参数↔扁平向量↔桶的固定映射、占位掩码
training.py      ModelState（不可变更新）、本地梯度和计算
reference.py     独立单进程联合批 oracle（不 import 被测归约/协调器）
round_types.py   RejectReason/RoundStatus、提交/提交报告/轮次等值对象
reduction.py     纯函数加权桶归约（梯度和 ÷ 覆盖样本数）+ 桶证据
coordinator.py   轮次状态机、显式覆盖校验、提交屏障、诊断
diagnostics.py   结构化 accept/reject/undecided 事件 + 张量化脱敏
api.py           FastAPI：协调器的薄 HTTP 外壳（拒绝是带类别的数据，非 5xx）
worker.py        worker 进程循环：取基线→本地求和→分桶→乱序提交→心跳
runtime.py       本地编排：uvicorn 线程 + spawn 进程 + 轮次驱动/超时
config.py        RuntimeConfig 与固定种子合成夹具
examples/demo.py 三个教学场景，打印桶世代与归约依据
tests/           独立组织：单元 / 集成 marker，conftest 夹具，drivers 驱动
```

依赖方向严格自上而下；`reference.py` 与 `tests/drivers.py` 只复用模型参数名
等常量，不复用任何被测归约逻辑，因此参考答案是独立推导的。

失败类别（`RejectReason`，机器可读字符串）：
`unknown_worker`、`worker_lost`、`not_participant`、`round_not_open`、
`round_aborted`、`round_committed`、`wrong_round`、`wrong_base_generation`、
`unknown_bucket`、`bad_shape`、`non_finite`、`invalid_sample_count`、
`sample_count_mismatch`、`duplicate_bucket`、`placeholder_nonzero`、
`bad_mask`、`missing_gradient`、`incomplete_submission`、
`worker_skipped_round`。

诊断事件带 `record_id`（HTTP 层还有 `x-request-id`）、轮次、worker、
outcome、reason 与关键状态（世代、样本数、覆盖人数）。张量值只记录
形状 / 四舍五入的 L2 范数 / sha256 前 12 位，不打印任何原始标量。

## 4. 依赖版本（本机实测）

- Python 3.12.3
- numpy 2.4.6
- fastapi 0.141.1
- uvicorn 0.54.0
- 测试：pytest 9.1.1、httpx 0.28.1、pytest-cov 7.1.0

安装（可选，环境中已具备）：

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -e ".[dev]"
```

## 5. 本地验证命令与预期判断方式

```bash
# 1) 全量测试（77 个）：期望 77 passed，0 failed
python3 -m pytest -q

# 2) 仅快速单元测试（不启动子进程/HTTP）
python3 -m pytest -q -m unit

# 3) 真实多进程 + HTTP 集成测试
python3 -m pytest -q -m integration

# 4) 覆盖率（期望总行覆盖率 >= 80%，当前约 94%）
python3 -m pytest -q --cov=src/bucket_sync --cov-report=term-missing

# 5) 教学演示：三个场景，末行期望
#    "All demo scenarios completed and matched independent expectations."
python3 examples/demo.py
```

判定方式（不是"接口能调用"就算过）：

- 数值断言：分布式多进程结果与 `reference.py` 的单进程联合批 oracle
  逐元素比较，`max abs error = 0.00e+00`（或 < 1e-9）；并显式断言它**不
  等于**"平均的平均"错误基线。
- 世代断言：提交前后 generation 数字、每桶证据中的 `generation`、
  `generation_before/after` 全部精确比较。
- 失败断言：每个故障路径断言具体 `RejectReason` 类别与记录 id，并核对
  诊断 detail（如期望/实际 base_token、缺失槽位、各 worker 样本数）。
- 进程级故障：真实 spawn 子进程，模拟提交 1 个桶后崩溃、提交后挂起
 （无心跳）、错误世代令牌、缺梯度、不等批量、乱序完成。

## 6. 测试结果如实标记

- `python3 -m pytest`：**77 passed, 0 failed**（60 个 unit + 17 个 integration，本机已运行）。
- 多进程集成测试在 Linux/Python 3.12 上运行；Windows/macOS 未在本机验证。
- `bandit` 静态安全扫描：本机未安装，**未运行**（代码无外部输入落盘/
  shell/SQL；唯一外部输入是经 pydantic 校验的 localhost JSON）。
- `ruff`/`black`：本机未安装，**未运行**；已用 `compileall` 字节编译通过。

## 7. HTTP 接口速览

`POST /workers/{id}/register|heartbeat|lost`、`POST /liveness/check`、
`POST /rounds/begin|commit|abort`、`POST /rounds/skip/{id}`、
`POST /buckets/submit`（含 `present_mask`）、`GET /rounds/current`、
`GET /state`、`GET /diagnostics`、`GET /health`。拒绝提交返回 HTTP 200 +
`{"accepted": false, "reason": <类别>, "record_id": ...}`，便于客户端按
类别处理而不是靠状态码猜。
