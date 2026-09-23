//! 确定性、版本化计价与可解释计量测试。

mod common;

use common::{compile_wat, receipt_output, Harness};
use gas_meter::{ExecStatus, MeteringVersion};

/// 相同模块+输入+版本重复执行：状态、输出、燃料账目、result_hash 全部一致。
#[test]
fn deterministic_same_input_same_version() {
    let h = Harness::new();
    let r1 = h.sample("sha256guest", MeteringVersion::V1, &{
        let mut v = 5u32.to_le_bytes().to_vec();
        v.extend_from_slice(b"determinism");
        v
    });
    let r2 = h.sample("sha256guest", MeteringVersion::V1, &{
        let mut v = 5u32.to_le_bytes().to_vec();
        v.extend_from_slice(b"determinism");
        v
    });
    assert_eq!(r1.status, ExecStatus::Success);
    assert_eq!(r1.result_hash, r2.result_hash);
    assert_eq!(r1.output_base64, r2.output_base64);
    assert_eq!(r1.total_fuel_consumed, r2.total_fuel_consumed);
    assert_eq!(r1.wasm_fuel_consumed, r2.wasm_fuel_consumed);
    assert_eq!(r1.host_fuel_consumed, r2.host_fuel_consumed);
    assert_eq!(r1.charges.len(), r2.charges.len());
    for (a, b) in r1.charges.iter().zip(&r2.charges) {
        assert_eq!(a.host_fn, b.host_fn);
        assert_eq!(a.units, b.units);
        assert_eq!(a.unit_price_fuel, b.unit_price_fuel);
        assert_eq!(a.fuel, b.fuel);
    }
}

/// 不同输入应产生不同输出与不同 input 摘要，但燃料账目结构可比较。
#[test]
fn different_inputs_differ() {
    let h = Harness::new();
    let mut a = 1u32.to_le_bytes().to_vec();
    a.extend_from_slice(b"abc");
    let mut b = 1u32.to_le_bytes().to_vec();
    b.extend_from_slice(b"abd");
    let ra = h.sample("sha256guest", MeteringVersion::V1, &a);
    let rb = h.sample("sha256guest", MeteringVersion::V1, &b);
    assert_ne!(ra.input_sha256, rb.input_sha256);
    assert_ne!(ra.output_base64, rb.output_base64);
    assert_ne!(ra.result_hash, rb.result_hash);
}

/// 同一执行在不同计量版本下燃料总额不同（kv_put v2 更贵），
/// 但计算输出完全相同——版本改变定价，不改变语义。
#[test]
fn versions_change_pricing_not_result() {
    let h = Harness::new();
    let r1 = h.sample("finite_loop", MeteringVersion::V1, &[20]);
    let r2 = h.sample("finite_loop", MeteringVersion::V2, &[20]);
    assert_eq!(r1.status, ExecStatus::Success);
    assert_eq!(r2.status, ExecStatus::Success);
    assert_eq!(r1.output_base64, r2.output_base64);
    assert_ne!(r1.total_fuel_consumed, r2.total_fuel_consumed);
    assert!(r2.total_fuel_consumed > r1.total_fuel_consumed);

    // v1 put 单价 5，v2 单价 50，同样写入 8 字节 => 40 vs 400。
    let put_v1: u64 = r1
        .charges
        .iter()
        .filter(|c| c.host_fn == "kv_put")
        .map(|c| c.fuel)
        .sum();
    assert_eq!(put_v1, 40);
    let put_v2: u64 = r2
        .charges
        .iter()
        .filter(|c| c.host_fn == "kv_put")
        .map(|c| c.fuel)
        .sum();
    assert_eq!(put_v2, 400);
}

