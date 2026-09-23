# 可验证计算收据（Verifiable Computation Receipt）— RISC Zero

纯后端样例：在 **RISC Zero zkVM** 中对一批**最多 256 个有符号整数**做范围校验、
排序并计算中位数，生成真实的 **STARK 收据（receipt）**。公开日志（journal）
以密码学方式绑定**输入承诺、长度和结果**。宿主只有在**验证收据通过、且程序
ID 与预期镜像一致**之后才返回“已验证结果”。**开发模式（dev mode）的假收据
永远无法伪装成真实证明**，会被宿主显式拒绝。

- 语言/工具链：Rust（host, stable）+ RISC Zero `3.0.6`（guest target
  `riscv32im-risc0-zkvm-elf`）
- 证明类型：composite STARK（本地 CPU，经 `r0vm` 子进程）
- 无前端、无网络依赖（除首次构建下载 crates）

---

## 1. 它证明了什么

guest（`methods/guest/median_guest`）在 zkVM 内执行以下逻辑，宿主无法伪造：

1. 读取 `Vec<i32>`（输入对验证者保密，只公开承诺）。
2. **范围/批次校验**（在 zkVM 内强制执行）：
   - 非空；
   - 长度 `1..=256`；
   - 每个元素 ∈ `[-10000, 10000]`。
   - 任一条件不满足 → guest panic，执行故障，**根本不会产生成功收据**。
3. 排序并取**下中位数**（偶数长度取排序后下标 `(n-1)/2`，保证结果仍为整数）。
4. 计算 `SHA-256(规范编码(输入))` 作为**输入承诺**，commit 到公开日志：

   ```text
   JournalData { input_commitment: [u8;32], length: u32, median: i32 }
   ```

   规范编码 = `u32 小端长度 || 每个 i32 小端`（guest 与宿主共用同一函数，
   定义见 `shared/src/lib.rs`）。

收据的 seal 对“**这个镜像（image ID）执行、Halted(0)、输出该 journal**”负责。
宿主在返回结果前做四重检查（`host/src/verify.rs`）：

1. `receipt.inner` **不是** `InnerReceipt::Fake`（显式拒绝 dev-mode 假收据）；
2. 用 `VerifierContext::default().with_dev_mode(false)` 验证 seal，
   环境里就算设了 `RISC0_DEV_MODE=1` 也无法降级；
3. 收据必须对**预期程序 ID** `MEDIAN_GUEST_ID` 验证通过（换程序即失败）；
4. 重算输入承诺并比对长度——journal 必须绑定宿主实际送入的那批数据。

> 可选加固：还可在 `risc0-zkvm` 上开启 `disable-dev-mode` feature，从编译期
> 彻底关闭 dev mode（见文末“加固选项”）。

---

## 2. 目录结构

```
Cargo.toml                 工作区（host / methods / shared）
rust-toolchain.toml
shared/                    guest 与 host 共享：校验、中位数、规范编码、JournalData
methods/
  build.rs                 risc0_build::embed_methods() 编译两个 guest
  src/lib.rs               生成 MEDIAN_GUEST_{ELF,ID} / DECOY_GUEST_{ELF,ID}
  guest/
    median_guest/          真正的范围校验+中位数程序
    decoy_guest/           另一个不同 image ID 的程序（用于“换程序 ID”测试）
host/
  src/io.rs                输入解析、宿主侧 SHA-256 承诺、计时结构
  src/verify.rs            安全核心：拒 Fake、dev-mode-off、image ID、绑定
  src/pipeline.rs          执行 / 证明 / 验证三阶段分离计时
  src/main.rs              CLI：demo|prove|verify|execute
  tests/host_security.rs   快速测试（dev mode 执行 + 全部安全属性）
  tests/real_proof.rs      真实 STARK 端到端测试（#[ignore]，按需运行）
inputs/                    示例输入（合法/边界/越界/空/6项/256项）
```

---

