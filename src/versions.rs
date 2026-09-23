//! 计量版本定义。
//!
//! 计量版本是确定性的核心：模块摘要、输入、计量版本三者相同，
//! 就必须产生相同的结果与相同的燃料账目。每个版本固定宿主调用单价、
//! Wasmtime 引擎版本、以及一份人类可读的规则说明。
//!
//! 注意：这里的 fuel 是 Wasmtime 的抽象燃料单位，**不是**任何真实区块链的
//! Gas，也没有提供向链上 Gas 的换算系数。

use std::fmt;
use wasmtime::Engine;

/// 已支持的计量版本。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MeteringVersion {
    /// V1：基础定价，宿主调用统一较低单价。
    V1 = 1,
    /// V2：对状态写入（kv_put）与加密类调用收取更高燃料，
    /// 体现“写放大/成本敏感操作”的定价修正。计算规则变化 => 新版本。
    V2 = 2,
}

impl MeteringVersion {
    pub fn from_u32(v: u32) -> Option<Self> {
        match v {
            1 => Some(Self::V1),
            2 => Some(Self::V2),
            _ => None,
        }
    }

    pub fn as_u32(self) -> u32 {
        self as u32
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::V1 => "v1-baseline-2026-09",
            Self::V2 => "v2-write-weighted-2026-09",
        }
    }

    /// input_read（每字节）
    pub fn input_read_per_byte(self) -> u64 {
        match self {
            Self::V1 => 1,
            Self::V2 => 1,
        }
    }

    /// output_write（每字节）
    pub fn output_write_per_byte(self) -> u64 {
        match self {
            Self::V1 => 1,
            Self::V2 => 2,
        }
    }

    /// kv_get（每字节返回载荷）
    pub fn kv_get_per_byte(self) -> u64 {
        match self {
            Self::V1 => 2,
            Self::V2 => 2,
        }
    }

    /// kv_put（每字节写入值）
    pub fn kv_put_per_byte(self) -> u64 {
        match self {
            Self::V1 => 5,
            Self::V2 => 50,
        }
    }

    /// kv_has（每次调用的固定费用）
    pub fn kv_has_flat(self) -> u64 {
        match self {
            Self::V1 => 20,
            Self::V2 => 20,
        }
    }

    /// 对一次宿主操作的分项计费描述（用于可解释计量）。
    /// 返回 (操作名, 计费数量, 单价, 小计)。
    pub fn describe_charge(self, op: HostOp, units: u64) -> (&'static str, u64, u64, u64) {
        let (name, unit_price) = match op {
            HostOp::InputRead => ("input_read", self.input_read_per_byte()),
            HostOp::OutputWrite => ("output_write", self.output_write_per_byte()),
            HostOp::KvGet => ("kv_get", self.kv_get_per_byte()),
            HostOp::KvPut => ("kv_put", self.kv_put_per_byte()),
            HostOp::KvHas => ("kv_has", self.kv_has_flat()),
        };
        (name, units, unit_price, units.saturating_mul(unit_price))
    }

    /// 该计量版本固化的 Wasmtime 引擎（配置在进程生命周期内不变）。
    pub fn build_engine(self) -> anyhow::Result<Engine> {
        let mut config = wasmtime::Config::new();
        config
            .consume_fuel(true)
            // 确定性：关闭非必要、可能引入额外行为面的提案。
            .wasm_bulk_memory(true)
            .wasm_multi_value(false)
            // 不允许模块使用多内存（单一线性内存便于边界限制）。
            .wasm_multi_memory(false)
            // 关闭 SIMD/引用类型/线程等非必要能力，收窄攻击面。
            .wasm_simd(false)
            .wasm_relaxed_simd(false)
            .wasm_reference_types(false)
            .wasm_threads(false)
            .wasm_tail_call(false)
            .wasm_gc(false)
            // 不启用 epoch interruption 之外的后台行为；
            // fuel 负责终止无限循环，不依赖 wall-clock。
            .cranelift_opt_level(wasmtime::OptLevel::Speed);
        // 默认编译策略即 Cranelift。wasmtime::Error 即 anyhow::Error。
        Engine::new(&config)
    }

    pub fn rules_summary(self) -> serde_json::Value {
        serde_json::json!({
            "version_id": self.as_u32(),
            "name": self.name(),
            "wasmtime": env!("CARGO_PKG_VERSION_WASMTIME"),
            "host_charges_per_unit": {
                "input_read.byte": self.input_read_per_byte(),
                "output_write.byte": self.output_write_per_byte(),
                "kv_get.byte_returned": self.kv_get_per_byte(),
                "kv_put.byte_written": self.kv_put_per_byte(),
                "kv_has.call": self.kv_has_flat(),
            },
            "note": "fuel is Wasmtime abstract metering, NOT blockchain gas; no gas conversion is claimed",
        })
    }
}

/// 宿主可计费操作（仅限白名单内）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostOp {
    InputRead,
    OutputWrite,
    KvGet,
    KvPut,
    KvHas,
}

impl fmt::Display for MeteringVersion {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}({})", self.name(), self.as_u32())
    }
}
