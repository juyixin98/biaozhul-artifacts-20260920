# 版本化 Gas 计量器（Versioned Gas Meter）

离线 WASM 执行沙箱 + 版本化资源计量服务。纯后端，Rust 实现：

- **Wasmtime 24**：编译并执行不可信 WASM 模块，以确定性的 **fuel** 限制计算量；
- **Axum 0.7**：HTTP API（提交执行、查询状态、计量版本表、执行日志、样例下载）；
- 模块只能调用**明确白名单**的宿主函数，**无网络、无任意文件访问、无 WASI**；
- 宿主写入先进**事务缓冲**，执行成功才**原子发布**（临时文件 + `rename`），
  燃料耗尽 / 内存越界 / 陷阱时整体回滚，不提交任何宿主状态；
- 相同模块 + 输入 + 计量版本 + 燃料上限产生相同结果与相同燃料账单（`determinism_key`）。

> ⚠️ **燃料不是链 Gas。** 响应中的燃料是本沙箱的确定性资源单位
> （多数 WASM 指令消耗 1 单位；宿主调用按计量版本表附加收费）。
> 它不代表任何区块链的真实 Gas 或费用，映射到链 Gas 需要链方提供经过审计的转换表。
> 每个燃料账单都带 `unit` 与 `disclaimer` 字段明确声明这一点。

---

## 目录结构

```
.
├── Cargo.toml              # 依赖清单（锁定版本见 Cargo.lock）
├── build.rs                # 编译期把 wat/*.wat -> wasm/*.wasm
├── wat/                    # 7 个手写 WAT 样例模块
├── src/
│   ├── main.rs             # gas-meter CLI（serve / run-sample / export-samples）
│   ├── engine.rs           # 沙箱引擎：白名单 linker、fuel、limiter、事务、原子提交
│   ├── schedule.rs         # 计量版本注册表（v1 基线、v2 宿主费翻倍）
│   ├── state.rs            # 提交态 KV（原子落盘）+ 内存执行日志
│   ├── api.rs              # Axum 路由与请求/响应模型
│   ├── error.rs            # 陷阱/链接/编译错误分类
│   ├── samples.rs          # 内嵌样例模块
│   └── base64.rs           # 标准 base64（无额外依赖）
├── tests/
│   ├── integration.rs      # 16 个引擎级测试
│   └── http_api.rs         # 10 个 HTTP 级测试
├── examples/requests/      # 9 个可直接 curl 的请求体
└── scripts/acceptance.sh   # 一键验收（测试 + clippy + 构建 + 端到端冒烟）
```

## 环境要求

- Rust（验证于 rustc 1.98.1；edition 2021）
- 无需外部数据库、无需网络访问即可运行（依赖在构建期从 crates.io 获取）

---

## 本地启动

```bash
# 1) 构建（首次会编译 wasmtime，约 1–3 分钟）
cargo build --release

# 2) 启动 HTTP 服务（默认 127.0.0.1:8080，数据目录 ./data）
./target/release/gas-meter serve --addr 127.0.0.1:8080 --data-dir ./data

# 健康检查
curl -s http://127.0.0.1:8080/health
# {"service":"versioned-gas-meter","status":"ok"}
```

离线运行单个样例（不落盘，便于调试）：

```bash
# 输入默认：n=100000（8 字节小端）
./target/release/gas-meter run-sample finite_loop
# 自定义输入（base64）
./target/release/gas-meter run-sample finite_loop --input-b64 "$(python3 -c 'import base64;print(base64.b64encode((5000).to_bytes(8,"little")).decode())')"
# 指定计量版本或燃料上限
./target/release/gas-meter run-sample infinite_loop --version 1
./target/release/gas-meter run-sample finite_loop --fuel-limit 1000

# 导出示例 wasm 字节
./target/release/gas-meter export-samples --dir ./wasm-out
```

