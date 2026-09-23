//! 执行收据：一次 WASM 调用的完整、可复现记录。

use serde::{Deserialize, Serialize};

/// 执行终态。只有 `Success` 会发布事务缓冲；其余状态一律回滚。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecStatus {
    /// 模块 run() 返回 0，事务已原子发布。
    Success,
    /// 模块 run() 返回非零（业务中止），缓冲丢弃。
    ModuleAbort,
    /// 燃料耗尽（包括 Wasm 指令燃料与宿主调用计费），缓冲丢弃。
    OutOfFuel,
    /// 线性内存/表增长超过本次调用的内存上限，缓冲丢弃。
    MemoryLimitExceeded,
    /// WASM 陷阱（越界访问、unreachable、整数除零等），缓冲丢弃。
    Trap,
}

impl ExecStatus {
    pub fn committed(self) -> bool {
        matches!(self, Self::Success)
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::ModuleAbort => "module_abort",
            Self::OutOfFuel => "out_of_fuel",
            Self::MemoryLimitExceeded => "memory_limit_exceeded",
            Self::Trap => "trap",
        }
    }
}

/// 一条宿主调用的可解释计费明细。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChargeItem {
    /// 第几次宿主调用（从 0 开始）。
    pub seq: usize,
    /// 白名单函数名，如 "kv_put"。
    pub host_fn: String,
    /// 计费单位（字节数或次数）。
    pub units: u64,
    /// 该计量版本下的单价。
    pub unit_price_fuel: u64,
    /// 小计燃料。
    pub fuel: u64,
}

/// 一次执行的确定性收据。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Receipt {
    /// SHA-256(wasm 字节)，hex。
    pub module_sha256: String,
    /// 输入的 SHA-256，hex（空输入为 SHA-256("")）。
    pub input_sha256: String,
    /// 计量版本号（1/2）。
    pub metering_version: u32,
    /// 计量版本名称。
    pub metering_version_name: String,
    /// 固化的 Wasmtime 版本。
    pub wasmtime_version: String,
    /// 本次调用允许的燃料上限。
    pub fuel_limit: u64,
    /// 线性内存上限（字节）。
    pub memory_limit_bytes: u64,
    /// 表元素上限。
    pub table_limit_elements: u32,
    /// 执行终态。
    pub status: ExecStatus,
    /// 终止/中止的人类可读原因（成功时为 "ok"）。
    pub termination_reason: String,
    /// Wasm 指令消耗的燃料（不含宿主分项）。
    pub wasm_fuel_consumed: u64,
    /// 宿主调用计费总额。
    pub host_fuel_consumed: u64,
    /// 总消耗 = wasm + host。
    pub total_fuel_consumed: u64,
    /// 剩余燃料。
    pub fuel_remaining: u64,
    /// 宿主计费明细，按调用顺序。
    pub charges: Vec<ChargeItem>,
    /// 模块经 gas::output_write 产出的输出（base64）。
    pub output_base64: String,
    /// 发布到宿主 KV 的条目（仅 success 时非空）。
    pub committed_writes: Vec<(String, String)>,
    /// 是否发布了事务缓冲。
    pub committed: bool,
    /// 对确定性结果的承诺：对 (status, output, charges, fuel) 的摘要，
    /// 相同输入+版本重复执行时必须相同。
    pub result_hash: String,
}
