# adam-shard — 本地多进程 Adam 状态分片与跨进程数恢复

一套纯本地的多模块后端（Python 3.10+ / NumPy / FastAPI），实现 **Adam 优化器状态按进程分片保存**，
并支持 **用不同的进程数恢复后继续训练**。所有数据来自本地确定性合成夹具，无外部账号、无真实业务数据。

核心能力：

- 参数身份用**稳定名称 + 形状**（`layers.0.weight` / `(5,3)`），绝不依赖遍历序号；
- 一阶矩 `m`、二阶矩 `v`、步数 `step` 与参数切片严格对应；
- 重分片支持**不均匀尾片**（余数落在最后一个 rank），清单不完整一律拒绝加载；
- **模型与优化器检查点属于同一次原子提交**（单一 `commit_id`，暂存目录 + 一次 rename 发布）；
- 恢复后再走一步，与一个**从未经过分片核心的独立单进程参考实现**逐元素一致（float64 下 ~1e-17）。

---

## 1. 目录结构

```
src/adam_shard/
  tensor_types.py     # 张量身份 TensorId(name, shape)、规范化 dtype、SHA-256 摘要
  graph.py            # 真实计算图：tanh MLP 的前向 + 手写反向传播（非硬编码演示）
  layout.py           # 按稳定名称排序的扁平布局 flatten/unflatten + 清单
  sharding.py         # 不均匀尾片分片计划 + 2↔3 重分片索引映射
  adam.py             # 分片侧 Adam（切片更新，函数式、不就地修改）
  reference_adam.py   # 独立参考 Adam（按名称张量、另一条代码路径，测试的预言机）
  training_state.py   # 原子提交、清单/摘要/形状校验、重分片加载、rank 视图
  worker.py           # rank 工作进程：各持一个 m/v 分片（spawn 启动）
  coordinator.py      # 协调器：拉起进程、汇聚切片、一次提交落盘
  verification.py     # 有限差分梯度核验 + 分片 vs 参考逐元素比对（pass/fail/uncertain）
  fixtures.py         # 配置与确定性合成数据集（固定种子 teacher 网络 + 噪声）
  service.py          # 应用服务层（训练 / 校验 / 恢复一致性）
  api.py              # FastAPI：/train /validate /verify-parity /commits /health
  __main__.py         # 命令行入口
  errors.py           # 带稳定 category 的失败类型
config/default.json   # 启动配置
data/sample.npz       # 随仓附带的样例数据（也可确定性再生）
scripts/              # start-api.sh、demo.sh
tests/                # 独立单元 / 多进程集成 / HTTP / CLI 测试
```

默认模型是 `3→5→4→3` 的 tanh MLP，共 **59** 个参数。选 59 是因为它对 2 和 3 都除不尽：

```
59 = 2·29 + 1   →  2 进程分片大小 [29, 30]
59 = 3·19 + 2   →  3 进程分片大小 [19, 19, 21]
```

从而**主测试场景 2→3 的两侧都带不均匀尾片**。

---

