# 运行记录（RUN LOG）

本文件如实记录实际执行的命令、关键结果以及开发过程中**曾经失败**的用例与修复。
环境：Linux 6.8、Python 3.12.3、NumPy 2.5.3、pytest 9.1.1（均为本机已有，未联网安装任何依赖）。

## 1. 生成合成制品

```bash
python3 scripts/seed_artifacts.py --root ./artifacts
```

结果：创建 `v1`、`v2`、`v3`（有效）与 `v-bad-warmup`（预热失败夹具），
每个版本 6 个文件（4 个 `.npy` + `manifest.json` + `manifest.json.sha256`）。
生成确定性：同一组权重写两次，所有文件逐字节相同
（见 `test_manifest.py::test_artifact_writing_is_bit_reproducible`）。

## 2. 自动化测试

### 2.1 第一轮（暴露真实缺陷）

命令：`python3 -m pytest`

结果：**14 failed, 11 passed, 21 errors**。失败指向两个真实代码缺陷：

1. **dtype 比较写错类型**：代码用 `str(np.float32)`，其值是
   `"<class 'numpy.float32'>"`，而清单里是 `"float32"`，导致所有合法制品都报
   "manifest dtype != expected"。
   修复：改为与 `np.dtype(expected_dtype).name`（即 `"float32"`）比较。
2. **签名校验顺序错误**：`model_kind` 等语义字段在**校验签名之前**被检查，
   未重新签名的篡改会先命中语义错误（`ManifestValidationError`）而非完整性错误。
   修复：先验证清单字节 SHA-256，再做签名字段的语义校验。

另外在代码评审自查中，预防性修复了 `switch_to` no-op 分支的一个
**不可重入锁自死锁**（在持有 `_ref_lock` 时调用再次获取该锁的方法），改为锁内内联处理。

### 2.2 第二轮（4 处测试自身问题）

命令：`python3 -m pytest` → **4 failed, 42 passed**。

- 三个张量篡改用例只改了 `.npy`，会先在 **checksum** 关卡被拦截，到不了
  shape/dtype/非有限值关卡。修正测试：把新哈希写回清单并重新签名，从而能独立
  触发后续关卡（源代码关卡逻辑本身正确）。
- 批量与单条推理有约 `2.2e-8` 的 float32 kernel 数值差（1 ULP），断言改用
  `rtol=1e-6, atol=1e-6`（被测数值正确性不变）。

### 2.3 第三轮（又一个真实健壮性缺陷）

补充畸形清单/HTTP 边界测试后：`3 failed, 75 passed`。

- **真实缺陷**：`int(digest, 16)` 位于 try 块之外，非十六进制摘要会让未捕获的
  `ValueError` 逃逸出解析器。已将其移入 try，统一转为
  `ManifestValidationError("entry malformed")`。
- 两处 HTTP 行为补强：predict 的 409 响应也带回 `still_serving`；请求体过大
  (413) 时先排空请求体并关闭连接，避免连接被中途拆除导致客户端看到
  `Connection reset`。

### 2.4 最终结果

命令：`python3 -m pytest --cov=model_switch --cov-report=term-missing`

```
Name                           Stmts   Miss  Cover   Missing
------------------------------------------------------------
src/model_switch/__init__.py       5      0   100%
src/model_switch/app.py          136      4    97%
src/model_switch/loader.py       116      5    96%
src/model_switch/manager.py      144      2    99%
src/model_switch/manifest.py     146      1    99%
src/model_switch/model.py         47      4    91%
------------------------------------------------------------
TOTAL                            602     16    97%
78 passed
```

稳定性：连续多轮（共 8 次以上，含 6 预测线程/多切换的并发用例）全部
`78 passed`，未观察到并发偶发失败。覆盖率 97%，满足 ≥80% 要求。

> 未覆盖行均为防御性分支：`LoadedModel` 重复 dispose/acquire 的异常保护、
> HTTP 客户端中途断连的写响应容错、以及 model 上不经过管理器直接调用的少量保护分支。

