# modelswitch — 模型制品原子切换服务

纯后端本地机器学习基础设施服务（Python + NumPy，无外部模型/数据下载）。
核心能力：**本地模型版本的装载、校验、预热与原子切换**——在途请求继续使用旧版本，
旧版本资源在引用计数归零后释放；任何请求都不会看到装载到一半的权重。

所有模型制品均由固定种子的合成数据在本地生成，完全可复现。

## 设计

```
装载流水线（load_candidate，全部在“候选区”完成，不触碰现役版本）：
  read_manifest → verify_checksum → load_weights → validate → build_model → warmup → ready
        │ 任一环失败抛 ArtifactError 子类，现役版本不受影响 │
        ▼
  ModelVersion（不可变、已预热）→ registry.switch() 一次指针交换完成原子切换

注册表（ModelRegistry）：
  ACTIVE   当前服务版本，新请求在此取租约（Lease）
  STANDBY  上一个 ACTIVE，保留在内存中，rollback() 瞬时切回
  RETIRED  更早的版本；引用计数归零时 close() 释放权重
```

关键不变量：

- **原子性**：`switch()` / `rollback()` 只是在同一把锁下交换槽位指针；
  候选版本在装载完成前对注册表不可见，请求永远拿到完整装载的某个版本。
- **在途保护**：请求通过 `registry.acquire()` 持有租约，租约存续期间
  该版本资源绝不释放；请求全程使用同一个版本。
- **延迟释放**：RETIRED 槽位在引用计数归零（最后一个在途请求释放租约）时
  才 `close()` 释放权重数组。
- **预热门禁**：校验只做结构检查（sha256、形状、dtype）；数值可用性由预热
  判定——用种子化合成输入跑多批推理，输出必须有限且概率和为 1，
  否则抛 `WarmupError`，切换被拒绝。

## 目录结构

```
modelswitch/
  artifacts.py   合成制品生成（manifest.json + weights.npz，sha256 校验）
  model.py       确定性线性 softmax 模型（权重不可变，predict 线程安全）
  loader.py      分阶段装载流水线：校验、预热、ModelVersion
  registry.py    原子切换注册表：租约、引用计数、退役释放、回滚
  server.py      纯标准库 HTTP 服务
scripts/make_artifacts.py   生成可复现演示制品（含预热失败/校验失败夹具）
examples/requests.sh        请求样例（curl）
tests/                      pytest 自动化测试（21 项）
```

## 快速开始

```bash
pip install -r requirements.txt

# 生成合成制品：v1、v2（健康）、v3-bad-warmup（预热失败）、v4-bad-checksum（校验失败）
python scripts/make_artifacts.py

# 启动服务（初始激活 v1）
python -m modelswitch.server --artifact artifacts/v1 --port 8765

# 另开终端跑请求样例
bash examples/requests.sh

# 运行测试
python -m pytest tests/ -v
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| GET  | `/version` | 当前激活版本 |
| POST | `/predict` | `{"x": [[...8 维...]]}` → `{"version", "y"}` |
| POST | `/admin/switch` | `{"path": "artifacts/v2"}` 装载+预热+原子切换；失败返回 409 且现役版本不变 |
| POST | `/admin/rollback` | 切回上一版本 |
| GET  | `/admin/stats` | 各槽位版本、状态、在途引用数 |

## 请求样例（实测输出，见 RUNLOG.md）

```bash
curl -s -X POST http://127.0.0.1:8765/predict \
  -H 'Content-Type: application/json' \
  -d '{"x": [[0.1, -0.2, 0.3, 0.0, 0.5, -0.1, 0.2, 0.4]]}'
# {"version": "v1", "y": [[0.3092..., 0.3561..., 0.3346...]]}

curl -s -X POST http://127.0.0.1:8765/admin/switch \
  -H 'Content-Type: application/json' -d '{"path": "artifacts/v3-bad-warmup"}'
# 409 {"error": "WarmupError: ... non-finite outputs ...", "active_version": "v2"}
```

## 验收夹具与测试对应

| 验收项 | 测试 |
|--------|------|
| 预热失败，现役版本不受影响 | `tests/test_loader.py::test_numerically_unusable_weights_fail_warmup`、`tests/test_registry.py::test_failed_candidate_never_replaces_active`、`tests/test_server.py::test_switch_and_rollback_over_http` |
| 并发切换 + 读取，无半装载权重 | `tests/test_atomicity.py::test_concurrent_switch_and_read_never_sees_partial_model`（装载各阶段注入延迟，4 读线程 × 12 次切换，逐次校验输出与某一完整版本逐位一致） |
| 读取中回滚 | `tests/test_rollback.py::test_rollback_during_inflight_read`（在途请求持租约跨 switch→rollback，全程由 v1 服务，资源不提前释放） |
| 引用归零后释放 | `tests/test_registry.py::test_retired_version_released_only_when_refcount_zero` |
| 校验失败（篡改权重） | `tests/test_loader.py::test_tampered_weights_fail_checksum` |
