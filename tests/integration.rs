//! 引擎级集成测试：事务回滚、确定性、版本化计量、燃料/内存限制、幂等。

use std::sync::Arc;

use versioned_gas_meter::{
    samples, CommittedState, Engine, ExecRequest, Journal, TerminationKind, VersionRegistry,
};

fn engine_with(state: Arc<CommittedState>) -> Engine {
    Engine::new(VersionRegistry::new(), state, Arc::new(Journal::new(64))).unwrap()
}

fn req_for(name: &str, version: u32) -> ExecRequest {
    let sample = samples::by_name(name).unwrap();
    let input = match name {
        "finite_loop" => 100_000u64.to_le_bytes().to_vec(),
        "memory_grow" => 1u64.to_le_bytes().to_vec(),
        _ => Vec::new(),
    };
    ExecRequest {
        module: sample.wasm.to_vec(),
        input,
        metering_version: version,
        fuel_limit_override: None,
        idempotency_key: None,
    }
}

#[test]
fn finite_loop_commits_real_computation() {
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state.clone());
    let resp = engine.execute(req_for("finite_loop", 1));

    assert_eq!(resp.status, "committed");
    assert_eq!(resp.termination, None);
    assert_eq!(resp.state_seq_after, Some(1));

    // 返回值是读回的十进制和。
    let out = resp
        .output
        .as_deref()
        .map(versioned_gas_meter::base64::decode)
        .unwrap()
        .unwrap();
    assert_eq!(out, b"5000050000"); // n(n+1)/2 for n=100000

    // 事务里确实发布了 sum 与 hash 两个键。
    let (_, kv) = state.snapshot();
    assert_eq!(kv.get("sum").unwrap(), b"5000050000");
    assert_eq!(kv.get("hash").unwrap().len(), 32);

    // 账单恒等式：总消耗 = WASM 指令 + 宿主调用。
    let f = &resp.fuel;
    assert_eq!(
        f.consumed_total,
        f.wasm_instruction_fuel + f.host_call_fuel,
        "燃料账单必须守恒"
    );
    assert!(f.host_call_breakdown.contains_key("sha256"));
    // 明确声明不是链 Gas。
    assert!(f.disclaimer.contains("不是任何区块链的真实 Gas"));
}

#[test]
fn sha256_is_real_not_stubbed() {
    // guest 计算的 sha256("5000050000") 必须等于宿主参考实现。
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let resp = engine.execute(req_for("finite_loop", 1));
    let hash_b64 = resp.diff.upserts.get("hash").unwrap();
    let hash = versioned_gas_meter::base64::decode(hash_b64).unwrap();
    let expected = {
        use sha2::{Digest, Sha256};
        Sha256::digest(b"5000050000").to_vec()
    };
    assert_eq!(hash, expected);
}

#[test]
fn trap_and_fuel_exhaustion_roll_back_all_writes() {
    let dir = tempfile::tempdir().unwrap();
    let state = Arc::new(CommittedState::load(dir.path()).unwrap());
    let engine = engine_with(state.clone());

    // 先成功一次，seq=1。
    let ok = engine.execute(req_for("finite_loop", 1));
    assert_eq!(ok.status, "committed");

    // 无限循环：先写 poison 后耗尽燃料。
    let inf = engine.execute(req_for("infinite_loop", 1));
    assert_eq!(inf.status, "terminated");
    assert_eq!(inf.termination, Some(TerminationKind::OutOfFuel));
    assert_eq!(inf.fuel.remaining, 0);
    assert!(inf.diff.upserts.is_empty());

    // 陷阱后写入。
    let trap = engine.execute(req_for("trap_after_write", 1));
    assert_eq!(trap.termination, Some(TerminationKind::Trap));

    // 提交态不得出现 poison，seq 不得增长。
    let (seq, kv) = state.snapshot();
    assert_eq!(seq, 1, "失败执行不得推进状态版本");
    assert!(!kv.contains_key("poison"), "事务缓冲写入必须随失败回滚");
    assert!(kv.contains_key("sum"));

    // 重新从磁盘加载：落盘状态同样干净（原子发布）。
    let reloaded = CommittedState::load(dir.path()).unwrap();
    let (seq2, kv2) = reloaded.snapshot();
    assert_eq!(seq2, 1);
    assert!(!kv2.contains_key("poison"));
}

