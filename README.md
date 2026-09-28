# gradbucket — 本地多进程同步训练的梯度分桶与归约教学运行时

一个**可判定、可证伪**的同步 SGD 教学运行时：Python + FastAPI + NumPy，
全部数据为本地确定性合成夹具，无外部账号、无真实业务数据。

它刻意聚焦同步分布式训练里四个"正常输入看起来对、边界输入悄悄算错"的问题，
并为每一类提供**具体数值断言**与**独立参照答案**：

1. **固定参数顺序与桶布局**：轮内槽位（slot）偏移由参数声明顺序唯一确定，
   与梯度到达顺序无关；未计算的参数也必须占槽——显式零向量 + `mask=False`，
   不允许"缺槽"。
2. **按真实样本数平均不等长批**：每个参数槽的结果是
   `Σ_w n_w·g_w / Σ_w n_w`，分母**逐槽统计**覆盖该参数的工作者，
   绝不按工作者数简单平均。
3. **桶完成不提前更新权重**：桶收齐只把归约结果放入暂存区（STAGED），
   世代与权重不变；只有全部桶封存（seal）成功才**原子**替换权重、世代 +1。
4. **工作者失联该轮拒绝提交**：封存时欠桶且欠桶工作者心跳超时 →
   `REJECTED_WORKER_LOST`；欠桶但心跳仍新鲜 → `INDETERMINATE_PENDING`
   （保持轮开放，稍后可补齐），把"拒绝"与"暂时无法判定"严格分开。

---

## 目录与模块职责

```
gradbucket/
  tensors.py      张量类型：ParamSpec / 固定 BucketLayout / 槽位 / pack/unpack / 占位
  graph.py        计算图：线性模型 f(x)=Wx+b 的 MSE 平均损失梯度 + 确定性合成数据/分片
  reducer.py      归约核心：逐槽 mask + 真实样本数加权（纯函数，无状态）
  training.py     训练状态机：注册/提交/暂存/封存两阶段提交、世代、心跳判活
  reference.py    独立参照：单进程联合批直接求和（刻意不调用 reducer）+ 手算常数
  diagnostics.py  结构化诊断：request_id + verdict + 关键状态；张量只留脱敏指纹
  server.py       FastAPI 薄门面：JSON<->ndarray，判定全部委托 coordinator
  runtime.py      确定性编排：FakeClock 模拟完成顺序/缺梯度/不等批量/中断
  cluster.py      真实多进程：uvicorn 子进程 + worker 子进程 + HTTP
  worker_cli.py   工作者进程入口（支持 --omit / --fail-after-bucket / --withhold-bucket）
  config.py       JSON 运行配置加载
  cli.py          教学演示入口
tests/            按主题独立组织的 122 个测试（含独立小预言机）
configs/          JSON 配置示例
logs/             诊断与集群运行日志（运行后生成）
```

模块依赖方向（无环）：

```
tensors ─► graph ─► (reference 独立直连 graph)
   │                  reducer ─► tensors, diagnostics
   └──────────────►   training ─► reducer, tensors, diagnostics
                          runtime ─► training, graph, reducer
                server ─► training      cluster/worker_cli ─► server(HTTP)+graph
```

`reducer` 是无状态纯函数；`training.RoundCoordinator` 是 API 之下唯一持有
训练状态的对象，内部一把 `RLock` 保护所有读改写。

---

## 算法假设与语义

- **模型**：多输出线性模型，逐样本损失 `0.5·‖Wx+b−y‖²`，分片平均梯度
  `g = (1/N) Σ_i ...`。选它是因为联合批梯度可手工核对，且能体现不等批量。
- **同步训练契约**：所有工作者在**轮初同一权重快照、同一世代号**上计算；
  提交必须携带其依据的世代号，不符即 `REJECTED_STALE_ROUND`，杜绝跨轮梯度。
- **桶布局**：参数按声明顺序顺序填满容量 `bucket_capacity` 的桶。
  参数张量展平后占定长槽；桶向量是该桶各槽的**局部**连续拼接（从偏移 0
  开始），而 `Slot.offset` 是轮内全局偏移——二者区分明确，不混用。
