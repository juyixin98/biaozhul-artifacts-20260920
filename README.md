# 训练检查点恢复服务（Training Checkpoint Recovery）

纯后端本地机器学习基础设施服务，用 **Python 3 + NumPy** 从零实现。不下载任何外部模型或数据，
使用可复现的合成数据与一个小型线性回归模型，验证"训练中断 → 从检查点恢复 → 结果与不中断训练一致"
这一核心机制。无前端、无 Web 框架、无第三方运行时依赖（仅 NumPy）。

---

## 1. 它验证什么

一次可精确恢复的训练，检查点**绝不能只存权重**。本服务的每个检查点包含四个必需部分：

| 部分 | 内容 | 不保存的后果 |
|------|------|--------------|
| 模型参数 | `W`, `b` | 无法继续训练 |
| **优化器状态** | 动量 SGD 的速度 `vW`, `vb` | 动量丢失，更新轨迹偏离 |
| **随机数状态** | NumPy BitGenerator 完整 state | 洗牌/抽样序列不可复现 |
| **数据游标** | epoch、epoch 内位置 `pos`、当前置换序、`global_step` | 不知道下一批该取哪些样本 |

此外还保存：训练配置、数据集指纹（SHA-256）、逐步损失历史、检查点格式版本。

**恢复语义**：进程在任意批次被杀死后重启，从最近一次已提交检查点重建全部状态。
若崩溃点落在两次提交之间，未落盘的批次会被**确定性重放**——因为 RNG 与游标完全恢复，
重放的每个批次与不中断训练中对应批次取到完全相同的样本，最终参数与损失逐位一致。

## 2. 目录结构

```
checkpoint_service/
  config.py       TrainConfig（不可变 dataclass，入口处校验）与容差常量
  synthetic.py    由 data_seed 确定性生成的合成线性数据集 + SHA-256 指纹
  model.py        从零实现的线性模型（MSE + L2，解析梯度，float32）
  optimizer.py    带动量的 SGD（速度缓冲区即必须持久化的优化器状态）
  cursor.py       数据游标（epoch / pos / 置换序 / global_step）
  checkpoint.py   原子提交、校验和、严格校验（损坏/只存权重一律拒绝）
  trainer.py      可中断/可恢复的训练核心
  compare.py      验收对比：多批次点中断 vs 不中断
  service.py      服务层：run 的创建、训练、状态、检查点检视
  api.py          stdlib http.server 实现的 HTTP/JSON API
  __main__.py     命令行入口（create-run/train/status/inspect/list-runs/serve）
scripts/
  run_demo.py        同进程多中断点验收对比（输出 JSON 报告）
  crash_worker.py    真实子进程 worker，可在指定步骤硬退出（模拟 SIGKILL）
  run_crash_demo.py  真实杀进程 → 新进程重启 → 与参考运行对比
tests/               pytest 自动化测试（74 个，覆盖率 93%）
examples/            HTTP 请求样例（curl 脚本 + .http 文件 + 实际响应留存）
```

## 3. 安装

