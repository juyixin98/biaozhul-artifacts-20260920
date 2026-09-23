//! 离线 WASM 执行沙箱。
//!
//! 安全边界：
//! * 不链接 WASI，不提供任何文件/网络/环境变量/时钟能力。
//! * 仅允许模块 import 命名空间 `gas` 下的 5 个白名单函数；
//!   任何其他 import（wasi_snapshot_preview1、env 等）在实例化前拒绝。
//! * 燃料（Wasmtime fuel）耗尽即陷阱，无限循环必然终止。
//! * 线性内存/表增长受每次调用的硬性上限约束。
//! * 宿主写入只进私有事务缓冲；成功（run 返回 0）后才原子发布，
//!   燃料耗尽、越界陷阱、模块中止一律回滚。

use std::collections::BTreeMap;

use anyhow::{anyhow, bail, Context, Result};
use base64::Engine as _;
use wasmtime::{Caller, Engine, Linker, Memory, Module, Store, Trap};

use crate::receipt::{ChargeItem, ExecStatus, Receipt};
use crate::state::HostState;
use crate::versions::{HostOp, MeteringVersion};

/// 宿主 ABI 命名空间。
pub const HOST_NAMESPACE: &str = "gas";

/// 默认燃料上限（Wasmtime 抽象燃料单位，不是链 Gas）。
pub const DEFAULT_FUEL_LIMIT: u64 = 10_000_000;
/// 默认线性内存上限：8 MiB。
pub const DEFAULT_MEMORY_LIMIT: u64 = 8 * 1024 * 1024;
/// 默认表元素上限：10_000。
pub const DEFAULT_TABLE_LIMIT: u32 = 10_000;

/// 允许的 import 白名单：(函数名, 参数/返回签名描述)。
pub const ALLOWED_IMPORTS: &[&str] = &["input_read", "output_write", "kv_get", "kv_put", "kv_has"];

/// 一次执行的参数。
#[derive(Debug, Clone)]
pub struct ExecRequest {
    pub wasm: Vec<u8>,
    pub input: Vec<u8>,
    pub version: MeteringVersion,
    pub fuel_limit: u64,
    pub memory_limit_bytes: u64,
    pub table_limit_elements: u32,
}

impl ExecRequest {
    pub fn new(wasm: Vec<u8>, input: Vec<u8>, version: MeteringVersion) -> Self {
        Self {
            wasm,
            input,
            version,
            fuel_limit: DEFAULT_FUEL_LIMIT,
            memory_limit_bytes: DEFAULT_MEMORY_LIMIT,
            table_limit_elements: DEFAULT_TABLE_LIMIT,
        }
    }
}

/// 沙箱内部、随 store 携带的宿主状态。
struct SandboxState {
    version: MeteringVersion,
    input: Vec<u8>,
    /// 执行开始时的 KV 快照（事务隔离）。
    snapshot: Arc<BTreeMap<String, Vec<u8>>>,
    /// 私有事务缓冲。
    pending: BTreeMap<String, Vec<u8>>,
    output: Vec<u8>,
    charges: Vec<ChargeItem>,
    host_fuel: u64,
    charge_seq: usize,
    memory_limit: usize,
    table_limit: u32,
    /// 由宿主函数记录的非燃料类终止原因（如越界指针）。
    host_abort: Option<HostAbort>,
}

#[derive(Debug, Clone, Copy)]
enum HostAbort {
    OutOfFuel,
    OutOfBounds,
    MemoryLimit,
}

use std::sync::Arc;

/// 引擎池：按计量版本固化 Wasmtime 引擎，进程内复用。
pub struct Engines {
    v1: Engine,
    v2: Engine,
}

impl Engines {
    pub fn new() -> Result<Self> {
        Ok(Self {
            v1: MeteringVersion::V1.build_engine()?,
            v2: MeteringVersion::V2.build_engine()?,
        })
    }

    pub fn for_version(&self, v: MeteringVersion) -> &Engine {
        match v {
            MeteringVersion::V1 => &self.v1,
            MeteringVersion::V2 => &self.v2,
        }
    }
}

impl wasmtime::ResourceLimiter for SandboxState {
    fn memory_growing(
        &mut self,
        _current: usize,
        desired: usize,
        _maximum: Option<usize>,
    ) -> Result<bool> {
        if desired > self.memory_limit {
            // 返回 Err：立即陷阱终止，而不是让 memory.grow 返回 -1，
            // 从而保证“超上限即终止且不提交”。
            self.host_abort = Some(HostAbort::MemoryLimit);
            Err(anyhow!(
                "memory growth to {desired} bytes exceeds sandbox limit {} bytes",
                self.memory_limit
            ))
        } else {
            Ok(true)
        }
    }

