//! WASM 离线沙箱执行引擎。
//!
//! 安全边界：
//!   * Linker 中**只**注册 4 个白名单宿主函数（kv_get / kv_put / sha256 / abort），
//!     不链接 WASI、不注册任何 fd/socket/时钟/随机数接口，
//!     因此模块没有网络、文件系统、环境变量或 wall-clock 的能力；
//!     导入任何白名单外符号都会在实例化阶段以 link_error 失败。
//!   * 资源限制：Wasmtime fuel（CPU/指令）+ [`StoreLimits`]（线性内存、表）。
//!   * 宿主写入先进入 [`Overlay`] 事务缓冲；仅当 guest 正常返回且输出可解码时，
//!     才把差异应用到提交态并原子落盘。任何终止路径都直接丢弃覆盖层。

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, RwLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use wasmtime::{
    AsContext, Caller, Config, Engine as WtEngine, Extern, Linker, Memory, Module, OptLevel,
    ResourceLimiter, Store, TypedFunc,
};

use crate::error::{Error, Result, TerminationKind};
use crate::schedule::{Schedule, VersionRegistry};
use crate::state::{CommittedState, Journal};

// ---------------------------------------------------------------------------
// 请求 / 响应
// ---------------------------------------------------------------------------

/// 一次执行请求。
#[derive(Debug, Clone)]
pub struct ExecRequest {
    /// 原始 WASM 字节。
    pub module: Vec<u8>,
    /// 传给 guest `run` 的输入字节。
    pub input: Vec<u8>,
    /// 计量版本号。
    pub metering_version: u32,
    /// 可选燃料上限覆盖（不得超过版本表上限）。
    pub fuel_limit_override: Option<u64>,
    /// 幂等键：相同键的重复请求直接返回首次结果，不重复执行、不重复发布。
    pub idempotency_key: Option<String>,
}

/// 燃料账单（沙箱资源单位，**不是链 Gas**）。
#[derive(Debug, Clone, serde::Serialize)]
pub struct FuelReport {
    /// 执行前注入的燃料上限。
    pub limit: u64,
    /// 总消耗（= limit - 剩余）。
    pub consumed_total: u64,
    /// 执行结束时剩余燃料。
    pub remaining: u64,
    /// 归属到 WASM 指令执行的燃料。
    pub wasm_instruction_fuel: u64,
    /// 归属到宿主函数的额外燃料（各收费项之和）。
    pub host_call_fuel: u64,
    /// 按收费项拆分的宿主燃料，便于解释账单来源。
    pub host_call_breakdown: BTreeMap<String, u64>,
    /// 计量单位声明。
    pub unit: String,
    pub disclaimer: String,
}

/// 一次成功发布产生的差异。
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct DiffReport {
    /// upsert 的键（值以 base64 返回）。
    pub upserts: BTreeMap<String, String>,
    pub deletes: Vec<String>,
}

/// 执行结果（成功与失败都用同一结构如实描述）。
#[derive(Debug, Clone, serde::Serialize)]
pub struct ExecResponse {
    /// committed = 成功且状态已原子发布；terminated = 被终止/失败，未发布任何状态。
    pub status: String,
    /// 成功时为 None；终止时给出机器可读类别。
    pub termination: Option<TerminationKind>,
    /// 终止/拒绝的人类可读原因。
    pub error: Option<String>,

    pub module_sha256: String,
    pub module_size: usize,
    pub input_sha256: String,
    pub input_size: usize,
    pub metering_version: u32,
    pub metering_version_name: String,

    pub state_seq_before: u64,
    pub state_seq_after: Option<u64>,

    pub fuel: FuelReport,
    /// 执行期间观察到的线性内存峰值（字节）。
    pub peak_memory_bytes: usize,
    /// 内存硬上限（字节）。
    pub memory_limit_bytes: usize,

    /// guest 返回值（base64）。
    pub output: Option<String>,
    pub output_size: Option<usize>,

    pub diff: DiffReport,

    /// 确定性键：sha256(版本 | 模块摘要 | 输入摘要 | 燃料上限)。
    /// 相同确定性键 + 相同提交态基线必须产生相同结果与相同燃料账单。
    pub determinism_key: String,
    pub idempotency_key: Option<String>,