## 3. 环境准备

需要 Rust 与 RISC Zero 工具链。本机已通过 `rzup` 安装（版本与本仓库一致）：

```bash
# 若机器尚未安装 RISC Zero（已安装可跳过）
cargo install cargo-binstall   # 或参考 https://dev.risczero.com
cargo binstall cargo-risczero
cargo risczero install --version 3.0.6
# 校验：
r0vm --version          # risc0-r0vm 3.0.6
rzup --version          # 0.5.x
```

`cargo risczero install` 会安装 guest 编译所需的 Rust 工具链与 `r0vm`。
构建 guest 时 `risc0-build` 会自动使用它（目标 `riscv32im-risc0-zkvm-elf`）。

首次构建会编译较多依赖（含两个 guest），请预留几分钟与数 GB 磁盘。

---

## 4. 本地启动与验收命令

所有命令在仓库根目录执行。

### 4.1 一键真实演示（推荐先跑这个）

```bash
# 一条命令跑完全部正/负向真实场景（含多次真实证明，约 1 分钟）
./scripts/acceptance.sh

# 或单独跑真实执行 + 真实 STARK 证明 + 严格验证
cargo run --release -- demo --json
```

成功输出包含三段独立耗时与已验证结果，例如：

```json
{
  "verified": true,
  "image_id": "…(MEDIAN_GUEST_ID)…",
  "receipt_kind": "composite",
  "seal_bytes": 1234567,
  "length": 9,
  "median": 5,
  "input_commitment": "…sha256…",
  "timings": { "execute_ms": 12, "prove_ms": 180000, "verify_ms": 90 },
  "segments": 1,
  "total_cycles": 123456,
  "user_cycles": 60000,
  "receipt_path": "receipts/latest.bin"
}
```

人类可读格式（默认）：

```bash
cargo run --release -- demo
```

### 4.2 分阶段使用

```bash
# 仅执行（不证明）：快速看周期数、退出码
cargo run --release -- execute --input inputs/example.json

# 证明并保存收据（receipts/latest.bin）
cargo run --release -- prove --input inputs/boundary_values.json \
    --receipt receipts/boundary.bin

# 重新独立验证已保存的收据（钉住 MEDIAN_GUEST_ID，并重绑输入）
cargo run --release -- verify --receipt receipts/boundary.bin \
    --bind inputs/boundary_values.json
```

### 4.3 安全负向验收（必须全部失败/被拒）

```bash
# (1) dev mode 假收据：能证明，但宿主必须拒绝，退出码非 0
cargo run --release -- demo --dev-mode
#   → 报错含 "REFUSED: receipt is a dev-mode FAKE receipt"

# (2) 换程序 ID：先为“另一个程序”（decoy）生成一张真实收据，
#     然后用 MEDIAN 程序 ID 去验证它 —— 必须被拒。
cargo run --release -- demo --guest decoy \
    --input inputs/example.json --receipt receipts/decoy.bin
cargo run --release -- verify --receipt receipts/decoy.bin \
    --bind inputs/example.json
#   → 默认钉住 MEDIAN_GUEST_ID，与 decoy 镜像不符，验证失败
# 反过来看：同一张 decoy 收据在它自己的程序 ID 下可以通过
cargo run --release -- verify --receipt receipts/decoy.bin \
    --bind inputs/example.json --expect-decoy   # OK

# (3) 篡改公开日志（flip 一个字节）→ 验证失败
cargo run --release -- verify --receipt receipts/latest.bin \
    --bind inputs/example.json --tamper-journal

# (4) 超范围输入：guest 直接故障，无收据
cargo run --release -- execute --input inputs/out_of_range.json   # 失败
cargo run --release -- prove   --input inputs/out_of_range.json   # 失败

# (5) 空批次：guest 直接故障，无收据
cargo run --release -- execute --input inputs/empty.json          # 失败
```