    fn table_growing(
        &mut self,
        _current: u32,
        desired: u32,
        _maximum: Option<u32>,
    ) -> Result<bool> {
        if desired > self.table_limit {
            self.host_abort = Some(HostAbort::MemoryLimit);
            Err(anyhow!(
                "table growth to {desired} elements exceeds sandbox limit {}",
                self.table_limit
            ))
        } else {
            Ok(true)
        }
    }
}

impl SandboxState {
    /// 记录越界访问并返回陷阱。
    fn mark_oob(state: &mut SandboxState) -> Trap {
        state.host_abort = Some(HostAbort::OutOfBounds);
        Trap::MemoryOutOfBounds
    }
}

/// 尝试计费：先真实扣减 Wasmtime fuel 余额，再记录明细。
/// 所有对 caller 的借用顺序进行、互不重叠。
fn charge(
    caller: &mut Caller<'_, SandboxState>,
    op: HostOp,
    units: u64,
) -> std::result::Result<(), Trap> {
    // 1) 读取单价（只读借用，立即释放）
    let (name, counted, unit_price, fuel) = caller.data().version.describe_charge(op, units);
    // 2) 读取燃料余额
    let remaining = caller.get_fuel().unwrap_or(0);
    if remaining < fuel {
        // 3a) 不足：设置余额 0、标记宿主侧燃料耗尽并陷阱
        let _ = caller.set_fuel(0);
        caller.data_mut().host_abort = Some(HostAbort::OutOfFuel);
        return Err(Trap::OutOfFuel);
    }
    // 3b) 真实扣减
    caller
        .set_fuel(remaining - fuel)
        .map_err(|_| Trap::UnreachableCodeReached)?;
    // 4) 记账
    let s = caller.data_mut();
    s.host_fuel = s.host_fuel.saturating_add(fuel);
    s.charges.push(ChargeItem {
        seq: s.charge_seq,
        host_fn: name.to_string(),
        units: counted,
        unit_price_fuel: unit_price,
        fuel,
    });
    s.charge_seq += 1;
    Ok(())
}

/// 从模块内存安全读取一段字节；越界返回 MemoryOutOfBounds 陷阱。
fn read_mem<T>(
    caller: &mut Caller<'_, T>,
    memory: &Memory,
    ptr: u32,
    len: u32,
) -> std::result::Result<Vec<u8>, Trap> {
    let data = memory.data(&*caller);
    let start = ptr as usize;
    let end = start
        .checked_add(len as usize)
        .ok_or(Trap::MemoryOutOfBounds)?;
    if end > data.len() {
        return Err(Trap::MemoryOutOfBounds);
    }
    Ok(data[start..end].to_vec())
}

/// 向模块内存安全写入一段字节；越界返回 MemoryOutOfBounds 陷阱。
fn write_mem<T>(
    caller: &mut Caller<'_, T>,
    memory: &Memory,
    ptr: u32,
    bytes: &[u8],
) -> std::result::Result<(), Trap> {
    memory
        .write(&mut *caller, ptr as usize, bytes)
        .map_err(|_| Trap::MemoryOutOfBounds)
}

/// 读取模块内存中以 NUL 结尾的 UTF-8 key；任何形式的非法/越界都陷阱。
fn read_cstr<T>(
    caller: &mut Caller<'_, T>,
    memory: &Memory,
    ptr: u32,
    max_len: usize,
) -> std::result::Result<String, Trap> {
    let data = memory.data(&*caller);
    let start = ptr as usize;
    if start >= data.len() {
        return Err(Trap::MemoryOutOfBounds);
    }
    let mut end = start;
    while end < data.len() && end - start < max_len {
        if data[end] == 0 {
            break;
        }
        end += 1;
    }
    if end == data.len() || end - start == max_len {
        return Err(Trap::MemoryOutOfBounds);
    }
    std::str::from_utf8(&data[start..end])
        .map_err(|_| Trap::MemoryOutOfBounds)
        .map(str::to_string)
}