    /// 计时字段仅用于运维观测，不参与确定性比较。
    pub started_unix_ms: u128,
    pub duration_ms: u128,

    #[serde(skip)]
    pub journal_index: u64,
}

/// 日志条目（即 ExecResponse，单独别名便于 state 模块引用）。
pub type JournalEntry = ExecResponse;

// ---------------------------------------------------------------------------
// Store 内的宿主状态：资源限制器 + 事务覆盖层 + 账单
// ---------------------------------------------------------------------------

struct StoreLimits {
    memory_max: usize,
    table_max: u32,
    peak_memory: usize,
}

impl ResourceLimiter for StoreLimits {
    fn memory_growing(
        &mut self,
        _current: usize,
        desired: usize,
        _maximum: Option<usize>,
    ) -> anyhow::Result<bool> {
        if desired > self.memory_max {
            anyhow::bail!(
                "memory limit exceeded: growth to {desired} bytes, limit {} bytes",
                self.memory_max
            );
        }
        self.peak_memory = self.peak_memory.max(desired);
        Ok(true)
    }

    fn table_growing(
        &mut self,
        _current: u32,
        desired: u32,
        _maximum: Option<u32>,
    ) -> anyhow::Result<bool> {
        if desired > self.table_max {
            anyhow::bail!(
                "table limit exceeded: growth to {desired} elements, limit {} elements",
                self.table_max
            );
        }
        Ok(true)
    }
}

/// 单次执行的事务缓冲（执行结束即随 Store 一起销毁）。
struct Overlay {
    /// 执行开始时的提交态快照（读基线）。
    base: BTreeMap<String, Vec<u8>>,
    puts: BTreeMap<String, Vec<u8>>,
    deletes: HashSet<String>,
}

impl Overlay {
    fn new(base: BTreeMap<String, Vec<u8>>) -> Self {
        Self {
            base,
            puts: BTreeMap::new(),
            deletes: HashSet::new(),
        }
    }

    fn get(&self, key: &str) -> Option<&[u8]> {
        if self.deletes.contains(key) {
            return None;
        }
        match self.puts.get(key) {
            Some(v) => Some(v.as_slice()),
            None => self.base.get(key).map(|v| v.as_slice()),
        }
    }
}

struct HostState {
    schedule: Schedule,
    overlay: Overlay,
    limits: StoreLimits,
    /// 已实际从 Wasmtime 燃料池扣减的宿主费用累计。
    host_fuel_charged: u64,
    breakdown: BTreeMap<String, u64>,
}

impl HostState {
    fn note(&mut self, item: &str, amount: u64) {
        self.host_fuel_charged = self.host_fuel_charged.saturating_add(amount);
        *self.breakdown.entry(item.to_string()).or_default() += amount;
    }
}

// ---------------------------------------------------------------------------
// 宿主函数辅助
// ---------------------------------------------------------------------------

fn exported_memory(caller: &mut Caller<'_, HostState>) -> anyhow::Result<Memory> {
    match caller.get_export("memory") {
        Some(Extern::Memory(m)) => Ok(m),
        _ => Err(anyhow::anyhow!("guest 缺少 memory 导出")),
    }
}

/// 校验 [ptr, ptr+len) 完全落在 guest 线性内存内，否则越界陷阱。
fn check_range(memory: &Memory, ctx: &impl AsContext, ptr: u32, len: u32) -> anyhow::Result<()> {
    let start = ptr as usize;
    let end = start
        .checked_add(len as usize)
        .ok_or(wasmtime::Trap::MemoryOutOfBounds)?;
    if end > memory.data_size(ctx) {
        return Err(wasmtime::Trap::MemoryOutOfBounds.into());
    }
    Ok(())
}

