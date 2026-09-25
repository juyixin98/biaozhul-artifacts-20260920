# 模型制品原子切换服务（model-switch）

纯后端的**本地机器学习基础设施服务**：用 Python + NumPy 实现模型版本的
**装载 → 校验 → 预热 → 原子切换**，并保证：

- 在途请求继续使用它开始时拿到的**旧版本**；
- 旧版本的 NumPy 权重缓存在**引用归零后才释放**；
- 任何请求都**不可能看到半装载（half-loaded）的权重**；
- 预热失败 / 校验失败时**自动回滚**，旧版本继续服务；
- 并发切换被串行化，竞争失败快速返回明确错误，不破坏状态。

不下载任何外部模型或数据集：模型是一个直接用 NumPy 写的小型 MLP，权重由
"按版本号播种" 的随机数确定性生成，因此整个制品仓库**逐比特可复现**，并且每个版本
的预热黄金输出可在装载时逐值核对。

## 目录结构

```
.
├── pyproject.toml
├── README.md
├── src/model_switch/
│   ├── errors.py      # 异常体系
│   ├── model.py       # 小型 MLP + 按版本确定性生成权重 + 独立参考前向
│   ├── manifest.py    # 制品布局、清单签名(SHA-256)、解析与结构校验
│   ├── loader.py      # 校验(checksum/shape/dtype/有限性) + 预热；引用计数句柄
│   ├── manager.py     # 租约/引用计数、原子切换、退役延迟释放、回滚
│   └── app.py         # 纯标准库 ThreadingHTTPServer 的 HTTP 接口
├── scripts/
│   ├── seed_artifacts.py   # 生成可复现合成制品（含预热失败夹具）
│   ├── serve.py            # 启动 HTTP 服务
│   └── demo_concurrency.py # 可观测的并发/在途/延迟释放演示
├── tests/             # 78 个 pytest 用例（单元 + 并发 + HTTP 端到端）
└── artifacts/         # 运行 seed 后生成（已被 .gitignore 忽略）
```

## 制品格式

每个版本一个目录：

```
artifacts/<version>/
├── manifest.json        # 模型种类、各张量 shape/dtype/字节数/sha256、预热黄金样例
├── manifest.json.sha256 # 对清单原文的独立 SHA-256 签名
├── W1.npy  b1.npy  W2.npy  b2.npy
```

装载时依次经过四道关卡，任一失败都不会触碰当前服务槽位：

1. **清单签名**：先核对 `manifest.json` 字节的 SHA-256（任何篡改且无签名密钥
   都在此被拒，而不会伪装成普通字段校验错误）；
2. **张量校验**：每个 `.npy` 的 SHA-256、字节数、shape、dtype、C 连续、值有限性；
3. **构造**：防御性拷贝权重，生成候选模型（私有对象，尚未发布）；
4. **预热**：用清单内签名保护的固定输入跑一次前向，与黄金 logits 在
   `rtol=1e-5, atol=1e-6` 内逐值比对，并要求输出有限。

## 快速开始

需要 Python ≥ 3.10 与 NumPy（开发环境：Python 3.12.3 / NumPy 2.5.3）。

```bash
# 1) 生成可复现的合成制品仓库（v1/v2/v3 + 预热失败夹具 v-bad-warmup）
python3 scripts/seed_artifacts.py --root ./artifacts

# 2) 起服务，启动前先装载并预热 v1
python3 scripts/serve.py --root ./artifacts --initial v1 --port 8000

# 3) 另一终端：预测 / 切换 / 查状态
curl -s localhost:8000/status
curl -s -X POST localhost:8000/predict -H 'Content-Type: application/json' \
  -d '{"input":[0.1,-0.2,0.3,-0.4]}'
curl -s -X POST localhost:8000/switch  -H 'Content-Type: application/json' -d '{"version":"v2"}'
```

完整请求样例见 [`examples/requests.sh`](examples/requests.sh)。

## HTTP 接口

统一响应信封：`{"success": bool, "data": object|null, "error": object|null}`。

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 存活探针，永不 500 |
| `GET /status` | 当前版本、代号 generation、各引用计数、退役队列、事件历史 |
| `GET /versions` | 仓库中存在的版本 |
| `POST /switch` | `{"version":"v2"}`：装载+校验+预热+原子激活；失败返回 409 并在 `error.still_serving` 中给出继续服务的版本 |
| `POST /predict` | `{"input":[4 个 float]}` 或 `N×4` 批量，返回 `logits` 及服务它的 `version`/`generation` |

## 核心机制如何保证安全切换

- **租约（lease）+ 引用计数**：每个请求在入口对当前活动模型加一次引用并记录
  generation，整个请求期间持有；结束释放。切换不会改动已被钉住的对象。
- **先备后换**：候选模型的全部磁盘 I/O、校验、预热都在私有对象上完成；活动槽位
  的更新只是锁内一次指针交换（不包含任何 I/O），因此不存在"换了一半"的窗口。
- **延迟释放**：切换后管理器放弃自己对旧模型的那一个引用；只要还有在途请求，
  旧模型进入退役集合保留权重，最后一个租约释放时才 `dispose()` 清空缓冲区。
- **失败回滚**：装载/预热抛错时活动槽位根本未被修改；候选私有对象随即释放，
  旧版本与 generation 均不变。
- **并发切换**：整段切换由一把锁串行化；在超时时间内拿不到锁的第二个切换者收到
  `SwitchBusyError`，绝不静默排队或污染状态。

## 验收场景与对应测试

| 验收点 | 测试 |
|---|---|
| 预热失败夹具 | `test_loader.py::test_warmup_*`、`test_manager.py::test_warmup_failure_*`、`test_app.py::test_switch_warmup_failure_rolls_back_via_http` |
| 并发切换 | `test_manager.py::test_concurrent_switch_second_caller_gets_busy_or_waits`、`test_many_serialized_concurrent_switches_all_succeed` |
| 在途请求读取中回滚/钉住旧版 | `test_inflight_request_keeps_old_version_until_lease_released`、`test_retirement_chain_with_overlapping_leases` |
| 绝不暴露半装载权重 | `test_predictions_during_switch_always_match_a_complete_version`、`test_app.py::test_concurrent_predictions_during_http_switch_never_tear` |
| 旧资源引用归零后释放 | `test_old_version_freed_immediately_without_inflight`、`test_retired_model_used_after_release_is_dead` |

并发/在途行为另有不依赖测试框架的可观测脚本：

```bash
python3 scripts/demo_concurrency.py --root ./artifacts
```

## 运行测试

```bash
python3 -m pytest                                   # 全部用例
python3 -m pytest --cov=model_switch --cov-report=term-missing
```

实际执行的命令、原始输出与结论（含开发过程中曾失败、随后修复的如实记录）见
[`docs/RUN_LOG.md`](docs/RUN_LOG.md)。