/// 构建并注册白名单宿主函数。
fn build_linker(engine: &Engine) -> Result<Linker<SandboxState>> {
    let mut linker: Linker<SandboxState> = Linker::new(engine);

    // input_read(ptr: i32, max_len: i32) -> i32
    // 把本次输入复制到模块内存，返回复制的字节数；buffer 不足时截断。
    linker.func_wrap(
        HOST_NAMESPACE,
        "input_read",
        |mut caller: Caller<'_, SandboxState>, ptr: i32, max_len: i32| -> Result<i32> {
            if ptr < 0 || max_len < 0 {
                return Err(SandboxState::mark_oob(caller.data_mut()).into());
            }
            let memory = get_memory(&mut caller)?;
            let n = caller.data().input.len().min(max_len as usize);
            let chunk = caller.data().input[..n].to_vec();
            write_mem(&mut caller, &memory, ptr as u32, &chunk)?;
            charge(&mut caller, HostOp::InputRead, n as u64)?;
            Ok(n as i32)
        },
    )?;

    // output_write(ptr: i32, len: i32) -> i32
    // 收集模块输出；返回收取的字节数。
    linker.func_wrap(
        HOST_NAMESPACE,
        "output_write",
        |mut caller: Caller<'_, SandboxState>, ptr: i32, len: i32| -> Result<i32> {
            if ptr < 0 || len < 0 {
                return Err(SandboxState::mark_oob(caller.data_mut()).into());
            }
            let memory = get_memory(&mut caller)?;
            let bytes = read_mem(&mut caller, &memory, ptr as u32, len as u32)?;
            let n = bytes.len() as u64;
            charge(&mut caller, HostOp::OutputWrite, n)?;
            caller.data_mut().output.extend_from_slice(&bytes);
            Ok(n as i32)
        },
    )?;

    // kv_get(key_ptr: i32, val_ptr: i32, val_max: i32) -> i32
    // 读取快照（不含未提交写入）；命中返回复制字节数并写入值，未命中返回 -1。
    linker.func_wrap(
        HOST_NAMESPACE,
        "kv_get",
        |mut caller: Caller<'_, SandboxState>,
         key_ptr: i32,
         val_ptr: i32,
         val_max: i32|
         -> Result<i32> {
            if key_ptr < 0 || val_ptr < 0 || val_max < 0 {
                return Err(SandboxState::mark_oob(caller.data_mut()).into());
            }
            let memory = get_memory(&mut caller)?;
            let key = read_cstr(&mut caller, &memory, key_ptr as u32, 4096)?;
            let value = caller.data().snapshot.get(&key).cloned();
            match value {
                None => Ok(-1_i32),
                Some(v) => {
                    let n = v.len().min(val_max as usize);
                    write_mem(&mut caller, &memory, val_ptr as u32, &v[..n])?;
                    charge(&mut caller, HostOp::KvGet, n as u64)?;
                    Ok(n as i32)
                }
            }
        },
    )?;

    // kv_put(key_ptr: i32, val_ptr: i32, val_len: i32) -> i32
    // 写入私有事务缓冲；成功返回 0。发布只在 run() 返回 0 后发生。
    linker.func_wrap(
        HOST_NAMESPACE,
        "kv_put",
        |mut caller: Caller<'_, SandboxState>,
         key_ptr: i32,
         val_ptr: i32,
         val_len: i32|
         -> Result<i32> {
            if key_ptr < 0 || val_ptr < 0 || val_len < 0 {
                return Err(SandboxState::mark_oob(caller.data_mut()).into());
            }
            let memory = get_memory(&mut caller)?;
            let key = read_cstr(&mut caller, &memory, key_ptr as u32, 4096)?;
            let value = read_mem(&mut caller, &memory, val_ptr as u32, val_len as u32)?;
            let n = value.len() as u64;
            charge(&mut caller, HostOp::KvPut, n)?;
            caller.data_mut().pending.insert(key, value);
            Ok(0_i32)
        },
    )?;

    // kv_has(key_ptr: i32) -> i32，命中 1 / 未命中 0。
    linker.func_wrap(
        HOST_NAMESPACE,
        "kv_has",
        |mut caller: Caller<'_, SandboxState>, key_ptr: i32| -> Result<i32> {
            if key_ptr < 0 {
                return Err(SandboxState::mark_oob(caller.data_mut()).into());
            }
            let memory = get_memory(&mut caller)?;
            let key = read_cstr(&mut caller, &memory, key_ptr as u32, 4096)?;
            charge(&mut caller, HostOp::KvHas, 1)?;
            Ok(if caller.data().snapshot.contains_key(&key) {
                1
            } else {
                0
            })
        },
    )?;

    Ok(linker)
}