/// 从 Wasmtime 燃料池扣减宿主费用。记录的是**实际从池中取走**的量，
/// 以保证账单恒等式 consumed_total = wasm_instruction_fuel + host_call_fuel。
/// 余额不足时把池清零并抛 out-of-fuel 陷阱。
fn charge_fuel(caller: &mut Caller<'_, HostState>, item: &str, amount: u64) -> anyhow::Result<()> {
    if amount == 0 {
        return Ok(());
    }
    let remaining = caller.get_fuel()?;
    let take = amount.min(remaining);
    caller.set_fuel(remaining - take)?;
    caller.data_mut().note(item, take);
    if amount > remaining {
        return Err(wasmtime::Trap::OutOfFuel.into());
    }
    Ok(())
}

/// guest 违反 ABI 契约（键非 UTF-8、长度超版本限制等）：以陷阱终止，不发布。
fn contract_violation(msg: impl Into<String>) -> anyhow::Error {
    anyhow::Error::new(wasmtime::Trap::UnreachableCodeReached).context(msg.into())
}

// ---------------------------------------------------------------------------
// 白名单宿主函数
// ---------------------------------------------------------------------------

fn host_kv_put(
    mut caller: Caller<'_, HostState>,
    key_ptr: i32,
    key_len: i32,
    val_ptr: i32,
    val_len: i32,
) -> anyhow::Result<()> {
    let memory = exported_memory(&mut caller)?;
    let (key_max, val_max, call_base, put_base, per_byte) = {
        let s = &caller.data().schedule;
        (
            s.kv_key_max_bytes,
            s.kv_value_max_bytes,
            s.host.call_base,
            s.host.kv_put_base,
            s.host.kv_put_per_byte,
        )
    };

    if key_len < 0 || val_len < 0 {
        return Err(wasmtime::Trap::MemoryOutOfBounds.into());
    }
    if key_len as usize > key_max {
        return Err(contract_violation(format!(
            "kv_put 键长度 {key_len} 超过版本上限 {key_max}"
        )));
    }
    if val_len as usize > val_max {
        return Err(contract_violation(format!(
            "kv_put 值长度 {val_len} 超过版本上限 {val_max}"
        )));
    }
    check_range(&memory, &caller, key_ptr as u32, key_len as u32)?;
    check_range(&memory, &caller, val_ptr as u32, val_len as u32)?;

    let mut key = vec![0u8; key_len as usize];
    memory.read(&caller, key_ptr as usize, &mut key)?;
    let mut value = vec![0u8; val_len as usize];
    memory.read(&caller, val_ptr as usize, &mut value)?;
    let key = String::from_utf8(key).map_err(|_| contract_violation("kv_put 键必须为 UTF-8"))?;

    // 先扣费（可能在此终止），再缓冲写入。
    charge_fuel(&mut caller, "call_base", call_base)?;
    charge_fuel(
        &mut caller,
        "kv_put",
        put_base + ((key_len as u64 + val_len as u64) * per_byte),
    )?;

    caller.data_mut().overlay.deletes.remove(&key);
    caller.data_mut().overlay.puts.insert(key, value);
    Ok(())
}

fn host_kv_get(
    mut caller: Caller<'_, HostState>,
    key_ptr: i32,
    key_len: i32,
    out_ptr: i32,
    out_cap: i32,
) -> anyhow::Result<i32> {
    let memory = exported_memory(&mut caller)?;
    let (key_max, val_max, call_base, get_base, per_byte) = {
        let s = &caller.data().schedule;
        (
            s.kv_key_max_bytes,
            s.kv_value_max_bytes,
            s.host.call_base,
            s.host.kv_get_base,
            s.host.kv_get_per_byte,
        )
    };

    if key_len < 0 || out_cap < 0 {
        return Err(wasmtime::Trap::MemoryOutOfBounds.into());
    }
    if key_len as usize > key_max {
        return Err(contract_violation(format!(
            "kv_get 键长度 {key_len} 超过版本上限 {key_max}"
        )));
    }
    if out_cap as usize > val_max + 1 {
        return Err(contract_violation(format!(
            "kv_get 输出缓冲 {out_cap} 超过版本值上限 {val_max}"
        )));
    }
    check_range(&memory, &caller, key_ptr as u32, key_len as u32)?;
    check_range(&memory, &caller, out_ptr as u32, out_cap as u32)?;

    let mut key = vec![0u8; key_len as usize];
    memory.read(&caller, key_ptr as usize, &mut key)?;

    // 未命中：仅收调用基础费，返回 -1。
    let value: Vec<u8> = {
        let h = caller.data();
        match String::from_utf8(key)
            .ok()
            .and_then(|k| h.overlay.get(&k).map(|v| v.to_vec()))
        {
            Some(v) => v,
            None => {
                charge_fuel(&mut caller, "call_base", call_base)?;
                charge_fuel(&mut caller, "kv_get_miss", get_base)?;
                return Ok(-1);
            }
        }
    };
    if value.len() > out_cap as usize {
        // 缓冲区不足：不写入，按未命中处理。
        charge_fuel(&mut caller, "call_base", call_base)?;
        charge_fuel(&mut caller, "kv_get_short", get_base)?;
        return Ok(-1);
    }

    charge_fuel(&mut caller, "call_base", call_base)?;
    charge_fuel(
        &mut caller,
        "kv_get",
        get_base + ((key_len as u64 + value.len() as u64) * per_byte),
    )?;
    memory.write(&mut caller, out_ptr as usize, &value)?;
    Ok(value.len() as i32)
}

