# 请求样例

每个 `*.json` 是一个可直接运行的请求；`results/*.result.json` 是其在当前代码下
实际运行得到的完整响应（随仓库提交，便于不运行即可查看）。

```bash
../../bin/cbcast -in chain.json        # 或: go run ../../cmd/cbcast -in chain.json
```

| 文件 | 场景 | 关键验证点 |
|---|---|---|
| `chain.json` | A→B→C→D 链式因果广播 | B1 先于 A1 到 C、C1 先于 B1 到 D；缓冲后级联释放 |
| `concurrent.json` | A/B/C 各广播一条，互相独立 | 乱序到达但零误缓冲；A1 迟到副本被判重 |
| `loss.json` | A1 在 C 处永久丢失 | B1 永久缓冲；根因链 B1→A1（根因 A1@C） |
| `backpressure.json` | C 缓冲容量为 1 | A3 背压被拒，A1 补齐后释放 A2，重传 A3 成功，顺序 A1→A2→A3 |

字段语义见上级目录 `README.md` 的“JSON 请求格式”一节。
