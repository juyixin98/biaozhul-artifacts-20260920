//! 密码学样例测试：来宾模块的 SHA-256 必须是真实计算，
//! 与宿主侧 sha2 crate（独立实现）逐向量一致。

mod common;

use common::Harness;
use gas_meter::{ExecStatus, MeteringVersion};
use sha2::{Digest, Sha256};

fn guest_iterated_hash(h: &Harness, msg: &[u8], iter: u32, v: MeteringVersion) -> Vec<u8> {
    let mut input = iter.max(1).to_le_bytes().to_vec();
    input.extend_from_slice(msg);
    let r = h.sample("sha256guest", v, &input);
    assert_eq!(r.status, ExecStatus::Success, "{}", r.termination_reason);
    common::receipt_output(&r)
}

fn reference_iterated(iter: u32, msg: &[u8]) -> [u8; 32] {
    let mut d = Sha256::digest(msg);
    for _ in 1..iter.max(1) {
        d = Sha256::digest(d);
    }
    d.into()
}

#[test]
fn sha256_known_vectors() {
    let h = Harness::new();
    // NIST FIPS 180 经典向量
    let cases: &[(&[u8], &str)] = &[
        (
            b"",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        ),
        (
            b"abc",
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        ),
        (
            b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
        ),
    ];
    for (msg, expected) in cases {
        let got = guest_iterated_hash(&h, msg, 1, MeteringVersion::V1);
        assert_eq!(hex::encode(&got), *expected, "msg={msg:?}");
        // 独立宿主实现交叉验证
        assert_eq!(hex::encode(Sha256::digest(msg)), *expected);
    }
}

#[test]
fn sha256_iterated_chains_are_real() {
    let h = Harness::new();
    for iter in [1u32, 2, 3, 10, 100] {
        let got = guest_iterated_hash(&h, b"chain-test", iter, MeteringVersion::V1);
        let exp = reference_iterated(iter, b"chain-test");
        assert_eq!(got, exp, "iter={iter}");
    }
}

/// 超过消息长度上限（guest 只读前 4096 字节）仍需与“截断输入”一致——
/// 这里直接验证一个长输入确实被真实处理而不是返回常量。
#[test]
fn sha256_long_message() {
    let h = Harness::new();
    let msg = vec![0xABu8; 1000];
    let got = guest_iterated_hash(&h, &msg, 1, MeteringVersion::V1);
    // guest 只读 4096 窗口，1000 字节完整在内。
    assert_eq!(hex::encode(got), hex::encode(Sha256::digest(&msg)));
}

/// 高迭代次数消耗更多燃料，且在极小燃料预算下被真实终止（而非伪造结果）。
#[test]
fn sha256_iterations_burn_fuel_and_can_be_killed() {
    let h = Harness::new();
    let mut input = 100u32.to_le_bytes().to_vec();
    input.extend_from_slice(b"fuel");
    let r_low = h.sample("sha256guest", MeteringVersion::V1, &input);
    let r_one = {
        let mut i = 1u32.to_le_bytes().to_vec();
        i.extend_from_slice(b"fuel");
        h.sample("sha256guest", MeteringVersion::V1, &i)
    };
    assert!(
        r_low.total_fuel_consumed > r_one.total_fuel_consumed,
        "more iterations must burn more fuel"
    );

    // 极小预算：应 OutOfFuel，且不提交 sha256 键。
    let h2 = Harness::new();
    let mut req = gas_meter::ExecRequest::new(
        gas_meter::samples::compile(gas_meter::samples::by_id("sha256guest").unwrap()).unwrap(),
        input.clone(),
        MeteringVersion::V1,
    );
    req.fuel_limit = 50;
    let r = h2.run_req(req);
    assert_eq!(r.status, ExecStatus::OutOfFuel);
    assert!(!r.committed);
    assert!(h2.host.current().get("sha256").is_none());
}