fn host_sha256(
    mut caller: Caller<'_, HostState>,
    in_ptr: i32,
    in_len: i32,
    out_ptr: i32,
) -> anyhow::Result<()> {
    let memory = exported_memory(&mut caller)?;
    let (val_max, call_base, hash_base, per_byte) = {
        let s = &caller.data().schedule;
        (
            s.kv_value_max_bytes,
            s.host.call_base,
            s.host.sha256_base,
            s.host.sha256_per_byte,
        )
    };
    if in_len < 0 {
        return Err(wasmtime::Trap::MemoryOutOfBounds.into());
    }
    if in_len as usize > val_max {
        return Err(contract_violation(format!(
            "sha256 输入 {in_len} 超过版本上限 {val_max}"
        )));
    }
    check_range(&memory, &caller, in_ptr as u32, in_len as u32)?;
    // 输出必须可容纳 32 字节摘要。
    check_range(&memory, &caller, out_ptr as u32, 32)?;

    let mut input = vec![0u8; in_len as usize];
    memory.read(&caller, in_ptr as usize, &mut input)?;

    charge_fuel(&mut caller, "call_base", call_base)?;
    charge_fuel(
        &mut caller,
        "sha256",
        hash_base + (in_len as u64 * per_byte),
    )?;

    // 真实密码学运算（sha2 crate），绝非桩实现。
    let digest = Sha256::digest(&input);
    memory.write(&mut caller, out_ptr as usize, &digest)?;
    Ok(())
}

fn host_abort(
    mut caller: Caller<'_, HostState>,
    _msg_ptr: i32,
    _msg_len: i32,
) -> anyhow::Result<()> {
    let call_base = caller.data().schedule.host.call_base;
    // abort 本身不额外收费；基础调用费仍记账（池中通常仍有余额，忽略失败）。
    if call_base > 0 {
        let remaining = caller.get_fuel()?;
        let take = call_base.min(remaining);
        caller.set_fuel(remaining - take)?;
        caller.data_mut().note("call_base", take);
    }
    Err(wasmtime::Trap::UnreachableCodeReached.into())
}

/// 构建只含白名单函数的 Linker。
/// 白名单：env.kv_get / env.kv_put / env.sha256 / env.abort。无其它任何能力。
fn build_linker(engine: &WtEngine) -> Linker<HostState> {
    let mut linker = Linker::new(engine);
    linker
        .func_wrap("env", "kv_put", host_kv_put)
        .expect("register kv_put");
    linker
        .func_wrap("env", "kv_get", host_kv_get)
        .expect("register kv_get");
    linker
        .func_wrap("env", "sha256", host_sha256)
        .expect("register sha256");
    linker
        .func_wrap("env", "abort", host_abort)
        .expect("register abort");
    linker
}

// ---------------------------------------------------------------------------
// guest ABI
// ---------------------------------------------------------------------------

struct Abi {
    memory: Memory,
    alloc: TypedFunc<i32, i32>,
    run: TypedFunc<(i32, i32), i64>,
}

