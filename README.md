# 训练检查点恢复（checkpoint-resume）

纯后端、纯 Python + NumPy 的本地机器学习基础设施服务：在可复现的合成数据上训练小型线性模型，
把**完整训练状态**存入检查点，进程中断后从检查点恢复，最终参数与损失和「不中断训练」逐位一致。
不下载任何外部模型或数据，无前端。

## 检查点里存什么（禁止只存权重）

| 内容 | 字段 | 为什么必须存 |
|---|---|---|
| 模型参数 | `model_w`, `model_b` | 显而易见 |
| 优化器状态 | `opt_vw`, `opt_vb`（动量缓冲） | 只存权重会丢动量，恢复后轨迹立即偏离（有测试专门验证） |
| 随机数状态 | `rng_keys`, `rng_pos`, `rng_has_gauss`, `rng_cached_gauss` | 决定后续 epoch 的 shuffle 顺序 |
| 数据游标 | `epoch`, `next_batch`, `perm`（当前 epoch 排列） | 决定下一批读哪些样本；批次末尾（`next_batch == n_batches`）是边界情形 |
| 步数与元数据 | `global_step` + JSON meta（配置、版本、保存时间） | 恢复前校验配置一致，防止恢复错运行 |

文件格式：`step_XXXXXXXX.ckpt.npz` + 配套 `step_XXXXXXXX.ckpt.sha256` 校验文件。
写入用「临时文件 + fsync + 原子 rename」，加载前强制 SHA-256 校验，
任何字节损坏都会抛出 `CorruptCheckpointError`；恢复时自动回退到次新的有效检查点，全部损坏则从头训练。

## 一致性保证与容差

恢复路径与不中断路径执行**完全相同的浮点运算序列**（同一批数据、同一 RNG 流、同一动量状态），
因此容差取 **0（逐位相等）**，比「明确容差内一致」更强。测试用 `np.testing.assert_array_equal` 断言。

## 目录结构

```
checkpoint_resume/
  config.py       # TrainConfig：一次运行的全部超参数
  data.py         # 合成回归数据 + 带游标的 BatchLoader
  model.py        # 线性模型 y = Xw + b，MSE 损失与梯度
  optimizer.py    # 带动量的 SGD（动量缓冲即优化器状态）
  checkpoint.py   # 保存/校验/加载/回退/裁剪，CorruptCheckpointError
  trainer.py      # 训练循环，组装全部可恢复状态
  server.py       # 标准库 http.server 的 JSON API（无外部依赖）
tests/            # 31 个自动化测试
scripts/demo_resume.py   # 中断-恢复对比演示
examples/requests.sh     # API 请求样例（curl）
```

## 快速开始

```bash
pip install -r requirements.txt   # 仅 numpy 与 pytest

# 运行测试
python3 -m pytest tests/ -q

# 演示：第 17 步中断，从检查点恢复训到第 40 步，与不中断基准对比
python3 scripts/demo_resume.py 17 40

# 启动本地服务（默认 127.0.0.1:8377）
python3 -m checkpoint_resume.server
```

## API 请求样例

```bash
CK=/tmp/ckpts
# 从头训练 10 步
curl -s -X POST http://127.0.0.1:8377/train -H 'Content-Type: application/json' \
  -d "{\"config\": {\"checkpoint_dir\": \"$CK\"}, \"steps\": 10, \"resume\": false}"

# 进程重启后断点续训 15 步（自动找最新有效检查点）
curl -s -X POST http://127.0.0.1:8377/train -H 'Content-Type: application/json' \
  -d "{\"config\": {\"checkpoint_dir\": \"$CK\"}, \"steps\": 15, \"resume\": true}"

curl -s http://127.0.0.1:8377/status         # 当前训练器状态
curl -s http://127.0.0.1:8377/checkpoints    # 列出检查点及完整性
curl -s -X POST http://127.0.0.1:8377/verify -H 'Content-Type: application/json' \
  -d "{\"path\": \"$CK/step_00000025.ckpt.npz\"}"   # 校验单个检查点
```

完整可执行样例见 [examples/requests.sh](examples/requests.sh)。

## 实测记录（本机实际运行，非预期值）

环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1，Linux 6.8.0-90-generic。

### 自动化测试

```
$ python3 -m pytest tests/ -q
...............................                                          [100%]
31 passed in 2.50s
```

未通过项：无。

覆盖要点：
- `tests/test_resume_consistency.py` — 在第 1/5/15/16/17/23/32/33/39 步中断（含 epoch 边界 16、32），
  恢复后最终参数与损失和不中断基准**逐位相等**；恢复后消费的批次序列哈希完全一致（不重复、不遗漏、不乱序）。
- `tests/test_cursor_boundary.py` — 检查点恰好落在批次末尾（`next_batch == n_batches`）时，
  恢复后正确进入下一 epoch 的第一个批次。
- `tests/test_checkpoint_integrity.py` — 字节翻转/垃圾内容/缺校验文件均被检出；
  最新检查点损坏时回退到次新有效检查点；全部损坏则从头训练；配置不匹配拒绝恢复；
  检查点必须包含优化器/RNG/游标（只存权重的反向用例证明轨迹会偏离）。
- `tests/test_server.py` — 通过 HTTP API 训练→中断→恢复，结果与不中断基准一致；损坏检查点被 `/verify` 检出。

### 演示脚本（不同批次中断）

```
$ python3 scripts/demo_resume.py 17 40     # epoch 中途
[对比] max|Δw|=0.000e+00  max|Δb|=0.000e+00  |Δloss|=0.000e+00
[损失] 不中断=0.0816387073  恢复=0.0816387073
[结论] 逐位一致 OK

$ python3 scripts/demo_resume.py 16 40     # 恰好在批次末尾（epoch 边界）
[对比] max|Δw|=0.000e+00  max|Δb|=0.000e+00  |Δloss|=0.000e+00
[结论] 逐位一致 OK

$ python3 scripts/demo_resume.py 32 40     # 第二个 epoch 边界
[对比] max|Δw|=0.000e+00  max|Δb|=0.000e+00  |Δloss|=0.000e+00
[结论] 逐位一致 OK
```

### 服务端到端（curl 实测摘要）

- `POST /train {steps:10, resume:false}` → `global_step=10`，保存 `step_00000010.ckpt.npz`
- `POST /train {steps:15, resume:true}` → 从 step 10 恢复，`global_step=25`，`full_loss=0.09085591271187142`
- 翻转 `step_00000025` 一个字节后 `POST /verify` → `{"valid": false, "error": "SHA-256 校验失败…"}`
- 再次 `POST /train {resume:true}` → 自动跳过损坏的 step 25，回退到 `step_00000020.ckpt.npz` 恢复

## 已知限制

- 单进程同步训练（演示规模足够，非分布式）。
- 恢复一致性依赖「同一 config（含 seed）」；改超参即视为新运行，恢复时会被配置校验拒绝。
- 检查点格式版本化（`checkpoint_version`），跨版本不兼容时拒绝加载而非静默误用。