- **遮罩粒度是整槽**：一个参数要么整槽算好（mask 全 True），要么整槽占位
  （全 False）。半截遮罩直接 `REJECTED_PARTIAL_SLOT_MASK`，防止"部分元素
  被当零参与平均"。
- **逐槽分母**：占位贡献者从该槽分子分母同时剔除；因此不同参数槽的有效
  样本数可以不同。某槽无任何覆盖者时，结果置零但标记 `covered=False`：
  若该参数是声明的已知零参数（如不参与损失的 `spare`）则放行；否则该槽
  只可能在桶收齐后暴露，而桶齐即无法补提，封存判终局
  `REJECTED_NO_EVIDENCE`（可 reset 重开）——不把零猜测成"平均梯度为零"。
- **样本数上界与非有限结果**：样本数必须是真正的正整数（布尔拒绝）且
  `≤ 2^53`；即使输入全有限，加权结果若溢出为 inf/NaN 也在封存/暂存前拒绝，
  绝不提交 NaN 权重。
- **数值顺序无关**：reducer 内部按 worker_id 排序后以固定结合顺序累加，
  因此不同完成/到达顺序产生**逐位相同**结果（测试用 `array_equal` 而非
  `allclose` 验证）。
- **判活与开工宽限**：以"最后一次接触"（心跳或成功提交）时间为准；只对
  **仍欠桶**的工作者判活——已交齐所有桶的工作者正常退出不算失联。工作者
  **首次接触之前**有开工宽限期，期内不把"启动慢"永久误判为失联。
- **seal 幂等**：封存是网络重试高发操作；已终结的轮重复 seal 返回缓存结局，
  不二次提交、不抛 500。
- **优化器**：纯函数 SGD，`w' = w − lr·g`；封存点一次性替换权重（不可变更新，
  不就地修改旧 dict）。

### 判定（verdict）类别

| 类别 | 含义 | 轮的结局 |
|---|---|---|
| `ACCEPTED` | 提交被接受；或封存成功，世代 +1 | COMMITTED |
| `REJECTED_BUCKET_SHAPE` | 桶向量/mask 长度与固定布局不符 | 提交被拒，轮不变 |
| `REJECTED_PARTIAL_SLOT_MASK` | 参数槽出现半截遮罩 | 提交被拒 |
| `REJECTED_NON_FINITE` | 梯度或归约结果含 NaN/inf | 提交/该轮被拒 |
| `REJECTED_SAMPLE_COUNT` | 样本数非正、为布尔或超 2^53 | 提交被拒 |
| `REJECTED_DUPLICATE` | 同工作者对同桶重复提交（优先判定） | 提交被拒 |
| `REJECTED_STALE_ROUND` | 轮次/世代不符或未知工作者 | 提交被拒 |
| `REJECTED_SEALED` | 轮已封存/拒绝后迟到提交 | 提交被拒 |
| `REJECTED_WORKER_LOST` | 欠桶 + 欠桶者超时/超开工宽限 | 轮 REJECTED，权重不更新 |
| `REJECTED_NO_EVIDENCE` | 桶齐但必需槽零证据且无法补提 | 轮 REJECTED，可 reset 重开 |
| `INDETERMINATE_PENDING` | 欠桶但欠桶者心跳仍新鲜 | 轮保持 OPEN，可补齐 |

---

## 本地验证

### 依赖版本（实测环境）

| 组件 | 版本 |
|---|---|
| Python | 3.12.3 |
| numpy | 2.4.6 |
| fastapi | 0.141.1 |
| pydantic | 2.13.5 |
| uvicorn | 0.54.0 |
| httpx | 0.28.1 |
| pytest | 9.1.1 |
| pytest-cov | 已安装即可 |

安装（可选，依赖均已满足时可跳过）：

```bash
pip install -e ".[test]"
```

### 1) 确定性场景演示（单进程，立即返回）

```bash
python -m gradbucket.cli demo-all
```

**预期判断**：末尾打印 `总体判断: 通过`，退出码 0。其中包含