fn check_abi(instance: &wasmtime::Instance, store: &mut Store<HostState>) -> Result<Abi> {
    let reject = |m: &str| Error::new(TerminationKind::ModuleRejected, m);

    let memory = instance
        .get_memory(&mut *store, "memory")
        .ok_or_else(|| reject("模块必须导出名为 `memory` 的线性内存"))?;
    let alloc = instance
        .get_typed_func::<i32, i32>(&mut *store, "alloc")
        .map_err(|_| reject("模块必须导出 alloc(i32) -> i32"))?;
    let run = instance
        .get_typed_func::<(i32, i32), i64>(&mut *store, "run")
        .map_err(|_| reject("模块必须导出 run(i32, i32) -> i64"))?;
    Ok(Abi { memory, alloc, run })
}

// ---------------------------------------------------------------------------
// 引擎本体
// ---------------------------------------------------------------------------

pub struct Engine {
    engine: WtEngine,
    registry: VersionRegistry,
    state: Arc<CommittedState>,
    journal: Arc<Journal>,
    idempotency: RwLock<HashMap<String, ExecResponse>>,
}

const FUEL_UNIT: &str = "wasmtime-fuel-unit";
const FUEL_DISCLAIMER: &str =
    "燃料是本沙箱的确定性资源单位（多数 WASM 指令=1，宿主调用按版本表附加），\
不是任何区块链的真实 Gas；不得据此冒称链上费用。";

fn make_determinism_key(
    version: u32,
    module_sha: &str,
    input_sha: &str,
    fuel_limit: u64,
) -> String {
    let mut h = Sha256::new();
    Digest::update(&mut h, version.to_string());
    Digest::update(&mut h, b"|");
    Digest::update(&mut h, module_sha);
    Digest::update(&mut h, b"|");
    Digest::update(&mut h, input_sha);
    Digest::update(&mut h, b"|");
    Digest::update(&mut h, fuel_limit.to_string());
    hex::encode(h.finalize())
}

impl Engine {
    pub fn new(
        registry: VersionRegistry,
        state: Arc<CommittedState>,
        journal: Arc<Journal>,
    ) -> anyhow::Result<Self> {
        let mut config = Config::new();
        config.consume_fuel(true);
        // 固定优化级别：编译/计量只依赖 Cargo.lock 锁定的 wasmtime 版本与给定配置。
        config.cranelift_opt_level(OptLevel::Speed);
        // 不启用 epoch 中断、不启用 WASI；终止只依赖确定性的 fuel 与 limiter。
        let engine = WtEngine::new(&config)?;
        Ok(Self {
            engine,
            registry,
            state,
            journal,
            idempotency: RwLock::new(HashMap::new()),
        })
    }

    pub fn registry(&self) -> &VersionRegistry {
        &self.registry
    }

    /// 执行（或命中幂等缓存直接返回首次结果）。
    pub fn execute(&self, req: ExecRequest) -> ExecResponse {
        if let Some(key) = &req.idempotency_key {
            if let Some(cached) = self.idempotency.read().unwrap().get(key) {
                return cached.clone();
            }
        }
        let resp = self.run_inner(&req);
        if let Some(key) = &req.idempotency_key {
            self.idempotency
                .write()
                .unwrap()
                .insert(key.clone(), resp.clone());
        }
        self.journal.append(resp.clone());
        resp
    }

    #[allow(clippy::too_many_arguments)]
    fn run_inner(&self, req: &ExecRequest) -> ExecResponse {
        let started = Instant::now();
        let started_unix_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);

        let module_sha = hex::encode(Sha256::digest(&req.module));
        let input_sha = hex::encode(Sha256::digest(&req.input));

        let schedule = match self.registry.get(req.metering_version) {
            Some(s) => s.clone(),
            None => {
                let available = self
                    .registry
                    .all()
                    .iter()
                    .map(|s| s.version)
                    .collect::<Vec<_>>();
                let fallback = self
                    .registry
                    .get(self.registry.current_version())
                    .unwrap()
                    .clone();
                return self.failure(
                    None,
                    &fallback,
                    &module_sha,
                    &input_sha,
                    req,
                    0,
                    0,
                    TerminationKind::BadRequest,
                    format!(
                        "未知计量版本 {}，可用版本：{available:?}",
                        req.metering_version
                    ),
                    started_unix_ms,
                    started,
                );
            }
        };