---

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/v1/metering/versions` | 计量版本表（燃料上限、内存上限、宿主收费） |
| GET | `/v1/metering/versions/:v` | 单个版本 |
| POST | `/v1/transactions` | 提交一次沙箱执行 |
| GET | `/v1/state` | 提交态全部键（含值长度） |
| GET | `/v1/state?key=sum` | 读取单个键（值为 base64） |
| GET | `/v1/journal?limit=20` | 最近执行日志（成功与失败都记录） |
| GET | `/v1/samples` | 样例清单 |
| GET | `/v1/samples/:name` | 下载样例 `.wasm`（`application/wasm`） |

`POST /v1/transactions` 请求体：

```json
{
  "module_ref": "sample:finite_loop",
  "module_base64": null,
  "input_base64": "oWQ3AAAAAA==",
  "metering_version": 1,
  "fuel_limit": 20000000,
  "idempotency_key": "可选-幂等键"
}
```

- `module_ref`（`sample:<name>`）与 `module_base64`（标准 base64 的 wasm 字节）二选一；
- `fuel_limit` 可省略；若提供则**不得超过版本表上限**，否则 `bad_request`；
- `idempotency_key`：相同键的重复请求直接返回首次结果，不重复执行、不重复发布。

**HTTP 状态码语义**：参数/格式错误为 4xx；沙箱按预期终止（燃料/越界/陷阱/链接失败）
返回 **200**，响应体中 `"status": "terminated"` 且 `termination` 给出机器可读原因
（`out_of_fuel` / `memory_out_of_bounds` / `memory_limit_exceeded` / `trap` /
`link_error` / `compile_error` / `module_rejected` / `bad_request` / `host_error`）。

---

## 快速体验（curl）

```bash
# n = 100000 的小端 8 字节
N=$(python3 -c 'import base64;print(base64.b64encode((100000).to_bytes(8,"little")).decode())')

# 1) 有限循环：1+..+n，真实 SHA-256，成功提交
curl -s -X POST http://127.0.0.1:8080/v1/transactions \
  -H 'content-type: application/json' \
  -d "{\"module_ref\":\"sample:finite_loop\",\"input_base64\":\"$N\"}" | python3 -m json.tool

# 2) 无限循环：燃料耗尽终止，缓冲的 poison 不提交
curl -s -X POST http://127.0.0.1:8080/v1/transactions \
  -H 'content-type: application/json' \
  -d '{"module_ref":"sample:infinite_loop"}' | python3 -m json.tool

# 3) 陷阱后写入回滚 / 内存增长越限 / 越界读 / 非白名单导入
for s in trap_after_write memory_grow oob_read host_bad_ptr denied_import; do
  curl -s -X POST http://127.0.0.1:8080/v1/transactions \
    -H 'content-type: application/json' \
    -d "{\"module_ref\":\"sample:$s\"}" | python3 -c 'import json,sys;r=json.load(sys.stdin);print(r["termination"])'
done

# 4) 提交态：只有成功执行的 sum/hash，没有 poison
curl -s http://127.0.0.1:8080/v1/state | python3 -m json.tool

# 5) 计量版本对比：相同输入，v1 与 v2 结果相同但宿主账单不同
curl -s -X POST http://127.0.0.1:8080/v1/transactions -H 'content-type: application/json' \
  -d "{\"module_ref\":\"sample:finite_loop\",\"input_base64\":\"$N\",\"metering_version\":2}" \
  | python3 -m json.tool
```

`examples/requests/` 目录为每个样例准备了可直接使用的请求体：

```bash
curl -s -X POST http://127.0.0.1:8080/v1/transactions \
  -H 'content-type: application/json' --data @examples/requests/infinite_loop.json
