# 本地神经网络训练工作流（CPU / Dense / ReLU / Dropout）

FastAPI + SQLAlchemy 2.0 + PostgreSQL + PyTorch(CPU) 实现的单机训练服务。
支持网络 JSON 定义与校验、白名单数据集、带租约的训练队列、epoch 边界暂停/恢复/取消、
原子检查点与损坏回退、可断线补取的 SSE 指标流，以及“固定种子恢复训练 ≡ 连续训练”的确定性保证。

不包含审批流、GraphQL、可视化网络编辑器，也不支持 Dense/ReLU/Dropout 以外的层或 GPU。

## 快速开始（Docker）

```bash
# 1. 生成演示数据到 ./data（已随仓库提供两份，可重新生成）
python -m scripts.make_demo

# 2. 启动 PostgreSQL + API/worker
docker compose up --build
```

API: http://localhost:8000  （Swagger 文档：`/docs`）
数据卷：`./data` 以只读挂入容器内白名单目录 `/data`；检查点写入命名卷 `ckpt`。

## 本地（不用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# 准备数据库（PostgreSQL）
sudo -u postgres psql -c "CREATE ROLE trainer LOGIN PASSWORD 'trainer';"
sudo -u postgres createdb -O trainer nn_training
sudo -u postgres createdb -O trainer nn_training_test

# 生成演示数据
python -m scripts.make_demo

# 启动（白名单默认 ./data，可用环境变量覆盖，参考 .env.example）
DATA_WHITELIST_DIRS=$PWD/data CHECKPOINT_DIR=/tmp/nnlab-checkpoints \
  uvicorn app.main:app --reload
```

## 网络 JSON

```json
{
  "input_features": 4,
  "layers": [
    {"name": "h1", "type": "dense",   "out_features": 16, "input": "input"},
    {"name": "a1", "type": "relu",    "input": "h1"},
    {"name": "d1", "type": "dropout", "p": 0.2,           "input": "a1"},
    {"name": "o1", "type": "dense",   "out_features": 3,  "input": "d1"}
  ]
}
```

* `input` 引用另一个层名或虚拟源 `"input"`；仅允许单输入（无多输入合并层）。
* 校验：唯一层名、引用必须存在、禁止自环、Kahn 拓扑排序检测环、恰好一个输出层、
  按拓扑序逐层推导张量宽度；参数范围（1 ≤ dense 宽度 ≤ 100000、0 ≤ dropout p ≤ 0.999）。
* **发布即不可变**：架构只做插入，spec 永不更新；相同内容（规范化后 SHA-256）去重复用。
  训练作业绑定 `architecture_id`、数据集摘要（digest）、随机种子、超参数和划分索引快照。

## API 摘要（所有接口用 `X-User-Id` 标识用户，默认 `anonymous`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/architectures` | 校验并发布网络（幂等，按内容哈希去重） |
| GET  | `/api/architectures`, `/api/architectures/{id}` | 查询 |
| POST | `/api/datasets` | 注册白名单内 CSV / npy，记录行数、特征数、SHA-256 |
| GET  | `/api/datasets` | 查询 |
| POST | `/api/jobs` | 提交作业（每用户最多 3 个**运行中**作业，排队不限） |
| GET  | `/api/jobs`, `/api/jobs/{id}` | 查询（仅本人作业） |
| POST | `/api/jobs/{id}/action` | `{"action": "pause"|"resume"|"cancel"}`，epoch 边界生效 |
| GET  | `/api/jobs/{id}/events?after_seq=N` | JSON 补取事件（幂等，可重复读） |
| GET  | `/api/jobs/{id}/events/stream?after_seq=N` | SSE：先补历史再实时推，含 `ping` 注释；终态发 `end` |
| GET  | `/api/jobs/{id}/checkpoints` | 已发布检查点引用及有效性 |

## 一次完整调用

```bash
B=http://localhost:8000
DS=$(curl -s -X POST $B/api/datasets -H 'X-User-Id: alice' -H 'Content-Type: application/json' \
  -d '{"name":"iris","path":"/data/iris_tiny.csv","task":"classification"}')
A=$(curl -s -X POST $B/api/architectures -H 'X-User-Id: alice' -H 'Content-Type: application/json' \
  -d '{"name":"mlp","input_features":4,"layers":[
       {"name":"h","type":"dense","out_features":16,"input":"input"},
       {"name":"a","type":"relu","input":"h"},
       {"name":"o","type":"dense","out_features":3,"input":"a"}]}')
curl -s -X POST $B/api/jobs -H 'X-User-Id: alice' -H 'Content-Type: application/json' -d '{
  "architecture_id": '$(echo $A | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')',
  "dataset_id": '$(echo $DS | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')',
  "epochs": 10, "seed": 42,
  "hyperparams": {"lr": 0.05, "batch_size": 16, "val_fraction": 0.25}}'
curl -N "$B/api/jobs/1/events/stream" -H 'X-User-Id: alice'
```

## 数据集安全与固定划分

* 路径先 `realpath` 规范化（解析全部符号链接），再校验解析结果必须位于白名单根目录内；
  拒绝 `../` 越界、符号链接逃逸、非常规文件、未知扩展名。
* CSV：首行表头、数值列；默认最后一列为标签，可用 `label_column` 指定。
  npy：2-D，最后一列为标签，`allow_pickle=False`。
* 注册时保存 SHA-256；训练前重新计算并比对，文件被换则拒绝训练。
* 划分索引由 `(行数, val_fraction, seed)` 在**提交时**一次性确定并存在作业行上；
  恢复训练直接复用，绝不重新划分。

## 队列、租约与“旧执行者”隔离

