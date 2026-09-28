# Sparse Embedding Gradient Update Service

大词表嵌入表的**稀疏梯度累积与优化服务**。Python 3.11+ / FastAPI / NumPy，
全部数据为本地合成夹具，无外部账号或真实业务数据依赖。

交付的是**可审查的实现**，不是方案说明：重复索引先聚合、零梯度与未触及行的
优化器步数规则明确、裁剪严格区分全局/行级、索引越界整批拒绝、持久化只写实际
触及行并保留事务边界。

---

## 1. 目录结构（按关注点分层）

```
src/sparse_embedding/
  tensors.py       # 层1 张量类型：dtype/形状/索引范围的整块校验
  graph.py         # 层2 计算图原语：重复索引聚合(np.add.at)、全局/行裁剪
  optimizer.py     # 层3a 纯优化器行规则：SGD / 动量SGD / 耦合权重衰减
  state.py         # 层3b 训练状态：稀疏事务、零梯度/未触及规则、回滚镜像
  persistence.py   # 层4 事务持久化：只存触及行，临时文件+fsync+原子改名
  service.py       # 层5b 服务门面：加锁、判定(verdict)、结构化日志
  app.py           # 层5a FastAPI 边界：显式错误码→HTTP 映射
  schemas.py       #       HTTP 请求/响应模型
  config.py        # 独立配置层：不可变、启动期校验、JSON 往返
  journal.py       # 结构化运行日志：run_id 关联、版本、计算阶段、判定
  errors.py        # 封闭错误类别（绝不把异常/未知态折叠成成功）

tests/
  reference_oracle.py  # 独立稠密参考实现（纯Python dict聚合/手写公式）
  conftest.py
  test_tensors.py      # 层1
  test_graph.py        # 层2（含手算夹具断言、全局vs行裁剪不可混用）
  test_optimizer.py    # 层3a（手算公式）
  test_state_oracle.py # 层3b/数值验证：对照独立稠密oracle
  test_persistence.py  # 层4：稀疏落盘、原子性、形状不兼容拒绝、重启恢复
  test_service_api.py  # 层5：门面 + 真实HTTP + 日志关联
  test_config.py       # 配置层

fixtures/             # 最小合成夹具（含手算期望值，非被测代码生成）
scripts/              # 调用示例与可复现演示
results/              # 已留档的真实运行结果（测试报告、端到端结果、日志）
docs/
```

单文件实现、纯调用壳、固定返回值均**不**满足本工程要求——每层都有独立测试，
参考答案（`reference_oracle.py`）使用与被测核心**不同的机制**独立实现。

---

## 2. 明确的语义规则（复核重点）

| 关注点 | 规则 | 位置 |
|---|---|---|
| 重复索引 | 同一 batch 内重复 ID **先求和**再进优化器；用 `np.add.at`（无缓冲 scatter-add），重复行不会被覆盖 | `graph.aggregate_duplicates` |
| 零梯度行 | 聚合后**全零**的行**不产生优化器步**：权重、动量缓冲、逐行计数器都不变 | `state.apply_batch` |
| 未触及行 | 不在 batch 中的行**完全不动**：无动量衰减、无权重衰减；逐位保持 | `state.apply_batch` |
| 空批 | 显式 `empty` 判定（非静默成功）：不聚合、不裁剪、不步进，全局步数不增加 | service / app |
| 非空但全零 | 仍计为一个更新轮次（全局步数 +1），但逐行计数器只对真正步进的行 +1 | `state.apply_batch` |
| 裁剪模式 | `none`/`global`/`row` 为封闭枚举，**互斥代码路径，绝不混用**；全局用一个共享系数，行级每行独立系数 | `graph.clip_gradients` |
| 索引越界 | 在任何聚合/写入**之前**对全部索引做范围检查，**一个坏索引整批拒绝**，状态零改动 | `tensors` 校验 |
| 持久化 | 只序列化真正步进过的触及行；`*.tmp`+fsync+`os.replace` 原子提交，失败保留旧检查点 | `persistence.CheckpointStore` |