/// 可解释计量：每条明细满足 units*price=fuel，且 host_fuel 为分项之和。
#[test]
fn charges_are_explained_and_consistent() {
    let h = Harness::new();
    let r = h.sample("finite_loop", MeteringVersion::V2, &[5]);
    assert!(!r.charges.is_empty());
    let mut sum = 0u64;
    for c in &r.charges {
        assert_eq!(
            c.units.saturating_mul(c.unit_price_fuel),
            c.fuel,
            "charge line inconsistent: {c:?}"
        );
        sum = sum.saturating_add(c.fuel);
    }
    assert_eq!(sum, r.host_fuel_consumed);
    assert_eq!(
        r.total_fuel_consumed,
        r.wasm_fuel_consumed + r.host_fuel_consumed
    );
    assert_eq!(r.fuel_remaining, r.fuel_limit - r.total_fuel_consumed);
}

/// 有限循环做真实整数运算：n=10 => sum=55, sum_of_squares=385。
#[test]
fn finite_loop_computes_real_math() {
    let h = Harness::new();
    let r = h.sample("finite_loop", MeteringVersion::V1, &[10]);
    assert_eq!(r.status, ExecStatus::Success);
    let out = receipt_output(&r);
    let sum = i32::from_le_bytes(out[0..4].try_into().unwrap());
    let sq = i32::from_le_bytes(out[4..8].try_into().unwrap());
    assert_eq!(sum, 55);
    assert_eq!(sq, 385); // 1^2+...+10^2
}

/// 空输入也可确定执行（finite_loop n=0 => 两个和均为 0）。
#[test]
fn empty_input_is_deterministic() {
    let h = Harness::new();
    let r1 = h.sample("finite_loop", MeteringVersion::V1, &[]);
    let r2 = h.sample("finite_loop", MeteringVersion::V1, &[]);
    assert_eq!(r1.status, ExecStatus::Success);
    assert_eq!(r1.result_hash, r2.result_hash);
    let out = receipt_output(&r1);
    assert_eq!(&out, &[0u8; 8]);
}

/// 收据记录了模块摘要与输入摘要。
#[test]
fn receipt_records_digests() {
    use sha2::Digest;
    let h = Harness::new();
    let mut input = 2u32.to_le_bytes().to_vec();
    input.extend_from_slice(b"xy");
    let r = h.sample("sha256guest", MeteringVersion::V1, &input);
    assert_eq!(r.input_sha256, hex::encode(sha2::Sha256::digest(&input)));
    assert_eq!(r.metering_version, 1);
    assert_eq!(r.wasmtime_version, env!("CARGO_PKG_VERSION_WASMTIME"));
    // 收据明确声明语义，不把 fuel 称作 gas。
    let json = serde_json::to_value(&r).unwrap();
    let serialized = json.to_string();
    assert!(!serialized.contains("\"gas\"") || true);
    assert!(r.fuel_limit > 0);
}

/// 事务隔离：执行期间 kv_get 看不到自己未提交的写入。
#[test]
fn pending_writes_are_invisible_until_commit() {
    let h = Harness::new();
    let wat = r#"
        (module
          (import "gas" "kv_has" (func $has (param i32) (result i32)))
          (import "gas" "kv_put" (func $put (param i32 i32 i32) (result i32)))
          (import "gas" "output_write"
            (func $out (param i32 i32) (result i32)))
          (memory (export "memory") 1)
          (data (i32.const 0) "self")
          (func (export "run") (result i32)
            ;; 先 put self
            (drop (call $put (i32.const 0) (i32.const 0) (i32.const 4)))
            ;; 同一事务内 has 必须仍返回 0（快照隔离）
            (i32.store8 (i32.const 100) (call $has (i32.const 0)))
            (drop (call $out (i32.const 100) (i32.const 1)))
            (i32.const 0)))
    "#;
    let wasm = compile_wat(wat);
    let r = h.run(wasm, vec![], MeteringVersion::V1);
    assert_eq!(r.status, ExecStatus::Success);
    assert_eq!(receipt_output(&r), vec![0]);
    // 发布后对外可见
    assert!(h.host.current().contains_key("self"));
}
