# 可验证计算收据：本地整数批次的范围校验与中位数（Rust + RISC Zero zkVM）

纯后端样例：在 RISC Zero zkVM 客体程序中对**最多 256 个有符号 64 位整数**做范围校验，
计算排序后的中位数，并把**输入承诺、批次长度与结果**写入公开 journal（日志）。
宿主只有在**收据通过密码学验证、且 image ID 等于预期程序 ID、且 journal 与实际输入绑定**后，
才返回“已验证结果”。开发模式（dev-mode）的假收据**无法**伪装成真实证明。

- 真实计算：范围校验、排序、中位数、SHA-256 承诺都在 zkVM 内真实执行；
- 真实证明：默认在本地生成真实的 RISC Zero STARK 收据（Composite）；
- 真实验证：宿主用嵌入式 image ID 调用收据验证，并独立重算承诺与中位数；
- 失败如实报告：换程序 ID、篡改 journal、超范围、空批次一律报错，绝不返回看似成功的结果。

---

## 1. 目录结构

```
.
├── Cargo.toml                # 宿主工作区（core / methods / host）
├── core/                     # 共享协议原语（no_std，可编译进 guest）
│   └── src/lib.rs            #   校验规则、规范编码、SHA-256 承诺、Journal 编解码
├── methods/
│   ├── build.rs              # 调 risc0-build 编译 guest 并生成 ELF / image ID
│   ├── Cargo.toml
│   ├── src/lib.rs            # 暴露 BATCH_MEDIAN_ELF 与 image_id()
│   └── guest/                # ★ zkVM 客体程序（独立的 cargo 工程）
│       ├── Cargo.toml
│       └── src/main.rs       #   读输入 → 长度/范围校验 → 排序求中位数 → 提交 journal
├── host/
│   ├── src/lib.rs
│   ├── src/main.rs           # CLI：execute / prove / verify
│   ├── src/service.rs        # 执行、证明、验证三阶段编排与分别计时
│   ├── src/verifier.rs       # ★ 准入控制：假收据拒绝、验印、image ID、输入绑定
│   ├── src/io_types.rs       # 输入 JSON、结果报告、收据封套
│   └── tests/e2e.rs          # 自动化测试（含真实证明的 ignored 测试）
└── examples/                 # 可复现样例输入
    ├── batch.json            #   8 个元素的正常批次（含负数/边界附近值）
    ├── single.json           #   单元素
    ├── max256.json           #   256 个元素（含 ±1_000_000 边界）
    ├── empty.json            #   空批次（必须失败）
    ├── out_of_range.json     #   1_000_001（必须失败）
    ├── out_of_range_neg.json #  -1_000_001（必须失败）
    └── neg_range.json
```

## 2. 环境要求

本机已安装（版本必须匹配，已验证）：

| 组件 | 版本 |
|------|------|
| Rust（宿主，stable） | 1.98.1 |
| `cargo-risczero` / `r0vm` | **3.0.6** |
| rzup Rust 工具链（guest） | 1.97.0，target `riscv32im-risc0-zkvm-elf` |
| `risc0-zkvm` / `risc0-build` crate | **3.0.6**（见 `Cargo.lock`） |

在全新机器上安装工具链（如尚未安装）：

```bash
curl -L https://risczero.com/install | bash
rzup install            # 安装与本项目匹配的 cargo-risczero 3.0.6 / r0vm / rust 工具链
r0vm --version          # 应输出 risc0-r0vm 3.0.6
```

> crate 主版本必须与 `r0vm` 一致。本项目锁定 `risc0-zkvm = 3.0.6`；
> 若你的工具链是其它小版本，请同步修改根 `Cargo.toml` 中的版本并重新 `cargo update -p risc0-zkvm`。

## 3. 快速开始（本地启动）

