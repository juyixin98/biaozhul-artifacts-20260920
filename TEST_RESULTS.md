# 实测记录（2026-09-23）

环境：Linux 6.8.0 x86_64；Rust 1.98.1 (stable)；cargo 1.98.1。
所有命令在交付目录内实际执行，结果如实记录。

## 1. 自动化测试 `cargo test`

```
running 10 tests
test unknown_sessions_and_parts_404 ............................. ok
test wrong_part_length_rejected ................................. ok
test missing_parts_blocks_completion ............................ ok
test zero_length_object_roundtrips .............................. ok
test repeated_complete_is_idempotent ............................ ok
test published_object_is_immutable .............................. ok
test part_reupload_identical_is_idempotent_different_rejected ... ok
test wrong_total_hash_is_rejected_and_unreadable_after_restart .. ok
test out_of_order_upload_then_read .............................. ok
test restart_after_crash_heals_state ............................ ok

test result: ok. 10 passed; 0 failed; 0 ignored
```

单元/集成/doc 测试合计：10 通过，0 失败。

## 2. 真实 HTTP 端到端（启动 release/debug 服务后跑示例脚本）

- `examples/demo.sh http://127.0.0.1:8080`：**通过**
  - 发布前读取 → `404`
  - 按 2、0、1 乱序上传三块 → 均 `200`
  - 相同内容重传 part 0 → `200`，sha256 与首次一致（幂等）
  - 首次 complete → `201`；再次 complete → `201`（幂等）
  - GET 对象响应头含 `x-object-sha256`/`etag`，`cmp` 字节一致
- `examples/failure_demo.sh http://127.0.0.1:8080`：**通过**
  - 缺块 complete → `422`，消息列出 `missing parts: [1]`，状态接口同样报缺失
  - 同编号不同内容 → `409 part_conflict`
  - 整对象哈希不符 → `422 whole-object sha256 mismatch`，随后 GET → `404`
  - 同 key 再次建会话 → `409`（不可变）
  - 脚本结尾打印 `ALL FAILURE-PATH CHECKS PASSED`

## 3. 真实进程崩溃恢复（SIGKILL，非优雅退出）

`examples/crash_recovery.sh`：**通过**（关键实测输出）

```
session created; parts 0 and 2 uploaded; 1 and 3 missing
--- SIGKILL server mid-upload ---
--- restart, verify state survived and object still unreadable ---
state OK: received [0, 2] missing [1, 3]
object before completion: HTTP 404 (want 404)
resumed + completed: HTTP 201
--- SIGKILL after publish, then restart ---
read after crash: HTTP 200
idempotent re-complete after restart: HTTP 201
VERIFIED: 3145851 bytes identical, sha256 correct
CRASH-RECOVERY ACCEPTANCE PASSED
```

覆盖：上传中途 `kill -9` → 重启后已传块保留、缺块清单正确、对象仍 `404`；
补传缺块并发布后再次 `kill -9` → 重启后只读到完整且 SHA-256 正确的对象，
重复 complete 仍幂等。

## 4. 构建产物

- `cargo build`：成功
- `cargo build --release`：成功，产物 `target/release/chunk-upload`（约 4.0 MB）
- 依赖已锁定在 `Cargo.lock`（cargo lockfile v4）

## 未完成 / 未覆盖项（如实说明）

1. **未运行 clippy / rustfmt**：环境以 `--profile minimal` 安装工具链，未含
   clippy/rustfmt 组件；代码按标准风格手写，`cargo build`/`cargo test` 无警告。
   需要时执行 `rustup component add clippy rustfmt && cargo clippy && cargo fmt`。
2. 读取对象当前为整文件载入内存（面向验收规模），未做超大对象的流式分块输出；
   接口不变即可扩展（README“限制”一节已注明）。
3. 未实现会话过期 GC、鉴权、TLS（建议部署于反向代理之后）。
4. 并发安全当前以单进程异步互斥锁 + 文件系统原子操作保证；未做跨多实例
   （多机共享文件系统）场景的测试与加锁。