```

---

## 样例模块（`wat/`）

| 样例 | 行为 | 预期终止 |
|---|---|---|
| `finite_loop` | 1..=n 累加；`kv_put("sum")`、宿主真实 `sha256`、`kv_put("hash")`、`kv_get` 读回 | `committed` |
| `infinite_loop` | 先 `kv_put("poison")` 后死循环 | `out_of_fuel`，poison 不发布 |
| `trap_after_write` | 先 `kv_put("poison")` 后 `unreachable` | `trap`，事务回滚 |
| `memory_grow` | 循环 `memory.grow` 直到越过版本内存上限 | `memory_limit_exceeded` |
| `oob_read` | 从线性内存末端边界读取 | `memory_out_of_bounds` |
| `host_bad_ptr` | 向 `kv_get` 传越界输出指针，宿主边界校验 | `memory_out_of_bounds` |
| `denied_import` | 尝试导入 `env.socket_connect`（网络） | `link_error`，实例化失败 |

### Guest ABI

沙箱接受的模块必须导出：

- `memory: Memory`（线性内存）
- `alloc(size: i32) -> i32`（在 guest 内存中分配输入缓冲）
- `run(in_ptr: i32, in_len: i32) -> i64`；返回值打包为 `(out_ptr << 32) | out_len`

### 白名单宿主函数（`env` 模块，仅这 4 个）

| 函数 | 签名 | 说明 |
|---|---|---|
| `kv_put` | `(key_ptr,key_len,val_ptr,val_len)` | 写入**事务缓冲**，成功后才发布 |
| `kv_get` | `(key_ptr,key_len,out_ptr,out_cap) -> i32` | 读提交态快照 + 事务覆盖层；未命中/缓冲不足返回 -1 |
| `sha256` | `(in_ptr,in_len,out_ptr)` | 真实 SHA-256（RustCrypto `sha2`），输出 32 字节 |
| `abort` | `(msg_ptr,msg_len)` | guest 主动终止（陷阱），不发布 |

所有指针/长度在宿主侧都做线性内存**边界校验**，越界即以 `memory_out_of_bounds` 陷阱终止。
键必须为 UTF-8，键/值长度受计量版本约束。Linker 不链接 WASI，也不提供时钟、随机数、
环境变量或任何 fd —— 模块在能力层面就无法联网或读写文件。

---

## 安全与正确性模型

1. **能力白名单**：`Linker` 只注册 4 个函数；任何其它导入在实例化阶段以
   `unknown import` 链接错误失败，模块体不会执行。
2. **计算量**：`Config::consume_fuel(true)` + `Store::set_fuel`，燃料归零确定性陷阱；
   无限循环必然终止。宿主调用额外收费时若余额不足，先把燃料池清零再抛 `OutOfFuel`，
   保证账单恒等式 `consumed_total = wasm_instruction_fuel + host_call_fuel`。
3. **内存**：`ResourceLimiter::memory_growing/table_growing` 强制版本上限；
   WASM 指令越界由 wasmtime 自身陷阱保证；宿主函数额外校验所有 guest 指针。
4. **事务原子性**：执行期间 `Overlay`（puts/deletes）隔离于提交态；仅在 guest 正常返回、
   输出范围可解码后，`CommittedState::apply` 才更新内存态并以
   `写临时文件 → fsync → rename → fsync 目录` 的顺序原子落盘。
   任何终止路径覆盖层随 `Store` 丢弃，进程崩溃也只会留下旧的完整 `state.json`。
5. **确定性**：固定 Cranelift 优化级别；燃料计量只依赖锁定的 wasmtime 版本与计量版本表，
   不含时间/随机数来源。`determinism_key = sha256(版本 | 模块摘要 | 输入摘要 | 燃料上限)`。
   计时字段（`started_unix_ms`、`duration_ms`）仅用于运维，不参与确定性。
6. **如实报告**：所有失败（含宿主持久化失败）都以结构化 `termination/error` 返回，
   绝不把失败伪装成成功；CLI 失败时退出码为 1。
7. **幂等**：`idempotency_key` 命中时返回首次响应，不重复执行与发布（该缓存为进程内）。

已知边界（如实说明）：

- 执行日志（`/v1/journal`）与幂等缓存是进程内存态，重启后清空；提交态是持久化的。
- 服务假定单次请求串行处理提交（当前实现 `apply` 在锁内）；未实现跨请求的并发冲突
  语义（如乐观锁版本协商），这超出本次范围。
- 不支持 WASM 多内存/线程/异常提案等非默认特性（使用 wasmtime 默认特性集）。

---

## 计量版本

| 版本 | 名称 | 燃料上限 | 内存上限 | 宿主收费 |
|---|---|---|---|---|
| 1 | `2026-01-baseline` | 20,000,000 | 4 MiB | 基线（`call_base=100`，`sha256_base=2000` 等） |
| 2 | `2026-06-hostcostx2` | 20,000,000 | 8 MiB | 宿主收费 ×2 |

版本参数冻结后不改；调整参数只能新增版本。相同输入在不同版本下**结果相同、
账单不同**（v2 宿主费恰为 v1 的两倍），且每个版本都有 `notes` 字段解释计量规则。
查询：`curl -s http://127.0.0.1:8080/v1/metering/versions`。

---

## 自动化测试

```bash
# 全部 27 个测试（1 个 base64 单测 + 16 个引擎集成测试 + 10 个 HTTP 测试）
cargo test

# Lint（零警告）
cargo clippy --all-targets -- -D warnings
```

测试覆盖：真实计算与 SHA-256、燃料账单守恒、陷阱/燃料/内存/越界/链接失败回滚、
落盘恢复、相同输入确定性、v1/v2 账单差异、v2 内存上限、燃料覆盖与上限拒绝、
幂等只发布一次、HTTP 4xx/200 语义、状态与日志接口。

### 一键验收

```bash
./scripts/acceptance.sh
```

它会依次执行：release 测试 → clippy（`-D warnings`）→ release 构建 → 在动态空闲端口
启动服务 → 全部成功/失败样例端到端冒烟（含回滚与持久化文件校验）→ 确定性重放对比。

---

## 依赖（均已锁定，见 `Cargo.lock`）

`wasmtime 24`、`axum 0.7`、`tokio 1`、`serde/serde_json 1`、`sha2 0.10`（真实 SHA-256）、
`hex`、`wat 1`（构建期编译 WAT）、`anyhow`、`thiserror`；
开发依赖 `tempfile`、`tower`、`http-body-util`。