## 2. 安装

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 或可编辑安装（含测试依赖）：pip install -e ".[test]"
export PYTHONPATH="$PWD/src"   # 未做可编辑安装时让 import / python -m 生效
```

依赖仅需 `numpy`、`fastapi`、`uvicorn`；测试需要 `pytest`、`httpx`。

生成样例数据：

```bash
python3 -m adam_shard --config config/default.json init-data
# wrote .../data/sample.npz
```

---

## 3. 一分钟快速上手（CLI）

### 3.1 用 2 个进程训练并提交

```bash
$ python3 -m adam_shard --config config/default.json train --world-size 2 --steps 6
{
  "commit_id": "req-6a934a84d51f-w2-s6-ab12cd",
  "step": 6,
  "world_size": 2
}
```

日志里可看到每个 rank 的 pid、拥有的扁平区间与汇聚过程（均带 `request_id`）：

```
[req=...] starting world_size=2 mode=fresh commit=None total_numel=59 sizes=(29, 30)
[req=...][rank=0] pid=... started world_size=2 owns flat [0,29) of 59
[req=...][rank=1] pid=... started world_size=2 owns flat [29,59) of 59
[req=...] coordinator assembled step=6 global_numel=59 rank_losses=1.51128159, 1.51128159
[req=...] committed model+optim atomically commit=...
```

### 3.2 以 **3** 个进程校验并重分片该提交

```bash
$ python3 -m adam_shard --config config/default.json validate <commit> --target-world-size 3
{
  "ok": true,
  "source_world_size": 2,
  "target_world_size": 3,
  "step": 6,
  "target_shard_sizes": [19, 19, 21],
  "parameters": [ {"name": "layers.0.bias", "shape": [5]}, ... ]
}
```

### 3.3 用 3 进程恢复、再走一步，与独立参考比对（关键结论）

```bash
$ python3 -m adam_shard --config config/default.json verify-parity <commit> --target-world-size 3
{
  "request_id": "req-cli-...",
  "stage": "restore+one_step",
  "version": "1.0.0",
  "location": "var/checkpoints/<commit>",
  "status": "pass",
  "checks": [
    { "name": "one_step_params",   "status": "pass", "detail": { "max_abs_err": 3.47e-18 } },
    { "name": "one_step_moments",  "status": "pass", "detail": { "max_abs_err": 2.78e-17 } },
    { "name": "step_counter_correspondence", "status": "pass",
      "detail": { "restored_step": 6, "observed_step": 7 } },
    { "name": "finite_difference_gradient", "status": "pass",
      "detail": { "probes": 27, "eps": 1e-06, "max_abs_err": 3.28e-10 } },
    { "name": "reshard_uneven_tail", "status": "pass",
      "detail": { "source_shard_sizes": [29, 30], "target_shard_sizes": [19, 19, 21] } }
  ],
  "failures": [],
  "uncertainties": []
}
```

> 结论：**恢复后一步更新与未分片参考实现的最大绝对偏差约 3.5e-18（机器精度）**；
> 梯度反向传播通过中心有限差分核验。命令退出码在 `fail` 时为 1。

一条命令跑完整段流程：`./scripts/demo.sh`。

---

## 4. HTTP 服务

```bash
./scripts/start-api.sh                 # 默认 http://127.0.0.1:8000
# 或：python3 -m uvicorn adam_shard.api:app --host 127.0.0.1 --port 8000
```

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","version":"1.0.0","storage_root":"var/checkpoints"}

# 2 进程训练
curl -s -X POST http://127.0.0.1:8000/train \
  -H 'Content-Type: application/json' \
  -d '{"world_size":2,"steps":3,"request_id":"req-demo"}'

# 校验并重分片到 3
curl -s -X POST http://127.0.0.1:8000/validate \
  -H 'Content-Type: application/json' \
  -d '{"commit_id":"<commit>","target_world_size":3}'

# 3 进程恢复 + 一步一致性
curl -s -X POST http://127.0.0.1:8000/verify-parity \
  -H 'Content-Type: application/json' \
  -d '{"source_commit":"<commit>","target_world_size":3,"request_id":"req-parity"}'
```

每个响应都回带 `request_id`；`verify-parity` 给出 `stage/version/location`，并把
**failures（硬失败）与 uncertainties（接近容差带的不确定结论）分开列出**。
失败统一以 HTTP 422 + 稳定 `category` 返回，例如：

```json
{"detail":{"ok":false,"category":"missing_commit",
  "message":"commit 'nope' not found under var/checkpoints","commit_id":"nope","extra":{}}}
```

---

## 5. 关键设计

### 5.1 身份：名称 + 形状，而非序号

- 扁平向量按**参数名排序**拼接，清单逐条记录 `name / shape / offset / numel`；
- `flatten()` 对字典遍历顺序不敏感（乱序输入产出同一向量），`install()` 按身份校验；
- 把张量装到错误名称/形状的槽位会被直接拒绝，状态不可能按 ordinal 串台。

### 5.2 不均匀尾片与重分片

`ShardPlan.create(total, world)`：每片 `floor(total/world)`，余数全部加到**最后一片**。
`reshard_indices(src, dst)` 给出全局元素区间的交集拷贝表，2↔3 双向都精确覆盖每个元素一次。
恢复时每个 rank 用 `load_rank_view()` **只读取它需要的源分片**（跨边界时读两片）。

### 5.3 模型与优化器同一次提交

