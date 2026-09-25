# 实际运行记录（RUN REPORT）

- 日期：2026-09-25
- 机器：Linux 6.8.0-90-generic (x86_64)
- 运行时：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1
- 工作目录：`/home/admin/Downloads/biaozhul/opp146/a`
- 网络：未下载任何外部模型/权重/数据；依赖仅 NumPy（环境已具备）。

本文件如实记录实际执行的命令、结果，以及开发过程中**真实出现并修复的失败项**。

---

## 1. 自动化测试

命令：

```bash
python3 -m pytest -q
python3 -m pytest --cov=checkpoint_service --cov-report=term -q
```

结果（最终一次干净运行）：

```
74 passed in 7.47s
TOTAL  726 stmts  53 miss  93%
```

- 74 个测试全部通过；0 失败、0 跳过。
- 行覆盖率 93%（≥ 80% 要求）。各核心模块：`model/optimizer/synthetic/errors/compare/config` 100%，
  `trainer 97%`、`cursor 98%`、`service 93%`、`checkpoint 84%`、`api 86%`、`__main__ 90%`。
- 测试分类：`@pytest.mark.unit`（单元）与 `@pytest.mark.integration`（集成/端到端）。

## 2. 验收对比：不同批次中断 vs 不中断

命令：

```bash
python scripts/run_demo.py --out ./demo_out
```

配置：512 样本 × 8 特征，batch=32，4 epoch（共 64 步，16 步/epoch），每 8 步提交一次检查点，
动量 SGD（lr=0.05, momentum=0.9, l2=1e-4），train_seed=42。容差 W/b 与 loss 均为 1e-6。

中断点覆盖：第 1 步（极早、区间中部）、第 7 步（提交前一步，强制重放 1–7）、
第 8 步（恰好提交边界）、第 16 步（**epoch 边界 / 批次末尾游标**）、第 17 步（越过边界）、
第 63 步（最后一步前）、以及三次中断 `8,24,35`（含区间中部）。

结果（退出码 0）：

| 中断批次 | W 最大差 | b 差 | 全量损失差 | 逐步损失差 | 结论 |
|---|---|---|---|---|---|
| @1       | 0 | 0 | 0 | 0 | PASS |
| @7       | 0 | 0 | 0 | 0 | PASS |
| @8       | 0 | 0 | 0 | 0 | PASS |
| @16      | 0 | 0 | 0 | 0 | PASS |
| @17      | 0 | 0 | 0 | 0 | PASS |
| @63      | 0 | 0 | 0 | 0 | PASS |
| @8,24,35 | 0 | 0 | 0 | 0 | PASS |

参考运行最终全量损失：`0.02703112`。机器可读报告：`demo_out/report.json`。

**说明**：差异实测全部为 `0.0`（逐位一致，优于 1e-6 容差）。原因是检查点完整保存了
参数、优化器动量、RNG BitGenerator 状态与数据游标，恢复后 float32 运算按完全相同的顺序重放；
容差作为跨平台浮点次序的明确余量保留。

## 3. 真实进程杀死 → 新进程重启（跨进程）

命令：

```bash
python scripts/run_crash_demo.py --crash-at 13
```

结果（退出码 0）：

```
[worker] HARD EXIT at step 13 (mid-interval)
worker 1 exit code: 137 (137 = killed, as expected)
[worker] resumed at step 8
[worker] finished at step 64 eval_loss=0.02703112
[reference] finished at step 64 eval_loss=0.02703112
W_max_abs_diff = 0.0, b_abs_diff = 0.0,
eval_loss_abs_diff = 0.0, history_max_abs_diff = 0.0
RESULT: PASS
```

worker 在第 13 步以 `os._exit(137)` 硬退出（等价 SIGKILL，无 Python 清理、无额外落盘）；
因检查点每 8 步提交，新进程从**第 8 步**恢复并确定性重放第 9–13 步，最终与独立参考运行逐位一致。
机器可读结果：`crash_demo_out/crash_report.json`。

## 4. HTTP API 实际联调（curl）

命令：

```bash
python -m checkpoint_service --runs-dir /tmp/cp_runs serve --port 8099
bash examples/http_requests.sh     # 另一终端
```

实测结果（另见 `examples/sample_output.txt`，由脚本真实运行留存）：

- `GET /health` → 200 `{"status":"ok"}`
- 创建 run → 201，step-0 检查点；`train stop_after=3` 后 `status` 显示进度已持久化在 step 3；
  继续训练到完成 `global_step=32, finished=true`。
