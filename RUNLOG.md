# 运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic，Python 3.12.3，NumPy 2.5.3，pytest 9.1.1。
所有命令在项目根目录执行，未下载任何外部模型或数据。

## 1. 自动化测试

```
$ python3 -m pytest tests/ -v
...
tests/test_atomicity.py::test_concurrent_switch_and_read_never_sees_partial_model PASSED
tests/test_loader.py (6 项) PASSED
tests/test_registry.py (8 项) PASSED
tests/test_rollback.py::test_rollback_during_inflight_read PASSED
tests/test_server.py (5 项) PASSED
=============== 21 passed in 2.56s ===============
```

并发用例单独重复运行 5 次，全部通过（每次约 0.3s）。

### 开发过程中出现并已修复的失败（如实记录）

1. **预热失败夹具最初未触发**（3 个测试失败）：夹具权重缩放系数取 1e300，
   经数值稳定的 softmax（减最大值）后输出仍然有限，预热通过。
   修复：缩放系数改为 1e308，使 logits 在 matmul 中溢出为非有限值，
   预热按预期拒绝；同时在 `predict` 内用 `np.errstate` 抑制预期的
   overflow/invalid 警告。
2. **演示端口冲突**：本机 8080 已被其他服务占用（`OSError: [Errno 98]
   Address already in use`，curl 打到的是别的服务，返回 404）。
   修复：演示与 `examples/requests.sh` 改用 8765 端口。

## 2. 制品生成

```
$ python3 scripts/make_artifacts.py
wrote artifacts/v1
wrote artifacts/v2
wrote artifacts/v3-bad-warmup
wrote artifacts/v4-bad-checksum
```

## 3. 端到端请求样例（实际输出）

```
$ python3 -m modelswitch.server --artifact artifacts/v1 --port 8765 &
$ bash examples/requests.sh

== health ==
{"status": "ok"}
== current version ==
{"version": "v1"}
== predict (served by v1) ==
{"version": "v1", "y": [[0.30924348643744726, 0.35610036658522226, 0.3346561469773304]]}
== switch to v2 (atomic; in-flight requests keep v1) ==
{"version": "v2", "switched": true}
== predict (now served by v2) ==
{"version": "v2", "y": [[0.4120927221209447, 0.3817369773201473, 0.20617030055890795]]}
== switch to artifact that fails warm-up (rejected; v2 stays active) ==
{"error": "WarmupError: warm-up inference 0 produced non-finite outputs (weights are numerically unusable)", "active_version": "v2"}
== switch to artifact with bad checksum (rejected; v2 stays active) ==
{"error": "ChecksumError: checksum mismatch for artifacts/v4-bad-checksum/weights.npz: manifest says d7ce9a348ca1..., file hashes to a0089d742704...", "active_version": "v2"}
== rollback to v1 ==
{"version": "v1", "rolled_back": true}
== registry stats ==
{"active": {"version": "v1", "state": "active", "inflight": 0, "closed": false},
 "standby": {"version": "v2", "state": "standby", "inflight": 0, "closed": false},
 "retired": []}
```

## 未通过项

无。最终测试 21/21 通过；开发中间态的失败已在上文列出并修复。