```bash
# 1) 仅执行（不出证明），快速验证功能；分别报告执行耗时与 cycle 数
cargo run --release -- execute examples/batch.json

# 2) 执行 + 生成真实 STARK 收据 + 用预期 image ID 验证 + 返回已验证中位数
cargo run --release -- prove examples/batch.json

# 3) 离线验证已保存的收据（默认在输入旁生成 *.receipt.json）
cargo run --release -- verify examples/batch.receipt.json
```

`prove` 成功后输出 JSON 报告，其中：

- `verified: true` 且 `receipt_flavor: "composite-stark"` 才表示**真实证明已验证**；
- `timings_ms` 分别给出 `execution_ms`（执行）、`proving_ms`（证明）、
  `verification_ms`（验证）与 `total_ms`，三者互不混淆；
- `image_id_hex` 为预期程序 ID；`input_commitment_hex` 为公开输入承诺（SHA-256）。

## 4. 协议说明（journal 绑定了什么）

客体对输入**持不信任态度**，自行执行全部规则，宿主输入不能替代客体校验：

1. 长度 `1..=256`；
2. 每个元素 ∈ `[-1_000_000, 1_000_000]`；
3. 在客体内复制一份并排序，计算中位数。

偶数长度的中位数约定为**中间两数中较小者**（有界整数上的确定性 tie-break，可复现）。

公开 journal 为固定 56 字节、无第三方序列化格式依赖：

| 偏移 | 长度 | 字段 |
|---|---|---|
| 0 | 4 | magic `BMDJ` |
| 4 | 2 | 版本号（小端，当前为 1） |
| 6 | 2 | 保留（0） |
| 8 | 8 | 批次长度 n（小端） |
| 16 | 8 | 中位数（小端 i64） |
| 24 | 32 | 输入承诺：SHA-256(`u64 LE n` ‖ 各 `i64 LE`，原始未排序顺序) |

宿主在验证后还会：用**实际发送的输入**重算承诺并与 journal 比对（输入绑定），
再独立重算中位数比对（语义复核）。因此收据不可能“为另一批输入作证”。

## 5. 安全模型：开发模式收据不能冒充真实证明

- 默认 `prove` 调用本地 prover，`dev_mode=false`，产出真实 `Composite` STARK 收据。
- 只有显式加上全局 `--dev-mode`（或 prover 端 `RISC0_DEV_MODE=1`）才会产出 `FakeReceipt`，
  stderr 会打印醒目告警，报告中 `receipt_flavor` 为 `"fake-dev-only"`、`note` 明确标注 FAKE。
- `host/src/verifier.rs` 的准入逻辑**默认拒绝任何 Fake 收据**（在密码学验证之前就拒绝），
  且验证使用显式构造的 `VerifierContext::with_dev_mode(allow_dev)`，
  不被环境变量静默影响。
- 还可编译期彻底关闭 dev 模式：`cargo build --release --features strict`
  （启用 risc0 自带 `disable-dev-mode`，设 `RISC0_DEV_MODE=1` 会直接 panic）。

## 6. 验收命令（逐项对应需求）

```bash
# 0) 全部快速自动化测试（用 dev 收据覆盖对抗场景，秒级）
cargo test -p batch-median-host --test e2e

# 1) 真实证明端到端：生成真实 STARK、默认严格路径验证、并复测篡改/换 ID
cargo test --release -p batch-median-host --test e2e -- --ignored --test-threads=1

# 2) 正常批次：真实证明并验证（输出 verified=true, flavor=composite-stark）
cargo run --release -- prove examples/batch.json --no-save

# 3) 更换程序 ID：验证必须失败（verified=false，退出码 2）
cargo run --release -- verify examples/batch.receipt.json \
  --expect-image-id 0000000000000000000000000000000000000000000000000000000000000000

# 4) 篡改日志（journal）：翻转中位数字节，验印必须失败
cargo run --release -- verify examples/batch.receipt.json --tamper-journal

# 5) 超范围输入：宿主预检失败（退出码 1）
cargo run --release -- execute examples/out_of_range.json
cargo run --release -- execute examples/out_of_range_neg.json

# 6) 空批次：宿主预检失败
cargo run --release -- execute examples/empty.json

# 7) 证明“客体自身”也强制规则（绕过宿主预检，客体 panic，退出码 1）
cargo run --release -- execute examples/out_of_range.json --unsafe-skip-preflight
cargo run --release -- execute examples/empty.json        --unsafe-skip-preflight

# 8) dev 假收据不能在默认验证下通过：
cargo run --release --dev-mode prove examples/single.json \
  --receipt-out /tmp/fake.receipt.json
cargo run --release -- verify /tmp/fake.receipt.json     # 期望 verified=false, 退出码 2
cargo run --release --dev-mode verify /tmp/fake.receipt.json  # 仅显式 opt-in 才接受，且标记 FAKE
```