无需联网安装模型/数据。仅需 Python ≥ 3.10 与 NumPy：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # 只有 numpy
# 开发/测试：pip install pytest pytest-cov
```

## 4. 快速开始

### 4.1 一键验收对比（同进程，多中断点）

```bash
python scripts/run_demo.py --out ./demo_out
```

它训练一次不中断的参考运行，再训练 7 个在不同批次中断后恢复的运行，逐一对比最终
`W / b / 全量损失 / 逐步损失`。详见 [`docs/RUN_REPORT.md`](docs/RUN_REPORT.md)。

### 4.2 真实进程杀死 → 重启（跨进程）

```bash
python scripts/run_crash_demo.py --crash-at 13
```

worker 子进程在第 13 个优化步后 `os._exit(137)`（等价 SIGKILL，无任何清理），
第二个新进程从第 8 步的检查点恢复、确定性重放 9–13 步并训练到底，再与独立参考运行逐位对比。

### 4.3 HTTP 服务

```bash
python -m checkpoint_service --runs-dir ./runs serve --host 127.0.0.1 --port 8080
```

另一终端：

```bash
bash examples/http_requests.sh
```

### 4.4 命令行

```bash
python -m checkpoint_service --runs-dir ./runs create-run --run-id r1 --n-epochs 4
python -m checkpoint_service --runs-dir ./runs train  --run-id r1 --steps 10
python -m checkpoint_service --runs-dir ./runs status --run-id r1
python -m checkpoint_service --runs-dir ./runs inspect --run-id r1
python -m checkpoint_service --runs-dir ./runs list-runs
```

## 5. HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| GET  | `/runs` | 列出全部 run（损坏的会标注 `corrupt`） |
| POST | `/runs/{run_id}` | 创建 run（body 为配置字段，可空；立即提交 step-0 检查点） |
| POST | `/runs/{run_id}/train` | 从检查点恢复并训练；body `{"stop_after": N}` 可选 |
| GET  | `/runs/{run_id}/status` | 当前参数、游标、损失等 |
| GET  | `/runs/{run_id}/checkpoint` | 检视已提交检查点包含哪些部分 |

错误以非 2xx + `{"error": <异常类型>, "detail": ...}` 返回：
`400 InvalidRequestError`、`404 RunNotFoundError/CheckpointNotFoundError`、
`409 RunExistsError/DatasetMismatchError`、`422 CheckpointCorruptError/CheckpointFormatError`。

完整请求/响应样例见 [`examples/api_requests.http`](examples/api_requests.http)、
[`examples/http_requests.sh`](examples/http_requests.sh)、
[`examples/sample_output.txt`](examples/sample_output.txt)。

## 6. 检查点磁盘格式与崩溃安全

每个 run 目录下两个文件：

```
checkpoint.payload   pickle 序列化的完整状态
checkpoint.meta      提交标记：payload 的 sha256、字节大小、格式版本
```

提交流程（崩溃安全）：先写 `payload.tmp` 并 fsync → 原子 `rename` 覆盖 payload →
再原子写 meta（meta 内含 payload 校验和）→ fsync 目录。因此读端只会看到
"无检查点"或"完整提交的检查点"两种状态，绝不会读到半成品。

加载时依次校验：meta 可读 → 字节大小一致 → **SHA-256 一致** → 可反序列化 →
结构完整（四个必需部分齐全、形状/dtype 正确、RNG state 可装载、游标置换合法、
数据集指纹合法、格式版本受支持）。任一不过即拒绝。

- **损坏检查点**（位翻转 / 截断 / meta 损坏 / 校验和不符）→ `CheckpointCorruptError`（HTTP 422）。
- **只存权重 / 缺少优化器状态、RNG 或游标** → `MissingCheckpointFieldError`，错误信息明确提示
  "weights-only checkpoints are rejected"（HTTP 422）。
- 校验在写盘前进行，非法状态不会覆盖此前已提交的完好检查点。

## 7. 两种"停止"语义

- **优雅暂停**（API/CLI 带 `stop_after`）：本次取得的进度在返回前落盘，下次从精确位置继续。
- **硬崩溃**（真实 OS 杀进程）：只有周期性提交（每 `checkpoint_every` 步）存活，
  恢复点可能早于崩溃点，中间批次确定性重放。验收演示覆盖这两种情况。

## 8. 容差

见 `checkpoint_service/config.py`：参数容差 `PARAM_ATOL = 1e-6`、损失容差 `LOSS_ATOL = 1e-6`。
由于 RNG/游标/优化器状态完全恢复、float32 运算按相同顺序重放，本实现的实测差异为 **0.0**
（逐位一致），优于规定容差；容差作为跨平台/浮点次序的明确余量保留。

## 9. 测试

```bash
python -m pytest                                  # 全部 74 个
python -m pytest -m integration                   # 仅集成测试
python -m pytest --cov=checkpoint_service         # 覆盖率（实测 93%）
```

覆盖重点：梯度有限差分校验、数据集指纹、游标 epoch 边界（批次末尾）、原子提交、
**损坏检查点拒绝**、**只存权重拒绝**、缺任一关键部分拒绝、未知版本拒绝、
多批次点（含提交边界、区间中部、epoch 边界、最后一步、多次中断）恢复一致性、
重放批次与不中断序列逐批相同、数据集指纹不符拒绝恢复、HTTP 全链路（含 422/409/404/400）。

## 10. 范围与非目标

- 纯后端：无任何前端代码/页面。
- 不下载外部模型、权重或数据；合成数据由种子确定性生成。
- pickle 仅用于本地、经校验和保护的状态文件；服务不接受任意上传的检查点。