> 注意：`--expect-decoy` 是“要求 decoy 程序 ID”。把它用于 median 收据，
> 或把默认的 `MEDIAN_GUEST_ID` 用于 decoy 收据，都会因 image-ID 不一致被拒。

### 4.4 自动化测试

```bash
# 快速套件：单元逻辑 + guest 执行(dev mode) + 全部安全属性（秒级）
cargo test --release

# 真实密码学套件：真正生成 composite STARK 并验证（CPU 较慢，默认 ignore）
cargo test --release --test real_proof -- --ignored --nocapture
```

快速套件覆盖：下中位数规则、边界/越界、空批次、超长（257）、guest 内范围
强制、**dev-mode 假收据被拒（即便 `RISC0_DEV_MODE=1`）**、**换程序 ID 被拒**、
篡改日志被拒、输入承诺不一致被拒、坏批次永远拿不到收据。

真实套件额外覆盖：真实 seal 非 Fake 且 `seal_size>0`、真实证明的 image-ID
钉扎、真实 seal 下篡改日志（`JournalDigestMismatch`）、越界/空/257 项、
256 项满批。

---

## 5. 三阶段耗时说明

`pipeline.rs` 用 `Instant` 分别测量：

| 阶段 | 内容 | 是否含密码学 |
|------|------|--------------|
| execute | 仅在 zkVM 执行，生成执行轨迹、周期数、退出码 | 否 |
| prove   | 生成 composite STARK 收据（dev mode 时为零密码学 Fake，会被拒） | 是 |
| verify  | 宿主验证 seal + image ID + 解码 journal + 重算输入承诺 | 是（验证明文快） |

真实证明远慢于执行与验证（STARK 的典型成本结构）。dev mode 下 prove 几乎
瞬时，但 `receipt_kind` 会显示 `fake …`，且严格验证必定拒绝。

---

## 6. 复现性与锁定依赖

- `Cargo.lock` 已提交，固定 host 与 guest 的全部依赖版本（RISC Zero 3.0.6
  及其电路 crate）。
- guest 由 `risc0-build` 在构建时针对 `riscv32im-risc0-zkvm-elf` 编译；
  其工具链版本由 `rzup` 固定（本仓库使用 `v1.97.0` 的 RISC Zero rustc、
  `cargo-risczero/r0vm 3.0.6`，见 `~/.risc0/settings.toml`）。
- 输入承诺使用标准 FIPS 180-4 SHA-256（guest 用 zkVM 加速实现，宿主用
  RustCrypto `sha2`），同一批输入两边摘要必然一致。

建议：`cargo build --release` 一次后保存 `target/` 或使用相同基础镜像，
以获得字节级可复现；逻辑级复现由 `Cargo.lock` + 固定工具链保证。

---

## 7. 安全模型与“失败如实报告”

- 宿主**只在**四重检查全部通过后构造 `VerifiedOutput { verified: true }`；
  任何一步失败都返回 `Err` 且进程退出码非 0，绝不返回带结果的“成功”。
- `InnerReceipt::Fake` 无论环境变量如何都被拒绝；验证上下文显式
  `with_dev_mode(false)`。
- 输入侧的宿主预检只是体验优化；范围/长度规则由 **guest 重新独立强制**，
  因此即使宿主被改，坏输入也只会导致 guest 故障而非产生收据。
- 真实证明失败（如电路错误、资源不足）会以 `proving FAILED` 的原始错误
  上报，不被吞掉、不回退到 dev mode。

### 加固选项（编译期彻底关闭 dev mode）

在 `host/Cargo.toml` 为 `risc0-zkvm` 开启 `disable-dev-mode` feature：

```toml
risc0-zkvm = { version = "^3.0.6", default-features = false,
               features = ["client", "std", "disable-dev-mode"] }
```

开启后，设置 `RISC0_DEV_MODE` 会直接 panic，假收据更无可能。本仓库默认通过
运行时严格检查拒绝，以便保留 dev-mode 用于测试与教学演示。
