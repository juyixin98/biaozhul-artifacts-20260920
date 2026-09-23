//! 沙箱安全策略测试：白名单、内存/表限制、燃料、回滚。

mod common;

use common::{compile_wat, Harness};
use gas_meter::{ExecRequest, ExecStatus, MeteringVersion, DEFAULT_FUEL_LIMIT};

/// 导入 wasi 命名空间的模块必须在实例化前被拒绝。
#[test]
fn rejects_wasi_imports() {
    let h = Harness::new();
    let wat = r#"
        (module
          (import "wasi_snapshot_preview1" "fd_write"
            (func (param i32 i32 i32 i32) (result i32)))
          (memory (export "memory") 1)
          (func (export "run") (result i32) (i32.const 0)))
    "#;
    let wasm = compile_wat(wat);
    let err = gas_meter::execute(
        &h.engines,
        &h.host,
        ExecRequest::new(wasm, vec![], MeteringVersion::V1),
    )
    .expect_err("wasi import must be rejected");
    let msg = format!("{err:#}");
    assert!(msg.contains("whitelist"), "unexpected error: {msg}");
}

/// 导入未知 gas 函数也必须拒绝（白名单是精确枚举，不是命名空间前缀）。
#[test]
fn rejects_unknown_gas_fn() {
    let h = Harness::new();
    let wat = r#"
        (module
          (import "gas" "socket_connect"
            (func (param i32) (result i32)))
          (memory (export "memory") 1)
          (func (export "run") (result i32) (i32.const 0)))
    "#;
    let wasm = compile_wat(wat);
    let err = gas_meter::execute(
        &h.engines,
        &h.host,
        ExecRequest::new(wasm, vec![], MeteringVersion::V1),
    )
    .expect_err("unknown gas fn must be rejected");
    assert!(format!("{err:#}").contains("whitelist"));
}

/// 没有导出 `run` 的模块必须拒绝。
#[test]
fn rejects_missing_run() {
    let h = Harness::new();
    let wat = r#"(module (memory (export "memory") 1))"#;
    let wasm = compile_wat(wat);
    let err = gas_meter::execute(
        &h.engines,
        &h.host,
        ExecRequest::new(wasm, vec![], MeteringVersion::V1),
    )
    .expect_err("missing run must be rejected");
    assert!(format!("{err:#}").contains("run"));
}

/// 无限循环必须因燃料耗尽终止，且不提交任何状态。
#[test]
fn infinite_loop_is_terminated_by_fuel() {
    let h = Harness::new();
    let r = h.sample("infinite_loop", MeteringVersion::V1, &[]);
    assert_eq!(r.status, ExecStatus::OutOfFuel);
    assert!(!r.committed);
    assert!(r.committed_writes.is_empty());
    assert_eq!(r.fuel_remaining, 0);
    assert_eq!(r.total_fuel_consumed, r.fuel_limit);
    assert!(h.state_keys().is_empty());
}

/// 默认燃料预算也能终止无限循环（不依赖调用方压预算）。
#[test]
fn infinite_loop_default_fuel_also_terminates() {
    let h = Harness::new();
    let mut req = ExecRequest::new(
        gas_meter::samples::compile(gas_meter::samples::by_id("infinite_loop").unwrap()).unwrap(),
        vec![],
        MeteringVersion::V1,
    );
    req.fuel_limit = DEFAULT_FUEL_LIMIT;
    let r = h.run_req(req);
    assert_eq!(r.status, ExecStatus::OutOfFuel);
    assert_eq!(r.total_fuel_consumed, DEFAULT_FUEL_LIMIT);
}

/// 陷阱前的宿主写入必须全部回滚。
#[test]
fn writes_after_trap_are_rolled_back() {
    let h = Harness::new();
    // 预置一个不相关键，确认回滚不会误删已提交数据。
    h.host.seed("committed", b"keep".to_vec());
    let r = h.sample("trap_after_write", MeteringVersion::V1, &[0]);
    assert_eq!(r.status, ExecStatus::Trap);
    assert!(!r.committed);
    assert!(h.host.current().get("poisoned").is_none());
    assert_eq!(
        h.host.current().get("committed").map(|v| v.as_slice()),
        Some(b"keep".as_slice())
    );
}

/// 模块返回非零 => 业务中止，写入同样回滚。
#[test]
fn module_abort_rolls_back() {
    let h = Harness::new();
    let wat = r#"
        (module
          (import "gas" "kv_put"
            (func $kv_put (param i32 i32 i32) (result i32)))
          (memory (export "memory") 1)
          (data (i32.const 0) "k")
          (func (export "run") (result i32)
            (drop (call $kv_put (i32.const 0) (i32.const 0) (i32.const 1)))
            (i32.const 7)))
    "#;
    let wasm = compile_wat(wat);
    let r = h.run(wasm, vec![], MeteringVersion::V1);
    assert_eq!(r.status, ExecStatus::ModuleAbort);
    assert!(!r.committed);
    assert!(h.host.current().get("k").is_none());
}