* 领取使用单个事务级 advisory lock 串行化（领取为低频操作），候选行
  `FOR UPDATE SKIP LOCKED`，按创建时间取最早；每用户运行中（持有有效租约）作业精确上限 3，
  某用户到上限时自动跳过看其他用户。
* 领取即写入 `executor_id` 与租约到期时间。心跳（每若干批次）续租。
* worker 每轮先 `reap_expired_leases`：租约过期的运行作业回到 QUEUED 并清掉旧执行者。
* **fencing token**：心跳、追加事件、发布检查点、提交 epoch、标记失败都带 `executor_id`；
  租约丢失/被他人接管后，旧执行者的所有写入 0 行生效（`LeaseLost`），
  不能再提交指标或检查点。

## 暂停 / 恢复 / 取消（epoch 边界）

* pause/cancel 把请求落到作业状态上；执行者在 epoch 开头与训练中续租点发现请求，
  **完成当前 epoch**（暂停）或在该 epoch 提交时一并落终态（取消），随后释放租约。
* resume 仅允许 PAUSED → QUEUED 并清除旧执行者；由新执行者领取后继续。
* 已完成/已取消/失败的作业拒绝重复控制操作（400）。

## 检查点：先落盘、后发布引用、损坏回退

每个 epoch：

1. 写临时文件 `ckpt-<epoch>.pt.tmp.<pid>`（含模型、优化器、torch/numpy/python RNG 状态、
   已完成位置 epoch、magic 版本）；
2. `flush` + `fsync`；
3. `os.replace` 原子改名 + 目录 fsync；
4. 之后才在事务内 upsert `CheckpointRef`（“发布引用”），并校验文件可读；
   因此已发布引用必然指向完整检查点。

恢复时取**最新且校验通过**的检查点；损坏引用标记 `valid=false`（保留审计，不参与回退候选），
加载器回退到最近的有效检查点。只保留最近 3 个有效检查点（引用与文件在提交后清理）。
被回退的 epoch 会重新训练并重新发布同 epoch 引用。

## 确定性（恢复 ≡ 连续）

* 模型构建前统一播种 torch / numpy / python；CPU 单线程保证归约位级一致。
* 每个 epoch 的批次顺序由 `(seed, epoch)` 派生的独立 `torch.Generator` 决定，
  恢复到 epoch N 不依赖此前的 RNG 调用历史。
* 声明容差：逐 epoch 指标 `|Δ| < 1e-6`；测试中权重按 `torch.equal` 比较为**位级一致**
  （见 `tests/test_resume.py`）。

## 事件与 SSE

* 每作业一条 append-only 事件表，`seq` 无空洞（追加前锁作业行取 `max(seq)+1`）；
  事件类型：`status`、`metrics`、`log`。指标全部来自真实训练，不产生训练之外的副作用。
* SSE 连接先按 `after_seq` 重放历史（断线重连补取），再跟实时；终态发送 `end` 关闭，
  空闲时发 `: ping` 注释。重复 GET 事件接口不会触发重复训练。

## 测试

```bash
# 需要 PostgreSQL，连接串由 tests/conftest.py 顶部的默认环境变量指定：
# postgresql+psycopg2://trainer:trainer@localhost:5432/nn_training_test
python -m pytest
```

覆盖（58 个用例）：

* **形状错误**：环、自环、未知输入、多输出、重名、坏层类型、dense/dropout 参数越界、
  回归输出宽度、输入特征不匹配导致作业失败；
* **路径越界**：绝对路径逃逸、`../`、符号链接逃逸（白名单内 symlink 放行）、坏 CSV/npy；
* **并发领取**：两执行者竞争一作业只有一个赢家、并发下每用户上限精确、其他用户不被阻塞；
* **租约**：过期回收再领取、旧执行者心跳/指标被 fence 拒绝、取消竞争不能再写指标；
* **损坏回退**：截断/垃圾文件拒绝加载、逐级回退、runner 恢复时标记坏引用并从上一有效点重训；
* **恢复一致性**：多次暂停恢复的逐 epoch 指标与连续运行一致（<1e-6）、最终权重位级一致、
  恢复不重新划分；
* **SSE**：背靠背补取、`seq` 无空洞、终态 `end`、重复读无副作用。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://trainer:trainer@localhost:5432/nn_training` | SQLAlchemy URL |
| `DATA_WHITELIST_DIRS` | `/tmp/nnlab-data` | 数据集白名单，多个目录用系统路径分隔符（`:`） |
| `CHECKPOINT_DIR` | `/tmp/nnlab-checkpoints` | 检查点根目录，按 `job-<id>/` 分子目录 |
| `LEADER_LEASE_SECONDS` | `30` | 领取租约时长 |
| `WORKER_ENABLED` | `1` | 进程内 worker 开关（测试置 0 同步驱动） |
| `WORKER_THREADS` | `2` | 单进程内 worker 线程数 |
| `MAX_RUNNING_PER_USER` | `3` | 每用户运行中作业上限 |

## 目录结构

```
app/
  config.py       配置
  db.py           engine/session/建表
  models.py       Architecture/Dataset/Job/Event/CheckpointRef
  graph.py        JSON 校验、DAG/形状、torch 模型
  datasets.py     路径安全、CSV/npy、摘要、固定划分
  checkpoints.py  原子写、校验、回退、保留策略
  queue.py        领取租约、fencing、状态机、gapless 事件、引用发布
  runner.py       CPU 确定性训练循环
  worker.py       进程内线程池 worker
  schemas.py      Pydantic 模型
  main.py         FastAPI 路由 + SSE
scripts/make_demo.py
tests/            58 个用例
```
