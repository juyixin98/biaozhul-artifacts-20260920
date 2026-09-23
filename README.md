# 版本化 Gas 计量器（Versioned Gas Meter）

一个**纯后端**的离线 WASM 执行沙箱：模块只能调用明确白名单内的宿主函数，
没有网络、没有任意文件访问；执行受**燃料**与**内存**双重限制；
宿主写入先进事务缓冲，执行成功才原子发布，失败一律回滚。

每次执行返回一张**可复现的收据**：模块 SHA-256、输入 SHA-256、计量版本、
燃料上限/明细、内存上限、终态与结果摘要。相同模块 + 输入 + 版本重复执行，
结果、燃料账目与 `result_hash` 完全一致。

> **重要诚实声明**：本项目的“燃料（fuel）”是 **Wasmtime 的抽象计量单位**，
> 用于确定性的资源记账与终止保证。它**不是任何区块链的真实链上 Gas**，
> 本项目也不提供、不暗示任何 fuel→gas 换算系数。收据与 API 中均显式标注这一点。

## 技术栈

| 组件 | 版本（锁定于 Cargo.lock） | 用途 |
|---|---|---|
| Rust | 1.98 (edition 2021) | 宿主与来宾 |
| wasmtime | 24.0.13 | WASM 编译、fuel 计量、ResourceLimiter |
| axum | 0.8.9 | HTTP API |
| tokio | 1.x | 异步运行时（沙箱在 blocking 池执行） |
| wat | 1.x | WAT 样例运行时编译 |
| sha2 / hex / base64 | — | 摘要、编码 |

## 沙箱安全边界

1. **不链接 WASI**，不提供 fd/socket/environ/clock 等任何能力。
2. **导入白名单**：实例化前静态检查，模块的每个 import 必须是命名空间
   `gas` 下这 5 个函数之一，且必须是函数类型；任何其他 import
   （`wasi_snapshot_preview1::fd_write`、`env::*`、`gas::socket_connect`…）
   一律拒绝（HTTP 400，模块不会被实例化）：
   - `input_read(ptr, max_len) -> i32`
   - `output_write(ptr, len) -> i32`
   - `kv_get(key_ptr, val_ptr, val_max) -> i32`（读执行开始时的快照）
   - `kv_put(key_ptr, val_ptr, val_len) -> i32`（写事务缓冲）
   - `kv_has(key_ptr) -> i32`
3. **燃料终止**：Wasmtime `consume_fuel` 按指令真实扣燃料；宿主调用按
   版本化单价额外扣燃料。余额为 0 立即陷阱 → `out_of_fuel`。
   无限循环无需 wall-clock 也必然终止。
4. **内存/表限制**：通过 `ResourceLimiter` 对每次调用设硬性上限；
   超限返回错误使增长操作立即陷阱 → `memory_limit_exceeded`
   （而不是让 `memory.grow` 安静地返回 -1）。
5. **宿主侧边界检查**：传给宿主函数的指针/长度在模块线性内存范围内校验，
   越界 → `trap`。
6. **事务原子性**：`kv_put` 只写本次执行私有的 `BTreeMap` 缓冲；
   仅当入口 `run()` 返回 **0** 时才合并进宿主 KV 的最新 `Arc` 快照（整体替换，
   对外原子可见）。燃料耗尽、越界陷阱、`unreachable`、非零返回都丢弃缓冲，
   已提交的既有状态不受影响。事务内 `kv_get/kv_has` 只能看到执行开始时的快照，
   看不到自己的未提交写入。

## 计量版本

| 版本 | input_read /字节 | output_write /字节 | kv_get /字节 | kv_put /字节 | kv_has /次 |
|---|---|---|---|---|---|
| v1 `v1-baseline-2026-09` | 1 | 1 | 2 | 5 | 20 |
| v2 `v2-write-weighted-2026-09` | 1 | 2 | 2 | **50** | 20 |

版本改变定价，不改变计算语义：同一执行在 v1/v2 下输出相同、燃料不同。
收据中每条宿主调用都有 `{seq, host_fn, units, unit_price_fuel, fuel}` 明细，
满足 `units × unit_price = fuel`，且
`total = wasm_fuel + host_fuel = fuel_limit − remaining`。

## 目录结构

```
├── Cargo.toml / Cargo.lock      # 锁定依赖
├── build.rs                     # 从 Cargo.lock 注入 wasmtime 版本
├── src/
│   ├── main.rs                  # 服务入口 (--listen, --seed-file)
│   ├── lib.rs
│   ├── api.rs                   # Axum 路由 / 请求响应
│   ├── sandbox.rs               # 沙箱、白名单、燃料/内存限制、事务
│   ├── state.rs                 # KV 快照隔离 + 原子发布
│   ├── versions.rs              # 计量版本与定价规则
│   ├── receipt.rs               # 执行收据
│   └── samples.rs               # 内置样例
├── wasm/                        # WAT 样例 + 预编译 Rust guest
│   ├── finite_loop.wat          # 有限循环（真实整数累加，提交）
│   ├── infinite_loop.wat        # 无限循环（燃料耗尽，不提交）
│   ├── trap_after_write.wat     # 写入后 unreachable（回滚）
│   ├── memory_grow.wat          # 内存增长（限内成功/超限终止）
│   ├── host_oob.wat             # 宿主越界指针（陷阱+回滚）
│   ├── frame_protocol.wat       # 协议：长度前缀分帧 + 真实 CRC-32 往返
│   └── prebuilt/sha256guest.wasm  # Rust(no_std) 真实 SHA-256 guest
├── wasm_modules/sha256guest/    # 上述 guest 的 Rust 源码（独立 crate）
├── examples/
│   ├── seed.json                # 启动时种子 KV
│   └── requests/*.json          # 每个样例的请求体
├── scripts/accept.sh            # 一键验收
└── tests/                       # 34 个自动化测试
```