/// 成功执行后写入原子发布，且对后续调用可见。
#[test]
fn success_publishes_writes_atomically() {
    let h = Harness::new();
    let r = h.sample("finite_loop", MeteringVersion::V1, &[10]);
    assert_eq!(r.status, ExecStatus::Success);
    assert!(r.committed);
    let mut keys = h.state_keys();
    keys.sort();
    assert_eq!(keys, vec!["n".to_string(), "trace".to_string()]);
}

/// 内存增长在上限内允许并真实可用；超上限立即终止。
#[test]
fn memory_growth_is_limited() {
    // 限制 2 MiB：增长 31 页（总 32 页=2MiB）允许。
    let mut req_ok = ExecRequest::new(
        gas_meter::samples::compile(gas_meter::samples::by_id("memory_grow").unwrap()).unwrap(),
        31u32.to_le_bytes().to_vec(),
        MeteringVersion::V1,
    );
    req_ok.memory_limit_bytes = 2 * 1024 * 1024;
    let h = Harness::new();
    let r_ok = h.run_req(req_ok);
    assert_eq!(
        r_ok.status,
        ExecStatus::Success,
        "{:?}",
        r_ok.termination_reason
    );
    let out = common::receipt_output(&r_ok);
    assert_eq!(u32::from_le_bytes(out.try_into().unwrap()), 32);

    // 增长 33 页超过 2 MiB => memory_limit_exceeded。
    let mut req_bad = ExecRequest::new(
        gas_meter::samples::compile(gas_meter::samples::by_id("memory_grow").unwrap()).unwrap(),
        33u32.to_le_bytes().to_vec(),
        MeteringVersion::V1,
    );
    req_bad.memory_limit_bytes = 2 * 1024 * 1024;
    let h2 = Harness::new();
    let r_bad = h2.run_req(req_bad);
    assert_eq!(r_bad.status, ExecStatus::MemoryLimitExceeded);
    assert!(!r_bad.committed);
}

/// 宿主边界检查：向宿主函数传越界指针必须陷阱且回滚。
#[test]
fn host_pointer_bounds_checked() {
    let h = Harness::new();
    let r = h.sample("host_oob", MeteringVersion::V1, &[]);
    assert_eq!(r.status, ExecStatus::Trap);
    assert!(!r.committed);
    // 越界发生在第二次宿主调用；第一次 kv_put 已真实执行并计费，
    // 但其缓冲写入必须随陷阱回滚。
    assert_eq!(
        r.charges.iter().filter(|c| c.host_fn == "kv_put").count(),
        1
    );
    assert!(r.charges.iter().all(|c| c.host_fn != "output_write"));
    assert!(h.state_keys().is_empty());
}

/// input_read 越界目标指针也必须被捕获。
#[test]
fn input_read_into_bad_pointer_traps() {
    let h = Harness::new();
    let wat = r#"
        (module
          (import "gas" "input_read"
            (func (param i32 i32) (result i32)))
          (memory (export "memory") 1)
          (func (export "run") (result i32)
            (drop (call 0 (i32.const 0x7fffffff) (i32.const 8)))
            (i32.const 0)))
    "#;
    let wasm = compile_wat(wat);
    let r = h.run(wasm, b"abc".to_vec(), MeteringVersion::V1);
    assert_eq!(r.status, ExecStatus::Trap);
}

/// 宿主函数收费本身也能耗尽燃料：超大写入在 kv_put 时被拒，
/// 缓冲不得留下该条目。
#[test]
fn host_charge_can_exhaust_fuel() {
    let h = Harness::new();
    let wat = r#"
        (module
          (import "gas" "kv_put"
            (func $put (param i32 i32 i32) (result i32)))
          (memory (export "memory") 1)
          (data (i32.const 0) "key")
          (func (export "run") (result i32)
            ;; v1 kv_put 每字节 5 fuel；预算 100 => 超过 20 字节即耗尽
            (drop (call $put (i32.const 0) (i32.const 0) (i32.const 4096)))
            (i32.const 0)))
    "#;
    let wasm = compile_wat(wat);
    let mut req = ExecRequest::new(wasm, vec![], MeteringVersion::V1);
    req.fuel_limit = 100;
    let r = h.run_req(req);
    assert_eq!(r.status, ExecStatus::OutOfFuel);
    assert!(!r.committed);
    assert!(h.host.current().get("key").is_none());
}
