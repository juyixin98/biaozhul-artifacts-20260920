# mptrainer — 合成小网络的混合精度训练器

核心约束：**主权重（fp32 master）、低精度前向参数（fp16）、优化器状态（fp32 momentum）三者物理分离**。
低精度参数每个 micro-step 从主权重临时派生、用后即弃；优化器只更新 fp32 主权重；
动态损失缩放（dynamic loss scaling）状态机独立保存、随检查点持久化。

技术栈：Python 3.12 · NumPy（计算图与数值）· FastAPI（服务入口）· pytest（测试）。
所有数据均为本地确定性合成夹具（`mptrainer.data.SyntheticBatchSource`），无外部账号或真实业务数据。

## 目录结构

```
configs/default.json        # 默认训练配置（配置层）
src/mptrainer/
  tensors.py                # 张量类型层：dtype 注册、转换、有限性检查
  graph.py                  # 计算图层：MLP 前向/反向（低精度执行）
  optimizer.py              # 优化器层：fp32 主权重上的动量 SGD（返回新对象，不原地改）
  scaler.py                 # 动态损失缩放状态机
  trainer.py                # 训练状态层：步语义、梯度累积、溢出策略
  checkpoint.py             # 检查点：完整训练状态（含 scaler）持久化/恢复
  validation.py             # 数值校验层：守恒/容差/轨迹比较
  runlog.py                 # 结构化 JSONL 运行日志（按 run_id 关联）
  data.py                   # 合成数据夹具（amplify 为受控溢出开关）
  config.py                 # 配置层：校验 + JSON 往返
service/app.py              # FastAPI 服务入口
scripts/demo.py             # 本地演示脚本
tests/                      # 独立测试层（32 个用例）
```

## 步语义（step semantics）

- `micro_step`：每次 `train_step` 调用（每次前向/反向尝试）都 +1。
- `optimizer_step`：仅在一次更新**真正提交**时 +1；学习率调度是它的纯函数
  （`lr = base_lr / (1 + lr_decay * optimizer_step)`），因此被跳过的窗口不会推进也不会破坏 LR 进度。
- **梯度溢出**：当缩放损失或梯度出现 inf/nan，整次更新被跳过——主权重按位守恒
  （测试断言 bitwise equal），缩放因子按 `backoff_factor` 回退，`optimizer_step` 冻结。
- **梯度累积**：一个累积窗口（`accum_steps` 个 micro-step）是原子的。窗口内任一 micro-step
  溢出 → 整个窗口（含已累积的干净梯度）被丢弃，不存在部分提交。
- **检查点**：保存主权重、优化器动量状态、scaler 状态（scale + 增长计数）、步计数器与配置；
  恢复后的运行与不中断运行逐位一致（`tests/test_checkpoint.py` 实证）。

## 错误语义

训练核心：

- 溢出**不是异常**，是正常训练结果：`StepRecord.decision == "skipped_overflow"`，
  `loss=None`，`reason` 字段说明判定依据（丢弃了多少个已累积 micro-step、scale 变化）。
- 配置非法抛 `ConfigError`（如 `accum_steps < 1`、未知 dtype）。
- 检查点格式不支持抛 `ValueError`。

HTTP 服务：

| 状态码 | 含义 |
|--------|------|
| 200    | 成功；溢出通过 step record 的 `decision` 字段表达，**不**映射为错误 |
| 404    | 未知 `run_id`，或该 run 尚无检查点 |
| 409    | `run_id` 已存在 |
| 422    | 配置非法（`ConfigError`）或步进参数非法（`n<1`、`amplify<=0`、`batch_size<1`） |

异常或未知状态不会统一返回成功：服务只对明确合法的请求返回 2xx。

## 运行日志

每次运行写 `logs/<run_id>.jsonl`，每行一条 JSON，包含：`run_id`（运行身份）、
`seq`/`ts`、`event`、`versions`（mptrainer/python/numpy 版本）、以及步级记录的
`micro_step`/`optimizer_step`/`decision`/`scale_before`/`scale_after`/`lr`/`reason`
（进度、计算步骤与判定依据）。测试 `test_run_log_correlates_decision_with_run_and_versions`
直接解析该文件断言上述字段。

## 复现步骤

```bash
pip install -r requirements.txt

# 1. 测试（实际执行并报告结果）
python3 -m pytest tests/ -v

# 2. 本地演示：干净窗口 → 放大输入触发溢出 → 跳步/回退 → 检查点恢复 → fp32 对照
python3 scripts/demo.py

# 3. 服务入口
uvicorn service.app:app --app-dir src --port 8000
#   POST /runs                    创建运行（可带 config）
#   POST /runs/{id}/steps         {"n": 4, "batch_size": 16, "amplify": 1.0e4}
#   GET  /runs/{id}/state         计数器、scale、当前 lr
#   GET  /runs/{id}/log           结构化运行日志
#   POST /runs/{id}/checkpoint    保存检查点（含 scaler 状态）
#   POST /runs/{id}/restore       恢复检查点
```

## 验证设计（测试如何对应约束）

- **受控溢出**：`amplify=1e4` 的放大输入使 fp16 激活/梯度越过 65504 → inf，
  触发 `skipped_overflow`；断言主权重按位守恒、`optimizer_step` 冻结、scale 减半
  （`tests/test_overflow.py`）。
- **对照全精度与不中断运行**：fp16 轨迹对比测试内手写的独立 fp64 参考实现
  （非被测核心生成），20 步内主权重偏差 < 2e-3；检查点恢复后与不中断运行逐位一致
  （`tests/test_vs_fp32_reference.py`、`tests/test_checkpoint.py`）。
- **恢复后的学习率进度**：溢出窗口不推进 LR 调度，恢复后与同 `optimizer_step`
  的干净运行 LR 相等（`tests/test_accumulation.py::test_lr_progress_unchanged_by_poisoned_window`）。
- **累积窗口原子性**：窗口第 2/3 步溢出 → 无部分提交，主权重不变，窗口计数清零
  （`tests/test_accumulation.py`）。
- **独立参考答案**：单次提交、动量累积、窗口均值梯度的期望值均由测试内 fp64 公式
  直接计算（不调用被测核心），另有限差分抽查计算图梯度
  （`tests/test_trainer_numerics.py`）。