/// 取模块导出的内存（校验阶段已强制要求导出 `memory`）。
fn get_memory(caller: &mut Caller<'_, SandboxState>) -> std::result::Result<Memory, Trap> {
    caller
        .get_export("memory")
        .and_then(|e| e.into_memory())
        .ok_or_else(|| {
            caller.data_mut().host_abort = Some(HostAbort::OutOfBounds);
            Trap::MemoryOutOfBounds
        })
}

/// 实例化前的静态校验：import 必须全部是 `gas` 白名单函数，
/// 且模块必须导出名为 `run` 的 (func [] -> [i32]) 入口。
fn validate_module(module: &Module) -> Result<()> {
    for import in module.imports() {
        let module_name = import.module();
        let field = import.name();
        if module_name != HOST_NAMESPACE || !ALLOWED_IMPORTS.contains(&field) {
            bail!(
                "import not on whitelist: {module_name}::{field} — \
                 only these host functions are permitted: gas::{{{}}}",
                ALLOWED_IMPORTS.join(", ")
            );
        }
        if !matches!(import.ty(), wasmtime::ExternType::Func(_)) {
            bail!("import {module_name}::{field} must be a function");
        }
    }

    let mut has_memory = false;
    let mut has_run = false;
    for export in module.exports() {
        match export.ty() {
            wasmtime::ExternType::Memory(_) if export.name() == "memory" => has_memory = true,
            wasmtime::ExternType::Func(_) if export.name() == "run" => has_run = true,
            _ => {}
        }
    }
    if !has_memory {
        bail!("module must export a linear memory named `memory`");
    }
    if !has_run {
        bail!("module must export a `run` function");
    }
    Ok(())
}

/// 编译、校验并执行一次 WASM 调用。返回收据；永不因模块行为而 panic。
pub fn execute(engines: &Engines, host: &HostState, req: ExecRequest) -> Result<Receipt> {
    let version = req.version;
    let engine = engines.for_version(version);

    let module = Module::from_binary(engine, &req.wasm).context("failed to decode wasm binary")?;
    validate_module(&module).context("module rejected by sandbox policy")?;

    let linker = build_linker(engine).context("failed to build host linker")?;

    let snapshot = host.current();
    let state = SandboxState {
        version,
        input: req.input.clone(),
        snapshot,
        pending: BTreeMap::new(),
        output: Vec::new(),
        charges: Vec::new(),
        host_fuel: 0,
        charge_seq: 0,
        memory_limit: req.memory_limit_bytes as usize,
        table_limit: req.table_limit_elements,
        host_abort: None,
    };

    let mut store = Store::new(engine, state);
    store.limiter(|s| s as &mut dyn wasmtime::ResourceLimiter);
    store
        .set_fuel(req.fuel_limit)
        .context("failed to set fuel")?;

    let instance = linker
        .instantiate(&mut store, &module)
        .context("instantiation rejected (likely disallowed import)")?;
    let run = match instance.get_typed_func::<(), i32>(&mut store, "run") {
        Ok(f) => f,
        Err(e) => bail!("export `run` must have signature () -> i32: {e}"),
    };

    // 真正执行计算/协议/密码操作。失败如实映射为终态。
    let outcome: Result<i32> = run.call(&mut store, ());
    let fuel_after = store.get_fuel().unwrap_or(0);
    let wasm_fuel_consumed = req
        .fuel_limit
        .saturating_sub(fuel_after)
        .saturating_sub(store.data().host_fuel);

    let (status, termination_reason) = classify(&store, outcome);

    // 仅 Success 原子发布事务缓冲。
    let committed_writes = if status == ExecStatus::Success {
        let writes = std::mem::take(&mut store.data_mut().pending);
        host.commit(&writes)
    } else {
        // 显式丢弃。
        store.data_mut().pending.clear();
        Vec::new()
    };

    let total_fuel_consumed = req.fuel_limit.saturating_sub(fuel_after);
    let output = store.data().output.clone();
    let charges = store.data().charges.clone();
    let host_fuel = store.data().host_fuel;

    let mut receipt = Receipt {
        module_sha256: hex::encode(sha256(&req.wasm)),
        input_sha256: hex::encode(sha256(&req.input)),
        metering_version: version.as_u32(),
        metering_version_name: version.name().to_string(),
        wasmtime_version: env!("CARGO_PKG_VERSION_WASMTIME").to_string(),
        fuel_limit: req.fuel_limit,
        memory_limit_bytes: req.memory_limit_bytes,
        table_limit_elements: req.table_limit_elements,
        status,
        termination_reason,
        wasm_fuel_consumed,
        host_fuel_consumed: host_fuel,
        total_fuel_consumed,
        fuel_remaining: fuel_after,
        charges,
        output_base64: base64::engine::general_purpose::STANDARD.encode(&output),
        committed_writes,
        committed: status.committed(),
        result_hash: String::new(),
    };
    receipt.result_hash = compute_result_hash(&receipt);
    Ok(receipt)
}