        if req.module.len() > schedule.module_max_bytes {
            return self.failure(
                None,
                &schedule,
                &module_sha,
                &input_sha,
                req,
                0,
                0,
                TerminationKind::BadRequest,
                format!(
                    "模块 {} 字节超过上限 {} 字节",
                    req.module.len(),
                    schedule.module_max_bytes
                ),
                started_unix_ms,
                started,
            );
        }
        if req.input.len() > schedule.input_max_bytes {
            return self.failure(
                None,
                &schedule,
                &module_sha,
                &input_sha,
                req,
                0,
                0,
                TerminationKind::BadRequest,
                format!(
                    "输入 {} 字节超过上限 {} 字节",
                    req.input.len(),
                    schedule.input_max_bytes
                ),
                started_unix_ms,
                started,
            );
        }
        let fuel_limit = match req.fuel_limit_override {
            Some(f) if f <= schedule.fuel_limit => f,
            Some(f) => {
                return self.failure(
                    None,
                    &schedule,
                    &module_sha,
                    &input_sha,
                    req,
                    0,
                    0,
                    TerminationKind::BadRequest,
                    format!("燃料覆盖值 {f} 超过版本上限 {}", schedule.fuel_limit),
                    started_unix_ms,
                    started,
                );
            }
            None => schedule.fuel_limit,
        };

        let (seq_before, snapshot) = self.state.snapshot();

        // 编译（失败不产生 Store）。Module::new 只做解析+校验，
        // 任何错误都属于「模块字节不合法/不受支持」，统一归类 compile_error。
        let module = match Module::new(&self.engine, &req.module) {
            Ok(m) => m,
            Err(e) => {
                let c = Error::new(TerminationKind::CompileError, format!("{e:#}"));
                return self.failure(
                    None,
                    &schedule,
                    &module_sha,
                    &input_sha,
                    req,
                    fuel_limit,
                    seq_before,
                    c.kind,
                    c.message,
                    started_unix_ms,
                    started,
                );
            }
        };

        let host = HostState {
            schedule: schedule.clone(),
            overlay: Overlay::new(snapshot),
            limits: StoreLimits {
                memory_max: schedule.memory_max_bytes,
                table_max: schedule.table_max_elements,
                peak_memory: 0,
            },
            host_fuel_charged: 0,
            breakdown: BTreeMap::new(),
        };
        let mut store = Store::new(&self.engine, host);
        store.limiter(|h| &mut h.limits);
        store
            .set_fuel(fuel_limit)
            .expect("fuel 已在 Config::consume_fuel(true) 中启用");

        let linker = build_linker(&self.engine);

        // 实例化（wasmtime 会执行模块 start 函数）：
        // 任何白名单外导入在此以 link_error 失败，模块体不会运行。
        let instance = match linker.instantiate(&mut store, &module) {
            Ok(i) => i,
            Err(e) => {
                let c = Error::from_anyhow(&e);
                return self.failure(
                    Some(&mut store),
                    &schedule,
                    &module_sha,
                    &input_sha,
                    req,
                    fuel_limit,
                    seq_before,
                    c.kind,
                    c.message,
                    started_unix_ms,
                    started,
                );
            }
        };

        let abi = match check_abi(&instance, &mut store) {
            Ok(a) => a,
            Err(e) => {
                return self.failure(
                    Some(&mut store),
                    &schedule,
                    &module_sha,
                    &input_sha,
                    req,
                    fuel_limit,
                    seq_before,
                    e.kind,
                    e.message,
                    started_unix_ms,
                    started,
                );
            }
        };

        let initial_mem = abi.memory.data_size(&store);
        store.data_mut().limits.peak_memory = initial_mem;