静态检查：本机未安装 `ruff`/`mypy`（未联网安装）；所有源文件均通过
`python3 -m py_compile`。

## 3. 真实服务端到端验证（HTTP）

启动：`python3 scripts/serve.py --root ./artifacts --initial v1 --port 8000`

实际观测到的关键响应：

- 预测 v1 → `logits=[0.32164, 0.31446]`，`generation=1`。
- 切换 v1→v2：HTTP 200，`replaced="v1"`，`retired_refcount=0`，加载阶段
  `manifest → tensor:W1/b1/W2/b2 → construct → warmup` 全部成功，
  `bytes_verified=744`，`elapsed_ms≈3.1`。
- 同一输入在 v2 上 → `logits=[0.20249, 0.02908]`，与 v1 明显不同，证明确实换了权重。
- 切换到 `v-bad-warmup`：**HTTP 409**，
  `error.code="WarmupValidationError"`，错误体给出
  `still_serving="v2"`；随后预测仍返回 `version="v2", generation=2`
  —— **预热失败自动回滚**。
- 切换到不存在版本 `ghost`：**409** `ArtifactNotFoundError`，仍服务 v2。

### 3.1 完整性校验实测（真实篡改文件）

- 将 `artifacts/v3/W1.npy` 末字节异或 0xFF 后切换 v3：
  **409 `ArtifactIntegrityError`**（checksum mismatch，给出期望/实际摘要前缀），
  状态仍为 v2；恢复文件后同一切换返回 200、成功装载 v3。
- 将 `artifacts/v2/manifest.json` 的 `model_kind` 改写但**不重新签名**后切换：
  **409 `ArtifactIntegrityError`**（manifest checksum mismatch），活动版本不受影响。

> 操作后所有被篡改文件均已从备份恢复，仓库回到干净状态。

### 3.2 在途请求 + 并发切换（可观测脚本）

命令：`python3 scripts/demo_concurrency.py --root ./artifacts`

实际输出要点：

```
[in-flight] request started on v1, holding it open ...
[concurrent] second switch rejected fast: SwitchBusyError (another switch is still in progress after 0.1s)
[switch] v1 -> v2 (gen 2), retired v1 refcount=1
[state] active=v2 gen=2 retired=[('v1', 1, False)]
[in-flight] held lease still computes as v1: [0.10019391, -0.12408912]
[new req]  new request is served by v2:       [-0.24809209, 0.49227500]
[release] after lease ended, retired=[] (v1 disposed)
ALL CONCURRENCY ASSERTIONS PASSED
```

即：在途请求跨越切换仍得到 v1 结果；旧 v1 在引用计数为 1 期间不释放；租约结束
引用归零后立即 dispose；并发的第二个切换快速失败且不污染状态。

### 3.3 HTTP 并发风暴

8 个客户端线程持续随机预测，同时串行发起 `v3→v1→v2→v3→v1→v2` 共 6 次切换。
每条预测都用**独立的参考前向**按其声明版本复核：

```
versions observed by clients: ['v1', 'v2', 'v3']
violations: NONE
final active: v2 gen 8
retired: []
HTTP STORM PASSED
```

没有任何一条请求出现非有限输出或"半装载/混合权重"（若张量来自不一致的半成品，
不可能与任一完整版本的参考输出在容差内一致）。

## 4. 已知限制 / 未做项

- 不做前端（按要求）。
- 仅本地、纯 NumPy；不下载外部模型或数据。
- HTTP 服务为演示/验收用途，采用标准库 `ThreadingHTTPServer`，未加鉴权/TLS/限流，
  不应直接暴露到不可信网络。
- 清单的"签名"是 SHA-256 完整性校验（演示防篡改），不是非对称密码学签名；
  生产场景应换成带密钥的签名验证。
- 未联网安装 ruff/mypy，故未运行静态检查（已做字节码编译与 97% 测试覆盖）。