一次提交 = 一个目录：`manifest.json` + `model-r*.npy` + `optim-r*.npz(m,v)`。
写入先落 `.stage-<id>/`，校验全部完成后**一次 `os.rename` 原子发布**，并刷新 `LATEST`。
恢复时 `require_same_commit(model_commit, optim_commit)` 强制二者一致，否则 `commit_mismatch`。

### 5.4 完整性与失败分类

清单自含 `manifest_digest`（对清单体的 SHA-256），每个分片含内容摘要。加载时按顺序校验：
清单摘要 → 版本 → 身份/布局 → 清单完整性（rank 恰好 0..N-1、跨度与规范计划一致）
→ 分片文件存在 → 形状 → 内容摘要。失败类型：

| category | 触发条件 |
|---|---|
| `missing_commit` | 提交目录/`LATEST` 不存在 |
| `corrupt_manifest` | 清单 JSON 损坏或其摘要不匹配 |
| `unsupported_version` | 格式版本不符 |
| `incomplete_manifest` | 清单列的 rank 缺失/重复/数量不符/跨度不符 |
| `missing_shard` | 清单声明的分片文件缺失 |
| `shape_mismatch` | 分片实际形状/长度与声明不符 |
| `digest_mismatch` | 内容或 m/v 被篡改、互换 |
| `commit_mismatch` | 模型与优化器来自不同提交，或目录名与清单 commit_id 不符 |
| `layout_mismatch` | 模型/优化器布局描述的参数集合不同 |

测试对**缺分片、错误形状、损坏内容、m/v 互换、损坏清单摘要、清单缺 rank、跨提交配对**
逐一断言了具体 `category`，而不是仅检查“接口能调用”。

### 5.5 参考答案独立产生

- 数值预言机 `reference_adam.ReferenceAdam` 与被测分片核心是**两条独立代码路径**
  （按名称张量、EMA 偏差形式 vs 扁平切片），其本身另用手写教科书公式测试背书；
- 梯度正确性由**中心有限差分**独立核验；
- 集成测试在测试内重新单步计算期望，参考答案不由被测核心生成。

---

## 6. 运行测试

```bash
$ python3 -m pytest
...................................................................  (单元/集成/HTTP/CLI)
78 passed in 7.0s
```

带覆盖率：

```bash
$ python3 -m pytest --cov=adam_shard --cov-report=term-missing
...
TOTAL   1231 stmts   ...   Cover  91%
```

测试地图：

| 文件 | 内容 |
|---|---|
| `tests/test_tensor_types.py` / `test_layout.py` / `test_sharding.py` | 身份、扁平布局、乱序、不均匀尾片、2↔3 映射 |
| `tests/test_adam_math.py` | 切片 Adam vs 手写公式；不可变；形状校验 |
| `tests/test_reference_adam.py` | 独立参考实现 vs 教科书公式（先给预言机背书） |
| `tests/test_graph.py` | 有限差分梯度、名称对齐、下降方向 |
| `tests/test_checkpoint.py` | 往返、2→3、缺分片/错形状/坏摘要/m-v 互换/坏清单/缺 rank/跨提交 |
| `tests/test_integration_mp.py` | **真多进程 2→3 恢复再一步 = 独立参考**；参数重排；各类失败 |
| `tests/test_worker_inproc.py` | 进程内驱动 rank，覆盖跨边界 rank 视图恢复 |
| `tests/test_api.py` | 真实 ASGI 栈下的 HTTP 成功与 422 分类 |
| `tests/test_cli.py` / `test_verification.py` | CLI 全流程；pass/fail/uncertain 分类 |

多进程测试使用 `spawn` 启动方式（Linux/macOS/Windows 均可），CPU 核数不足时进程会分时运行，
不影响正确性；要求 `world_size <= total_numel`（每个 rank 至少一个元素）。

---

## 7. 配置说明（`config/default.json`）

| 字段 | 含义 |
|---|---|
| `storage_root` | 检查点根目录（默认 `var/checkpoints`，已在 `.gitignore`） |
| `seed` | 模型初值与合成数据的固定随机种子 |
| `dtype` | `float64`（强一致性核验建议）或 `float32` |
| `model.dims` | MLP 层维度，改维度即改参数身份/形状与分片划分 |
| `data.n_samples` | 合成样本数 |
| `adam.{lr,beta1,beta2,eps}` | Adam 超参 |
| `train_steps` | 默认训练步数 |
| `api.{host,port}` | 服务监听地址 |