        // 通过 guest 的 alloc 申请输入缓冲。
        let input_len = req.input.len() as i32;
        let in_ptr = match abi.alloc.call(&mut store, input_len) {
            Ok(p) => p as u32,
            Err(e) => {
                let c = Error::from_anyhow(&e);
                return self.failure(
                    Some(&mut store),
                    &schedule,
                    &module_sha,
                    &input_sha,
                    req,
                    fuel_limit,
                    seq_before,
                    c.kind,
                    c.message,
                    started_unix_ms,
                    started,
                );
            }
        };
        // alloc 返回的指针可能是恶意值：写入前先做显式边界校验，
        // 越界归类为 memory_out_of_bounds（而不是宿主机内部错误）。
        if let Err(e) = check_range(&abi.memory, &store, in_ptr, req.input.len() as u32) {
            let c = Error::from_anyhow(&e);
            return self.failure(
                Some(&mut store),
                &schedule,
                &module_sha,
                &input_sha,
                req,
                fuel_limit,
                seq_before,
                c.kind,
                c.message,
                started_unix_ms,
                started,
            );
        }
        if let Err(e) = abi.memory.write(&mut store, in_ptr as usize, &req.input) {
            let c = Error::from_std(e);
            return self.failure(
                Some(&mut store),
                &schedule,
                &module_sha,
                &input_sha,
                req,
                fuel_limit,
                seq_before,
                c.kind,
                c.message,
                started_unix_ms,
                started,
            );
        }

        // 执行 guest run。
        let packed = match abi.run.call(&mut store, (in_ptr as i32, input_len)) {
            Ok(v) => v,
            Err(e) => {
                let c = Error::from_anyhow(&e);
                return self.failure(
                    Some(&mut store),
                    &schedule,
                    &module_sha,
                    &input_sha,
                    req,
                    fuel_limit,
                    seq_before,
                    c.kind,
                    c.message,
                    started_unix_ms,
                    started,
                );
            }
        };

        // 解码 (ptr << 32) | len，并在提交前校验输出范围。
        let out_ptr = (packed >> 32) as u32;
        let out_len = (packed & 0xffff_ffff) as u32;
        if let Err(e) = check_range(&abi.memory, &store, out_ptr, out_len) {
            let c = Error::from_anyhow(&e);
            return self.failure(
                Some(&mut store),
                &schedule,
                &module_sha,
                &input_sha,
                req,
                fuel_limit,
                seq_before,
                c.kind,
                c.message,
                started_unix_ms,
                started,
            );
        }
        let mut output = vec![0u8; out_len as usize];
        if let Err(e) = abi.memory.read(&store, out_ptr as usize, &mut output) {
            let c = Error::from_std(e);
            return self.failure(
                Some(&mut store),
                &schedule,
                &module_sha,
                &input_sha,
                req,
                fuel_limit,
                seq_before,
                c.kind,
                c.message,
                started_unix_ms,
                started,
            );
        }

        // guest 成功返回：在销毁 Store 前抓取燃料账单与内存峰值。
        let remaining = store.get_fuel().unwrap_or(fuel_limit);
        let consumed_total = fuel_limit - remaining;
        let host_call_fuel = store.data().host_fuel_charged;
        let wasm_instruction_fuel = consumed_total.saturating_sub(host_call_fuel);
        let breakdown = store.data().breakdown.clone();
        let peak_memory = store.data().limits.peak_memory;

        // 取走事务缓冲并原子发布。
        let overlay =
            std::mem::replace(&mut store.data_mut().overlay, Overlay::new(BTreeMap::new()));
        let upserts: BTreeMap<String, Vec<u8>> = overlay.puts;
        let deletes: Vec<String> = overlay.deletes.into_iter().collect();

        let seq_after = match self.state.apply(upserts.clone(), deletes.clone()) {
            Ok(s) => s,
            Err(e) => {
                let c = Error::new(
                    TerminationKind::HostError,
                    format!("状态原子发布失败：{e:#}"),
                );
                // 覆盖层已从 store 取出但发布失败：随栈展开丢弃，提交态未被修改。
                return self.failure(
                    Some(&mut store),
                    &schedule,
                    &module_sha,
                    &input_sha,
                    req,
                    fuel_limit,
                    seq_before,
                    c.kind,
                    c.message,
                    started_unix_ms,
                    started,
                );
            }
        };