- `demo-happy` / `demo-unequal`：`ACCEPTED`，世代 `0 -> 1`，每个参数槽
  对照"单进程联合批"最大绝对偏差约 `1e-16`（浮点取整级别）；
- `demo-omit`：`ACCEPTED`，`w` 槽有效样本 `N=5`、`b` 槽 `N=12`（分母逐槽不同）；
- `demo-lost`：`REJECTED_WORKER_LOST`，理由含丢失工作者与缺失桶，世代保持 0；
- `demo-pending`：`INDETERMINATE_PENDING`，轮仍 OPEN；
- 顺序交叉验证：全部代表完成顺序 `-> 逐位一致: True`。

单场景与诊断导出：

```bash
python -m gradbucket.cli demo-lost --dump-diag logs/demo-lost.jsonl
```

### 2) 测试套件

```bash
# 全部（含真实多进程集成，约 13 秒）
python3 -m pytest tests/ -q

# 仅快速单元/属性测试（不含派生进程）
python3 -m pytest tests/ -q -m "not integration"

# 仅真实多进程端到端
python3 -m pytest tests/ -m integration -q

# 覆盖率
python3 -m pytest tests/ -q --cov=gradbucket --cov-report=term-missing
```

**预期判断**：`122 passed`；总体覆盖率约 95%，其中 `reducer.py`、
`graph.py`、`reference.py` 为 100%，`training.py` 97%、`runtime.py` 99%。
任何 `FAILED` 都会打印具体数值差异与失败类别。

测试如何避免"答案由被测核心自己生成"：

- `tests/conftest.py::explicit_weighted_average`：测试自带的**纯 Python
  循环**加权平均，实现路径与 numpy/reducer 完全不同；
- `tests/conftest.py::finite_difference_grad`：**中心差分**数值梯度，独立于
  解析梯度推导；
- `gradbucket/reference.py::union_batch_gradient`：原始样本拼接的**单进程
  联合批**，不经过分桶/reducer；
- `gradbucket/reference.py::hand_computed_constants`：1/2/1 不等分的
  **手算字面量**，并给出"按人头平均会得到 −3.333、正确为 −2.5"的判别反例。

### 3) 真实多进程演示（uvicorn + 3 个独立工作者进程 + HTTP）

```bash
python -m gradbucket.cli cluster --workers 3 --samples 12
cat logs/cluster.log     # 子进程 stdout/stderr、seal 判定、诊断计数
```

**预期**：`code: ACCEPTED`、`generation_after: 1`，工作者退出码全 0，
`w` 槽归约依据列出三个工作者与样本数。集成测试 `tests/test_cluster.py`
另外固定了：交完桶 0 即崩溃的工作者返回退出码 **3** 且整轮
`REJECTED_WORKER_LOST`；压下桶 1 但持续心跳时为 `INDETERMINATE_PENDING`。

---

## HTTP 接口摘要

| 方法与路径 | 作用 |
|---|---|
| `POST /rounds/begin` | 声明固定参数布局、初始权重、在册工作者 |
| `GET  /rounds/current` | 轮状态：桶布局、暂存桶、缺失桶、心跳年龄 |
| `GET  /weights` | 当前权重与世代（封存前恒为轮初快照） |
| `POST /workers/{id}/heartbeat` | 续约 |
| `POST /rounds/{r}/buckets/{b}` | 提交桶向量 + mask + 真实样本数 + 世代 |
| `POST /rounds/{r}/seal` | 封存判定，成功才返回 commit（世代/权重/归约依据） |
| `POST /rounds/reset` | 终结被拒轮以便开新一轮 |
| `GET  /diagnostics` | 全部诊断记录与 verdict 计数 |

所有响应带 `request_id` 与 `verdict/code`；错误详情用稳定 `code` 表示
失败类别。诊断与响应**不回传梯度张量**，只给形状、有限性、L2 范数指纹。

## 局限（如实说明）

这是教学运行时，不是生产参数服务器：单机内存状态、无持久化、无 TLS/鉴权、
无分片重平衡；判活用单一时钟与固定超时；只实现 SGD。这些简化都被显式
建模为可测试的边界，而不是隐藏行为。