> 说明：`verify` 在“密码学上被拒绝”时按设计输出一份 `verified:false` 的报告并以**退出码 2**
> 结束（便于脚本区分“验过但被拒”与“工具自身出错（退出码 1）”）。

## 7. 自动化测试清单

`host/tests/e2e.rs`（默认跑 12 个，秒级）：

- `fake_receipt_is_rejected_by_default_verifier` —— **假收据默认必拒**
- `fake_receipt_is_accepted_only_with_explicit_opt_in`
- `changed_image_id_is_rejected` —— 更换程序 ID
- `tampered_journal_is_rejected` / `tampered_commitment_is_rejected` —— 篡改日志
- `receipt_does_not_bind_to_different_inputs` —— 给另一批输入则绑定失败
- `reordered_inputs_have_distinct_commitments`
- `out_of_range_inputs_rejected_pre_flight`、`empty_batch_rejected_pre_flight`、
  `oversized_batch_rejected_pre_flight`
- `malformed_journal_bytes_are_rejected`、`boundary_values_and_max_length_admitted`

`--ignored` 测试（真实 STARK，需 `--release`，约数十秒）：

- `real_stark_receipt_is_verified_and_admitted` —— 真实收据在严格默认路径下通过，
  且对篡改日志、更换 image ID 仍然失败；
- `real_proof_rejects_out_of_range_and_empty` —— 真实 prover 对客体报错不产出成功收据。

## 8. 可复现性

- 宿主依赖锁定：根 `Cargo.lock`（已提交）。
- guest 是独立 cargo 工程，拥有自己的 `methods/guest/Cargo.lock`（已提交）。
- ELF 与 image ID 在编译期由 `risc0-build` 计算并嵌入宿主；相同源码 + 锁定依赖
  复现同一 image ID（`cargo run --release -- execute examples/batch.json` 打印的
  `image_id_hex`）。
- guest 只能通过宿主构建脚本编译（`risc0-build` 会自动加上
  `--target riscv32im-risc0-zkvm-elf` 及所需 flags），不要在 `methods/guest` 目录里
  直接 `cargo build`（默认 target 下找不到 zkVM 专属依赖）。
- 首次构建会联网拉取 crate；之后完全离线、强制锁定版本：
  ```bash
  cargo fetch
  RISC0_BUILD_LOCKED=1 cargo build --release --offline   # guest 以 --locked 构建
  ```

## 9. 故障排查

- **`Risc Zero Rust toolchain not found`**：运行 `rzup install`（见第 2 节）。
- **版本不匹配 / verifier parameters 报错**：确认 `r0vm --version` 与 `Cargo.lock` 中
  `risc0-zkvm` 同为 3.0.6。
- **内存不足**：真实证明在 16 核机器上约需数 GB 内存；可关闭其它大进程，或用单段小输入
  `examples/single.json`。
- **首次 `prove` 很慢**：Composite STARK 证明是 CPU 密集型，属于正常；验证是毫秒级。
- **clippy 报 guest `can't find crate for std`**：clippy 的 `RUSTC_WRAPPER` 会干扰
  guest 子构建。guest 已由正常构建产出时，用 `RISC0_SKIP_BUILD=1 cargo clippy` 跳过即可。
