# 请求样例

本目录包含三类样例：

1. **`client.py`** —— 可运行的签名客户端（`call` 单发、`demo` 全场景）。
2. **`run_demo.sh`** —— 一键起服务并跑完所有攻击场景（含并发重复）。
3. **`requests.http`** —— 线上报文形态的原始 HTTP 文本样例（含正常请求与重放）。

## 单个请求

```bash
# 先生成密钥并在另一个终端启动服务：
python3 scripts/keygen.py --keystore dev-keys.json --key-id demo-key-1
PYTHONPATH=src python3 -m anti_replay.server --keystore dev-keys.json

# 发送一个合法签名请求（-v 显示规范请求串）：
PYTHONPATH=src python3 examples/client.py call \
    --keystore dev-keys.json --method POST --path /api/data \
    --body '{"hello":"world"}' --show-canonical
```

`requests.http` 里的时间戳是固定样例，超出 ±300 秒窗口后服务端会返回
`stale_timestamp`——这正是重放防护的预期行为；要发实时请求请使用 `client.py`。

## 全场景演示

```bash
examples/run_demo.sh
```

覆盖：正常请求、原样重放（409）、32 路并发重复（恰好 1 个 200）、正文篡改（401）、
时间边界 ±300/±301、路径编码歧义等价性、恶意路径（400）、未知 key（401）。