/// 把执行结果分类为收据终态。
fn classify(store: &Store<SandboxState>, outcome: Result<i32>) -> (ExecStatus, String) {
    match outcome {
        Ok(0) => (ExecStatus::Success, "ok: run() returned 0".to_string()),
        Ok(code) => (
            ExecStatus::ModuleAbort,
            format!("module aborted with non-zero exit code {code}; writes rolled back"),
        ),
        Err(err) => {
            // 取出 Wasmtime 陷阱码（用户错误/限制器错误也可能携带 Trap）。
            let trap: Option<Trap> = err
                .downcast_ref::<Trap>()
                .copied()
                .or_else(|| find_trap_in_chain(&err));
            // 宿主函数/限制器标记优先。
            match store.data().host_abort {
                Some(HostAbort::OutOfFuel) => (
                    ExecStatus::OutOfFuel,
                    "fuel exhausted inside a host function; writes rolled back".to_string(),
                ),
                Some(HostAbort::OutOfBounds) => (
                    ExecStatus::Trap,
                    "trap: out-of-bounds host memory access; writes rolled back".to_string(),
                ),
                Some(HostAbort::MemoryLimit) => (
                    ExecStatus::MemoryLimitExceeded,
                    format!(
                        "memory/table growth exceeded sandbox limit ({err}); writes rolled back"
                    ),
                ),
                None => match trap {
                    Some(Trap::OutOfFuel) => (
                        ExecStatus::OutOfFuel,
                        "fuel exhausted: execution terminated; writes rolled back".to_string(),
                    ),
                    Some(Trap::MemoryOutOfBounds) | Some(Trap::TableOutOfBounds) => (
                        ExecStatus::Trap,
                        format!("trap: {}; writes rolled back", trap.unwrap()),
                    ),
                    Some(other) => (
                        ExecStatus::Trap,
                        format!("trap: {other}; writes rolled back"),
                    ),
                    None => (ExecStatus::Trap, format!("trap: {err}; writes rolled back")),
                },
            }
        }
    }
}

/// 沿错误源链查找 Wasmtime 陷阱码。
fn find_trap_in_chain(err: &anyhow::Error) -> Option<Trap> {
    let mut src: Option<&dyn std::error::Error> = err.source();
    let mut depth = 0;
    while let Some(e) = src {
        if let Some(t) = e.downcast_ref::<Trap>() {
            return Some(*t);
        }
        src = e.source();
        depth += 1;
        if depth > 8 {
            break;
        }
    }
    None
}

fn sha256(data: &[u8]) -> Vec<u8> {
    use sha2::Digest;
    let mut h = sha2::Sha256::new();
    h.update(data);
    h.finalize().to_vec()
}

/// 对终态、输出、分项账目、燃料做摘要：相同输入+版本重复执行必须一致。
fn compute_result_hash(r: &Receipt) -> String {
    use sha2::Digest;
    let mut h = sha2::Sha256::new();
    h.update(b"gas-meter-result/v1\n");
    h.update([r.status.as_str(), "\n"].concat().as_bytes());
    h.update(r.total_fuel_consumed.to_le_bytes());
    h.update(r.wasm_fuel_consumed.to_le_bytes());
    h.update(r.host_fuel_consumed.to_le_bytes());
    h.update((r.charges.len() as u64).to_le_bytes());
    for c in &r.charges {
        h.update(c.host_fn.as_bytes());
        h.update(c.units.to_le_bytes());
        h.update(c.unit_price_fuel.to_le_bytes());
        h.update(c.fuel.to_le_bytes());
    }
    h.update(r.output_base64.as_bytes());
    hex::encode(h.finalize())
}