#[test]
fn memory_and_bounds_limits_terminate_without_commit() {
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state.clone());

    let grow = engine.execute(req_for("memory_grow", 1));
    assert_eq!(grow.termination, Some(TerminationKind::MemoryLimitExceeded));
    assert!(grow.peak_memory_bytes <= 4 * 1024 * 1024);

    let oob = engine.execute(req_for("oob_read", 1));
    assert_eq!(oob.termination, Some(TerminationKind::MemoryOutOfBounds));

    let bad_ptr = engine.execute(req_for("host_bad_ptr", 1));
    assert_eq!(
        bad_ptr.termination,
        Some(TerminationKind::MemoryOutOfBounds)
    );

    let (seq, _) = state.snapshot();
    assert_eq!(seq, 0);
}

#[test]
fn non_whitelisted_import_is_a_link_error() {
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let resp = engine.execute(req_for("denied_import", 1));
    assert_eq!(resp.termination, Some(TerminationKind::LinkError));
    assert!(resp.error.as_deref().unwrap().contains("socket_connect"));
    assert_eq!(
        resp.fuel.consumed_total, 0,
        "链接失败的模块不应执行任何指令"
    );
}

#[test]
fn determinism_same_input_same_version_same_bill() {
    // 两次独立引擎/独立状态执行相同请求，结果与燃料账单必须逐位一致。
    let run_once = || {
        let state = Arc::new(CommittedState::memory());
        let engine = engine_with(state);
        engine.execute(req_for("finite_loop", 1))
    };
    let a = run_once();
    let b = run_once();
    assert_eq!(a.status, "committed");
    assert_eq!(a.output, b.output);
    assert_eq!(a.diff.upserts, b.diff.upserts);
    assert_eq!(a.fuel.consumed_total, b.fuel.consumed_total);
    assert_eq!(a.fuel.wasm_instruction_fuel, b.fuel.wasm_instruction_fuel);
    assert_eq!(a.fuel.host_call_fuel, b.fuel.host_call_fuel);
    assert_eq!(a.fuel.host_call_breakdown, b.fuel.host_call_breakdown);
    assert_eq!(a.determinism_key, b.determinism_key);
}

#[test]
fn metering_versions_change_bill_not_result() {
    let v1 = engine_with(Arc::new(CommittedState::memory())).execute(req_for("finite_loop", 1));
    let v2 = engine_with(Arc::new(CommittedState::memory())).execute(req_for("finite_loop", 2));
    assert_eq!(v1.status, "committed");
    assert_eq!(v2.status, "committed");

    // 计算结果一致。
    assert_eq!(v1.output, v2.output);
    assert_eq!(v1.diff.upserts, v2.diff.upserts);

    // WASM 指令本身的燃料一致；宿主收费 v2 是 v1 的两倍（版本表如此定义）。
    assert_eq!(v1.fuel.wasm_instruction_fuel, v2.fuel.wasm_instruction_fuel);
    assert_eq!(v2.fuel.host_call_fuel, 2 * v1.fuel.host_call_fuel);

    // 确定性键包含版本号，因此不同版本键不同、可区分、可复现。
    assert_ne!(v1.determinism_key, v2.determinism_key);
    assert_ne!(v1.metering_version_name, v2.metering_version_name);
}

#[test]
fn v2_raises_memory_ceiling() {
    // v2 内存上限 8MiB：增长样例应越过 4MiB 后在 8MiB 处终止。
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let resp = engine.execute(req_for("memory_grow", 2));
    assert_eq!(resp.termination, Some(TerminationKind::MemoryLimitExceeded));
    assert!(resp.peak_memory_bytes > 4 * 1024 * 1024);
    assert!(resp.peak_memory_bytes <= 8 * 1024 * 1024);
}

#[test]
fn tiny_fuel_override_terminates_finite_loop_too() {
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let mut req = req_for("finite_loop", 1);
    req.fuel_limit_override = Some(1_000);
    let resp = engine.execute(req);
    assert_eq!(resp.termination, Some(TerminationKind::OutOfFuel));
    assert!(resp.state_seq_after.is_none());
}