        let fuel = FuelReport {
            limit: fuel_limit,
            consumed_total,
            remaining,
            wasm_instruction_fuel,
            host_call_fuel,
            host_call_breakdown: breakdown,
            unit: FUEL_UNIT.into(),
            disclaimer: FUEL_DISCLAIMER.into(),
        };
        let diff = DiffReport {
            upserts: upserts
                .into_iter()
                .map(|(k, v)| (k, crate::base64::encode(&v)))
                .collect(),
            deletes,
        };

        let determinism_key =
            make_determinism_key(schedule.version, &module_sha, &input_sha, fuel_limit);

        ExecResponse {
            status: "committed".into(),
            termination: None,
            error: None,
            module_sha256: module_sha,
            module_size: req.module.len(),
            input_sha256: input_sha,
            input_size: req.input.len(),
            metering_version: schedule.version,
            metering_version_name: schedule.name,
            state_seq_before: seq_before,
            state_seq_after: Some(seq_after),
            fuel,
            peak_memory_bytes: peak_memory,
            memory_limit_bytes: schedule.memory_max_bytes,
            output_size: Some(output.len()),
            output: Some(crate::base64::encode(&output)),
            diff,
            determinism_key,
            idempotency_key: req.idempotency_key.clone(),
            started_unix_ms,
            duration_ms: started.elapsed().as_millis(),
            journal_index: 0,
        }
    }

    /// 构造终止/失败响应。Store 存在时从中读取真实燃料与峰值；不存在则账单归零。
    #[allow(clippy::too_many_arguments)]
    fn failure(
        &self,
        store: Option<&mut Store<HostState>>,
        schedule: &Schedule,
        module_sha: &str,
        input_sha: &str,
        req: &ExecRequest,
        fuel_limit: u64,
        seq_before: u64,
        kind: TerminationKind,
        message: String,
        started_unix_ms: u128,
        started: Instant,
    ) -> ExecResponse {
        let (fuel, peak) = match store {
            Some(st) => {
                let remaining = st.get_fuel().unwrap_or(fuel_limit);
                let consumed_total = fuel_limit.saturating_sub(remaining);
                let host_call_fuel = st.data().host_fuel_charged;
                let wasm_instruction_fuel = consumed_total.saturating_sub(host_call_fuel);
                let peak = st.data().limits.peak_memory;
                (
                    FuelReport {
                        limit: fuel_limit,
                        consumed_total,
                        remaining,
                        wasm_instruction_fuel,
                        host_call_fuel,
                        host_call_breakdown: st.data().breakdown.clone(),
                        unit: FUEL_UNIT.into(),
                        disclaimer: FUEL_DISCLAIMER.into(),
                    },
                    peak,
                )
            }
            None => (
                FuelReport {
                    limit: fuel_limit,
                    consumed_total: 0,
                    remaining: fuel_limit,
                    wasm_instruction_fuel: 0,
                    host_call_fuel: 0,
                    host_call_breakdown: BTreeMap::new(),
                    unit: FUEL_UNIT.into(),
                    disclaimer: FUEL_DISCLAIMER.into(),
                },
                0,
            ),
        };

        ExecResponse {
            status: "terminated".into(),
            termination: Some(kind),
            error: Some(message),
            module_sha256: module_sha.into(),
            module_size: req.module.len(),
            input_sha256: input_sha.into(),
            input_size: req.input.len(),
            metering_version: schedule.version,
            metering_version_name: schedule.name.clone(),
            state_seq_before: seq_before,
            state_seq_after: None,
            fuel,
            peak_memory_bytes: peak,
            memory_limit_bytes: schedule.memory_max_bytes,
            output: None,
            output_size: None,
            diff: DiffReport::default(),
            determinism_key: make_determinism_key(
                schedule.version,
                module_sha,
                input_sha,
                fuel_limit,
            ),
            idempotency_key: req.idempotency_key.clone(),
            started_unix_ms,
            duration_ms: started.elapsed().as_millis(),
            journal_index: 0,
        }
    }
}
