//! 版本化计量表。
//!
//! 每个计量版本（metering version）冻结一组参数：
//!   * Wasmtime 燃料上限（确定性 CPU/指令预算）；
//!   * 线性内存与表上限（字节/元素数）；
//!   * 宿主函数调用的额外燃料收费表（覆盖真实工作量差异，例如 SHA-256 按输入字节计费）。
//!
//! 版本一经发布参数不可修改；新版本以新版本号追加，确保历史交易的计量可复现。
//!
//! 注意：这里的「燃料」是本沙箱的确定性资源单位，**不是任何区块链的真实 Gas**。
//! 转换为链 Gas 需要链方提供经过审计的映射，本服务不做这种冒称。

use std::collections::BTreeMap;

/// 宿主侧收费表（额外从 Wasmtime 燃料池扣除，见 `engine.rs`）。
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct HostCosts {
    /// 每次宿主函数调用的固定开销。
    pub call_base: u64,
    /// kv_put：固定部分。
    pub kv_put_base: u64,
    /// kv_put：每字节（键+值）。
    pub kv_put_per_byte: u64,
    /// kv_get：固定部分。
    pub kv_get_base: u64,
    /// kv_get：每字节（键+拷贝输出）。
    pub kv_get_per_byte: u64,
    /// sha256：固定部分。
    pub sha256_base: u64,
    /// sha256：每输入字节。
    pub sha256_per_byte: u64,
    /// abort：固定部分。
    pub abort_base: u64,
}

/// 一个冻结的计量版本。
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Schedule {
    /// 版本号，如 1。
    pub version: u32,
    /// 人类可读名称。
    pub name: String,
    /// Wasmtime 燃料上限（执行前注入；耗尽即陷阱）。
    pub fuel_limit: u64,
    /// 线性内存硬上限（字节），`memory.grow` 越过即陷阱。
    pub memory_max_bytes: usize,
    /// 表元素硬上限。
    pub table_max_elements: u32,
    /// 单次执行允许的 KV 缓冲键/值大小上限（字节）。
    pub kv_key_max_bytes: usize,
    pub kv_value_max_bytes: usize,
    /// 模块字节上传上限（字节）。
    pub module_max_bytes: usize,
    /// 输入字节上限。
    pub input_max_bytes: usize,
    /// 宿主收费表。
    pub host: HostCosts,
    /// 该版本的解释性说明（响应中原样返回，便于「可解释计量」）。
    pub notes: String,
}

impl Schedule {
    fn v1() -> Self {
        Schedule {
            version: 1,
            name: "2026-01-baseline".into(),
            // 足够有限循环样例完成，又能让无限循环在毫秒级内终止。
            fuel_limit: 20_000_000,
            memory_max_bytes: 4 * 1024 * 1024, // 4 MiB
            table_max_elements: 10_000,
            kv_key_max_bytes: 256,
            kv_value_max_bytes: 64 * 1024,
            module_max_bytes: 4 * 1024 * 1024,
            input_max_bytes: 64 * 1024,
            host: HostCosts {
                call_base: 100,
                kv_put_base: 500,
                kv_put_per_byte: 1,
                kv_get_base: 500,
                kv_get_per_byte: 1,
                sha256_base: 2_000,
                sha256_per_byte: 2,
                abort_base: 0,
            },
            notes: "基线版本：大多数 WASM 指令消耗 1 单位 wasmtime 燃料；\
宿主调用按 call_base + 各项 per_byte 额外扣费。燃料为沙箱资源单位，非链 Gas。"
                .into(),
        }
    }

    fn v2() -> Self {
        // v2 演示「版本化」：宿主收费翻倍、内存上限放宽。
        // 相同模块+输入在 v1/v2 下结果一致但燃料账单不同，且均可解释、可复现。
        let mut s = Self::v1();
        s.version = 2;
        s.name = "2026-06-hostcostx2".into();
        s.memory_max_bytes = 8 * 1024 * 1024;
        s.host.call_base *= 2;
        s.host.kv_put_base *= 2;
        s.host.kv_put_per_byte *= 2;
        s.host.kv_get_base *= 2;
        s.host.kv_get_per_byte *= 2;
        s.host.sha256_base *= 2;
        s.host.sha256_per_byte *= 2;
        s.notes = "宿主调用收费相对 v1 翻倍，内存上限放宽到 8 MiB；\
WASM 指令本身的燃料语义不变。燃料为沙箱资源单位，非链 Gas。"
            .into();
        s
    }
}

/// 全部已发布版本的注册表。
#[derive(Debug, Clone)]
pub struct VersionRegistry {
    current: u32,
    schedules: BTreeMap<u32, Schedule>,
}

impl Default for VersionRegistry {
    fn default() -> Self {
        let mut schedules = BTreeMap::new();
        schedules.insert(1, Schedule::v1());
        schedules.insert(2, Schedule::v2());
        Self {
            current: 1,
            schedules,
        }
    }
}

impl VersionRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn current_version(&self) -> u32 {
        self.current
    }

    pub fn get(&self, version: u32) -> Option<&Schedule> {
        self.schedules.get(&version)
    }

    pub fn all(&self) -> Vec<&Schedule> {
        self.schedules.values().collect()
    }
}