#[test]
fn fuel_override_above_version_cap_is_rejected() {
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let mut req = req_for("finite_loop", 1);
    req.fuel_limit_override = Some(u64::MAX);
    let resp = engine.execute(req);
    assert_eq!(resp.termination, Some(TerminationKind::BadRequest));
}

#[test]
fn idempotency_key_returns_first_result_and_publishes_once() {
    let dir = tempfile::tempdir().unwrap();
    let state = Arc::new(CommittedState::load(dir.path()).unwrap());
    let engine = engine_with(state.clone());

    let mut first = req_for("finite_loop", 1);
    first.idempotency_key = Some("key-123".into());
    let r1 = engine.execute(first);
    assert_eq!(r1.state_seq_after, Some(1));

    // 相同幂等键但换一个输入：必须直接返回首次结果，不能再次执行/发布。
    let mut second = req_for("finite_loop", 1);
    second.idempotency_key = Some("key-123".into());
    second.input = 1u64.to_le_bytes().to_vec();
    let r2 = engine.execute(second);
    assert_eq!(r2.output, r1.output);
    assert_eq!(r2.state_seq_after, Some(1));

    let (seq, _) = state.snapshot();
    assert_eq!(seq, 1);
}

#[test]
fn garbage_bytes_are_compile_error() {
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let resp = engine.execute(ExecRequest {
        module: b"\0asm_GARBAGE_NOT_A_MODULE".to_vec(),
        input: vec![],
        metering_version: 1,
        fuel_limit_override: None,
        idempotency_key: None,
    });
    assert_eq!(resp.termination, Some(TerminationKind::CompileError));
    assert_eq!(resp.fuel.consumed_total, 0);
}

#[test]
fn module_without_required_exports_is_rejected() {
    // 合法 wasm，但没有 memory/alloc/run 导出。
    let wasm = wat::parse_str("(module (func (export \"nothing\")))").unwrap();
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let resp = engine.execute(ExecRequest {
        module: wasm,
        input: vec![],
        metering_version: 1,
        fuel_limit_override: None,
        idempotency_key: None,
    });
    assert_eq!(resp.termination, Some(TerminationKind::ModuleRejected));
    assert!(resp.state_seq_after.is_none());
}

#[test]
fn oversized_input_is_bad_request() {
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let sample = samples::by_name("finite_loop").unwrap();
    // v1 input_max_bytes = 64KiB；给 1MiB 输入。
    let resp = engine.execute(ExecRequest {
        module: sample.wasm.to_vec(),
        input: vec![0u8; 1024 * 1024],
        metering_version: 1,
        fuel_limit_override: None,
        idempotency_key: None,
    });
    assert_eq!(resp.termination, Some(TerminationKind::BadRequest));
    assert!(resp.error.as_deref().unwrap().contains("输入"));
}

#[test]
fn unknown_version_is_bad_request() {
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let mut req = req_for("oob_read", 42);
    req.metering_version = 42;
    let resp = engine.execute(req);
    assert_eq!(resp.termination, Some(TerminationKind::BadRequest));
    assert!(resp.error.as_deref().unwrap().contains("未知计量版本"));
}

#[test]
fn malicious_alloc_returning_oob_pointer_is_terminated() {
    // 恶意模块的 alloc 永远返回线性内存末端之外的地址。
    // 宿主写入输入时必须以 memory_out_of_bounds 终止，不得继续调用 run。
    let wasm = wat::parse_str(
        r#"
        (module
          (memory (export "memory") 1)
          (func (export "alloc") (param i32) (result i32)
            ;; 返回 0x7fffffff，必然越界。
            (i32.const 0x7fffffff))
          (func (export "run") (param i32 i32) (result i64)
            (unreachable)
            (i64.const 0)))
        "#,
    )
    .unwrap();
    let state = Arc::new(CommittedState::memory());
    let engine = engine_with(state);
    let resp = engine.execute(ExecRequest {
        module: wasm,
        input: vec![1u8, 2, 3, 4],
        metering_version: 1,
        fuel_limit_override: None,
        idempotency_key: None,
    });
    assert_eq!(resp.termination, Some(TerminationKind::MemoryOutOfBounds));
    assert!(resp.state_seq_after.is_none());
}