---

## 3. 快速开始

```bash
# 依赖（环境已具备时可跳过；精确版本见 requirements.lock.txt）
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock.txt
pip install -e .

# 运行全部测试（含覆盖率）
python3 -m pytest --cov=sparse_embedding --cov-report=term-missing

# 直接驱动数值核心（不起服务）
python3 scripts/example_core.py

# 真实 HTTP 端到端演示（后台起 uvicorn，覆盖正常+异常+持久化重启）
python3 scripts/run_demo.py
```

### 启动服务并用 curl 调用

```bash
SPARSE_EMB_CONFIG=fixtures/config_small.json \
SPARSE_EMB_STATE_DIR=var/state \
uvicorn sparse_embedding.app:app --app-dir src --host 127.0.0.1 --port 8000

# 另开终端：
bash scripts/call_api.sh
```

环境变量：`SPARSE_EMB_CONFIG`（JSON 配置路径）、`SPARSE_EMB_ROWS`、
`SPARSE_EMB_DIM`、`SPARSE_EMB_STATE_DIR`、`SPARSE_EMB_LOG`。

### 主要端点

- `GET  /health`
- `POST /apply`        入参 `{run_id?, batch_id?, indices[], values[][]}`
- `GET  /state/summary`
- `GET  /state/row/{index}`
- `POST /checkpoint`

返回的 `verdict ∈ {applied, applied_zero_only, empty}`，错误返回
`error_code ∈ {batch_rejected, empty_batch, persistence_error, state_shape_error}`。

---

## 4. 数值验证方法（参考答案的独立性）

`tests/reference_oracle.py` 是一个**小词表稠密**参考实现：

- 聚合用 Python `dict` 按输入顺序累加（非 NumPy scatter）；
- 范数与裁剪系数用闭式公式逐元素手算；
- 优化器是稠密嵌套列表的动量 SGD，未触及行靠"从不访问"天然冻结。

它与被测 NumPy 稀疏核心**只共享初始表数值与标量超参**，后续每个数字都独立
计算。`test_state_oracle.py` 在重复ID、热冷交替、空批、大梯度、动量累积、
全局/行裁剪、纯SGD 等场景下断言两者的**权重、动量缓冲、逐行步数、全局步数**
逐元素一致（容差 1e-10）。此外 `fixtures/batches_small.json` 中的聚合/范数
期望值为**手工计算**，不来自被测实现。

---

## 5. 可复核的已留档结果

- `results/test_report.txt` —— 66 项测试全部通过，总覆盖率 **92%**。
- `results/demo_results.json` —— 真实 HTTP 运行的逐请求结果（含状态码、
  verdict、裁剪依据、重启恢复后的动量/步数）。
- `results/demo_state/journal.jsonl` —— 结构化日志，每行带 `run_id`、
  `versions`（python/numpy/fastapi/service）、`stages`（执行到的计算阶段）、
  `verdict` 与 `error_code`，异常与空批均为独立判定，不记为成功。

复现这些结果：

```bash
python3 -m pytest --cov=sparse_embedding --cov-report=term-missing | tee results/test_report.txt
python3 scripts/run_demo.py     # 重写 results/demo_results.json 与演示状态/日志
```

---

## 6. 设计取舍

- **float64 单一精度**：消除隐式精度转换；大梯度（1e6 量级）在 float64 下精确。
- **纯函数核心 + 唯一写入边界**：`graph`/`optimizer` 不修改入参；唯一就地写
  发生在 `state.apply_batch` 的活跃行上，并在写入前捕获稀疏 before-image，
  数值异常时整批回滚。
- **不可变配置**：`frozen=True` dataclass，启动期校验（lr、momentum 区间、
  裁剪阈值、`step_zero_rows` 禁止开启等），核心不读环境变量。
- **线程安全**：服务门面用 `RLock` 串行化状态变更。