## 本地启动

```bash
# 需要 Rust（在 rustc 1.98 上验证）。仅在重建 Rust guest 时需要：
rustup target add wasm32-unknown-unknown

cargo build                      # 或 cargo build --release
./target/debug/gas-meter \
  --listen 127.0.0.1:8080 \
  --seed-file examples/seed.json
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok","offline_sandbox":true,"network_access_for_modules":false,...}
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活与沙箱能力声明 |
| GET | `/v1/versions` | 计量版本、单价、wasmtime 版本、非 gas 声明 |
| GET | `/v1/samples` | 内置样例列表 |
| GET | `/v1/samples/{id}/wasm` | 下载样例 wasm 字节 |
| POST | `/v1/execute` | 执行一次沙箱调用 |
| GET | `/v1/state` | 查看已提交 KV（值为 hex） |

`POST /v1/execute` 请求体（字段均可组合）：

```json
{
  "sample_id": "finite_loop",        // 或 "module_base64": "<wasm>"
  "input_base64": "Cg==",            // 或 "input_text": "..."
  "metering_version": 1,             // 1 或 2，默认 1
  "fuel_limit": 10000000,            // 可选，默认 10_000_000
  "memory_limit_bytes": 8388608,     // 可选，默认 8 MiB
  "table_limit_elements": 10000      // 可选
}
```

模块的失败（燃料耗尽/陷阱/中止）**不是 HTTP 错误**：返回 200，结果在
`receipt.status` 中（`success` / `out_of_fuel` / `memory_limit_exceeded` /
`trap` / `module_abort`）。只有请求本身非法（坏 base64、未知版本、
非法 import 等）才返回 4xx。

## 验收命令

```bash
# 1) 全量自动化测试（34 个：安全/确定性/密码学/HTTP）
cargo test

# 2) 静态检查
cargo clippy --all-targets

# 3) 一键验收：自启服务、跑全部 11 个示例请求、用 python hashlib
#    交叉验证 SHA-256、校验回滚/确定性/版本计价，然后自动关闭
./scripts/accept.sh                 # debug
./scripts/accept.sh --build-release # release
```

手工快速体验：

```bash
cargo run &
sleep 2
curl -s -X POST http://127.0.0.1:8080/v1/execute -H 'content-type: application/json' \
  -d @examples/requests/finite_loop.json | jq .
curl -s -X POST http://127.0.0.1:8080/v1/execute -H 'content-type: application/json' \
  -d @examples/requests/infinite_loop.json | jq .receipt.status
curl -s -X POST http://127.0.0.1:8080/v1/execute -H 'content-type: application/json' \
  -d @examples/requests/sha256guest_abc.json | jq -r .receipt.output_base64 \
  | base64 -d | xxd -p     # ba7816bf...015ad
```

## 样例与实测结果

| 样例 | 输入 | 预期终态 | 关键验证 |
|---|---|---|---|
| `finite_loop` | 1 字节 n=10 | `success` | 输出 `sum=55, sqsum=385`，KV 提交 n/trace |
| `infinite_loop` | 任意 | `out_of_fuel` | 燃料归零、终止、零提交 |
| `trap_after_write` | 1 字节 | `trap` | `poisoned` 键不出现 |
| `memory_grow` | 小端 u32 页数 | `success` / `memory_limit_exceeded` | 限内触碰新页；超限终止 |
| `host_oob` | 无 | `trap` | 宿主指针边界检查，写入回滚 |
| `frame_protocol` | `n(u32le)+帧区` | `success` / `module_abort` | 每帧输出追加真实 CRC-32，与 zlib 一致；截断帧拒绝不提交 |
| `sha256guest` | `iter(u32le)+msg` | `success` / `out_of_fuel` | 与 hashlib 逐向量一致；高迭代可被燃料杀死 |

`sha256guest` 是一个真正的 `#![no_std]` Rust 来宾（约 3.5 KiB wasm），
自带完整 SHA-256 压缩函数实现，不调用任何系统库，只导入 `gas::*`。
已验证向量包括 NIST FIPS 180 的 `""`、`"abc"`、长串以及最多 100 轮链式哈希，
与宿主侧独立的 `sha2` crate / Python `hashlib` 结果逐字节一致。

重建 guest（非必需，产物已提交）：

```bash
cd wasm_modules/sha256guest
cargo build --release --target wasm32-unknown-unknown
cp target/wasm32-unknown-unknown/release/sha256guest.wasm ../../wasm/prebuilt/
```

## 确定性说明

- 引擎配置在进程内固定（Cranelift，fuel 开启，SIMD/线程/GC 等关闭）。
- 不给模块任何时钟/随机源/环境变量；输出只取决于模块字节、输入与版本。
- `result_hash = SHA-256(status ‖ 燃料总额/wasm/host ‖ 每条计费明细 ‖ 输出)`。
  跨 debug/release 构建、重复进程均一致（由测试保证）。
- Wasmtime 升级属于计量环境变化：版本的 `rules_summary` 与收据记录了
  Wasmtime 版本；如需严格固化，可在版本升级时新增计量版本号。

## 明确不做的事

- 不把 fuel 称为 gas，不提供 gas 价格/换算。
- 不提供任何前端页面。
- 不给模块网络或文件能力；不做 WASI 兼容。