- 检查点检视：`has_model / has_optimizer_state / has_rng_state / has_cursor` 全为 true。
- 对 payload 翻转一个字节后 `GET status` → **422 `CheckpointCorruptError: checkpoint checksum mismatch`**。
- 重复创建 → **409 `RunExistsError`**；未知 run → **404 `RunNotFoundError`**；
  `batch_size=0` → **400 `InvalidRequestError: batch_size must be positive`**。
- `GET /runs` 中被损坏的 run 标注 `"status": "corrupt: checkpoint checksum mismatch"`。

## 5. 损坏检查点 / 只存权重 验收

`tests/test_checkpoint.py` 中以真实落盘方式构造并断言：

- payload 位翻转 → 拒绝（校验和不符）；
- payload 截断 20 字节（模拟撕裂写）→ 拒绝；
- meta 写成非法 JSON / 篡改 meta 的 sha256 → 拒绝；
- 仅 payload 或仅 meta 存在 → 视为无已提交检查点；
- **只存权重**（仅 `format_version + model`）→ `MissingCheckpointFieldError`，
  信息为 "weights-only checkpoints are rejected"；
- 分别删除 `optimizer` / `rng_state` / `cursor` 任一关键部分 → 拒绝；
- 优化器缺少速度 `vW/vb` → 拒绝；格式版本号未知 → 拒绝；游标置换不合法 → 拒绝；
- 写一个非法状态**不会覆盖**此前已提交的完好检查点（先校验后写盘）。

CLI 端实测（植入只含权重的检查点）：

```json
{"error": "MissingCheckpointFieldError",
 "detail": "checkpoint is not a complete training state (missing: ['config','cursor',
           'data_fingerprint','loss_history','optimizer','rng_state']); weights-only checkpoints are rejected"}
```

## 6. 开发过程中出现过并已修复的失败项（如实记录）

首次跑 `pytest` 时有 5 个失败，均为真实问题，已逐一修复并复测通过：

1. **`FileNotFoundError`（检查点目录不存在）**——测试/脚本直接在尚未创建的 run 子目录保存。
   修复：`save_checkpoint` 在原子写之前 `os.makedirs(run_dir, exist_ok=True)`。

2. **优雅停止后进度丢失（语义缺陷，非仅测试问题）**——`service.train(stop_after=3)` 若未到
   `checkpoint_every` 就不提交，下一次调用从 step 0 恢复（测试中表现为 `3 == 6` 断言失败）。
   修复：在训练器引入 `commit_at_end` 区分两种语义——API 的优雅暂停返回前提交本次进度；
   模拟崩溃/真实杀进程路径**不提交**，从而保留并验证"从上次提交点确定性重放"。

3. **ndarray 的 dict 直接 `==` 比较**——`assert loaded["cursor"] == state["cursor"]` 触发
   "truth value of an array is ambiguous"。修复：对置换序用 `assert_array_equal`、标量字段逐一比较。

4. **小配置下损失未收敛到过严阈值**——18 步训练后损失 0.54，断言 `< 0.1` 失败（阈值脱离配置）。
   修复：该收敛性测试改用更充分的配置（256 样本、12 epoch），断言 `< 0.05`，实测通过。

5. **真实 SIGKILL 演示 worker 报 `OSError: [Errno 22]`（os.fsync 管道）**——
   `os.fsync(stdout)` 在 stdout 是被捕获管道时非法，导致 worker 退出码为 1 而非 137。
   修复：删除该 fsync，保留 `flush=True`；重跑得到预期退出码 137，验收 PASS。

此外，CLI 初版对 `TrainConfig` 的 `ValueError` 与 `CheckpointError` 未做整洁处理（打印 traceback），
已改为输出 JSON 错误并返回退出码 1，并补充了 CLI 测试。

## 7. 当前未通过项 / 已知限制

- 截至最后一次干净运行：**无失败测试、无未通过验收项**。
- 已知限制（非缺陷，属范围声明）：
  - pickle 仅用于本地、经 SHA-256 校验的状态文件；服务不接受用户上传的任意检查点。
  - 并发保护为单进程内的每-run 锁；多进程同时写同一 run 依赖原子 rename（最后提交者生效），
    未引入跨进程文件锁（本地单服务场景不需要）。
  - 仅 stdlib `http.server`，面向本地基础设施用途，未做 TLS/鉴权（不对外暴露）。
