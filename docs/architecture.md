# 架构与判定规则明细

本文档补充 README，集中说明跨层的状态转移、判定(verdict)与错误类别，便于
复核「零梯度/未触及行步数规则」与「裁剪不混用」两条重点。

## 1. 分层与依赖方向

```
config / errors / tensors        (无 I/O、无副作用)
        │
        ▼
graph (聚合、裁剪原语, 纯函数)
        │
        ▼
optimizer (行规则, 纯函数)
        │
        ▼
state (训练状态 + 稀疏事务；唯一就地写入边界)
        │
        ├── persistence (事务检查点)
        ▼
service (加锁门面、verdict、journal)
        ▼
app (FastAPI；错误码 → HTTP)
```

下层不知道上层存在；`graph`/`optimizer` 不导入 `state`/`service`，因此可脱离
服务与 I/O 独立测试，并与独立稠密 oracle 对照。

## 2. 一个 batch 的判定状态机

```
请求进入
  │
  ├─ 结构/范围校验失败 (形状、长度、宽度、非整数、NaN/Inf、索引<0 或 >=num_rows)
  │        └─► verdict=rejected, error_code=batch_rejected, HTTP 400
  │            整批拒绝：不聚合、不裁剪、不写任何状态、全局步数不变
  │
  ├─ 空批 (indices 长度 0)
  │        └─► verdict=empty, error_code=empty_batch, HTTP 200(verdict 字段)
  │            不聚合、不裁剪、不步进、全局步数不变
  │
  └─ 非空批
        aggregate(np.add.at, 重复求和) → clip(仅一种模式) → 逐行判定：
             聚合梯度全零的行 ─► 跳过 (zero_skipped)：权重/动量/逐行步数不变
             非零行         ─► 恰好一次优化器步 (active)，逐行步数 +1
        全局步数（更新轮次）恒为 +1（即便所有行都为零）
        verdict = active 非空 ? applied : applied_zero_only
        若步后出现非有限值 ─► 用稀疏 before-image 回滚活跃行，错误上抛，不记成功
```

要点：**逐行步数**只有真正吃到非零梯度的行才增加；**全局步数**记录非空更新
轮次。两者区分使「批发生了」与「这一行更新了」都可审计。

## 3. 裁剪模式严格互斥

| 模式 | 系数 | 作用域 | 零范数行 |
|---|---|---|---|
| `none` | 1.0 | 全部 | 原样（仍为零） |
| `global` | `min(1, max_norm / ‖G‖_F)`，单一系数 | 整个批所有行共享 | 随整体一起缩放 |
| `row` | `min(1, max_norm / ‖g_k‖_2)`，每行独立 | 单行 | 系数 1.0（避免 0/0） |

模式由 `ClipMode` 封闭枚举在配置期确定，`clip_gradients` 用互斥分支实现，
不存在「全局阈值 + 逐行系数」之类的混合路径。`test_graph.py` 中
`test_global_and_row_modes_cannot_be_mixed` 用反例断言两者结果确实不同。

## 4. 持久化事务

1. 取 `touched_rows`（真正步进过的行）的有序索引。
2. 在**同目录**写唯一名 `.<token>.npz`（后缀必须是 `.npz`，否则 `np.savez`
   会自行追加后缀导致发布错文件），写完 `fsync`。
3. 写 `.<token>.json` manifest 并 `fsync`。
4. `os.replace` 原子发布数据，再发布 manifest，再 `fsync` 目录。
5. 任一步失败：删除临时件，旧检查点保持可见，抛 `persistence_error`。

加载时按 manifest 校验 `num_rows/dim/optimizer/format_version`，不匹配抛
`state_shape_error`（HTTP 409），绝不静默错位加载。触及行从检查点覆盖，
未触及行来自确定性初始化，因此重启后冷行仍与初始表逐位一致。

## 5. 错误类别 → HTTP

| error_code | 含义 | HTTP |
|---|---|---|
| `batch_rejected` | 结构/范围错误，整批拒绝，状态零改动 | 400 |
| `empty_batch` | 显式空批 no-op | 200（body.verdict="empty"） |
| `persistence_error` | 检查点读写失败，事务未提交 | 500 |
| `state_shape_error` | 检查点与配置不兼容 | 409 |
| (请求体 JSON 结构错误) | Pydantic 校验 | 422 |

未知异常不会被翻译成成功：service 捕获后记 `verdict=error` 并重新抛出，
状态靠 before-image 回滚。
