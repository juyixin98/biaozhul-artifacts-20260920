//! 集成测试：往返一致性、确定性、重标定、截断流、资源限制、CLI。

use adaptive_arith::{decode, encode, Error, Limits};

fn limits() -> Limits {
    Limits::default()
}

fn roundtrip(data: &[u8]) -> (Vec<u8>, adaptive_arith::CodecStats) {
    let lim = limits();
    let mut compressed = Vec::new();
    let stats = encode(data, &mut compressed, &lim).expect("encode failed");
    let mut restored = Vec::new();
    decode(&compressed[..], &mut restored, &lim).expect("decode failed");
    assert_eq!(restored, data, "roundtrip mismatch");
    (compressed, stats)
}

/// 确定性伪随机数据（LCG），避免测试依赖外部随机源。
fn lcg_bytes(seed: u64, n: usize) -> Vec<u8> {
    let mut x = seed;
    (0..n)
        .map(|_| {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (x >> 33) as u8
        })
        .collect()
}

#[test]
fn empty_input_roundtrip() {
    let (compressed, stats) = roundtrip(b"");
    // 空输入只编码一个 EOF 符号，输出应非常短
    assert!(compressed.len() <= 8, "compressed: {compressed:02x?}");
    assert_eq!(stats.input_bytes, 0);
}

#[test]
fn single_byte_roundtrip() {
    for b in [0x00u8, 0x41, 0x7F, 0x80, 0xFF] {
        roundtrip(&[b]);
    }
}

#[test]
fn all_byte_values_roundtrip() {
    // 覆盖全部 256 个字节值，重复 4 轮
    let data: Vec<u8> = (0..=255u8).cycle().take(256 * 4).collect();
    roundtrip(&data);
}

#[test]
fn text_roundtrip() {
    roundtrip(b"the quick brown fox jumps over the lazy dog. ".repeat(50).as_slice());
}

#[test]
fn pseudorandom_roundtrip() {
    let data = lcg_bytes(0xDEAD_BEEF, 100_000);
    roundtrip(&data);
}

#[test]
fn long_skewed_data_triggers_multiple_rescales() {
    // 长偏斜数据：约 99% 为 'a'
    let mut data = Vec::with_capacity(300_000);
    for i in 0..300_000u32 {
        data.push(if i % 256 == 0 { b'b' } else { b'a' });
    }
    let (compressed, stats) = roundtrip(&data);
    assert!(
        stats.rescale_count >= 2,
        "expected multiple rescales, got {}",
        stats.rescale_count
    );
    // 偏斜数据应显著压缩
    assert!(
        compressed.len() < data.len() / 4,
        "compressed {} bytes from {}",
        compressed.len(),
        data.len()
    );
}

#[test]
fn encoding_is_deterministic() {
    let data = lcg_bytes(42, 50_000);
    let lim = limits();
    let mut a = Vec::new();
    let mut b = Vec::new();
    encode(&data[..], &mut a, &lim).unwrap();
    encode(&data[..], &mut b, &lim).unwrap();
    assert_eq!(a, b, "same input must produce identical output");
}

#[test]
fn golden_vector_is_stable() {
    // 格式稳定性锚点：任何改动若改变输出字节，必须同步更新 FORMAT.md 与此常量。
    let mut compressed = Vec::new();
    encode(&b"Hello, arithmetic coding!"[..], &mut compressed, &limits()).unwrap();
    assert_eq!(
        compressed,
        GOLDEN_HELLO,
        "encoded bytes changed; if the format change is intentional, update FORMAT.md and GOLDEN_HELLO"
    );
    let mut restored = Vec::new();
    decode(&compressed[..], &mut restored, &limits()).unwrap();
    assert_eq!(restored, b"Hello, arithmetic coding!");
}

/// encode(b"Hello, arithmetic coding!") 的确定性输出。
const GOLDEN_HELLO: &[u8] = &[
    0x48, 0x1d, 0x84, 0x5d, 0x50, 0xde, 0x4e, 0xfc, 0xd3, 0x58, 0x2f, 0xb2, 0x2e, 0x7c, 0xd8,
    0xbe, 0x3d, 0xef, 0x72, 0x03, 0x4c, 0xf5, 0x83, 0x5c, 0x8c, 0x40,
];

#[test]
fn truncated_stream_is_an_error() {
    let data = lcg_bytes(7, 50_000);
    let lim = limits();
    let mut compressed = Vec::new();
    encode(&data[..], &mut compressed, &lim).unwrap();

    // 截断为前半段：解码必须报错（TruncatedStream），不得静默成功或 panic
    let truncated = &compressed[..compressed.len() / 2];
    let mut out = Vec::new();
    match decode(truncated, &mut out, &lim) {
        Err(Error::TruncatedStream) => {}
        other => panic!("expected TruncatedStream, got {:?}", other.map(|_| ())),
    }

    // 逐字节截断也要么报错、要么（仅在完整流时）正确解出
    for cut in [0, 1, compressed.len() / 4, compressed.len() - 1] {
        let mut out = Vec::new();
        let result = decode(&compressed[..cut], &mut out, &lim);
        assert!(result.is_err(), "cut={cut} should fail");
    }
}

#[test]
fn decode_output_limit_is_enforced() {
    let data = lcg_bytes(99, 10_000);
    let mut compressed = Vec::new();
    encode(&data[..], &mut compressed, &limits()).unwrap();

    let lim = Limits {
        max_output_bytes: 9_999,
        ..Limits::default()
    };
    let mut out = Vec::new();
    match decode(&compressed[..], &mut out, &lim) {
        Err(Error::OutputLimitExceeded) => {}
        other => panic!("expected OutputLimitExceeded, got {:?}", other.map(|_| ())),
    }
    assert!(out.len() <= 9_999);
}

#[test]
fn encode_output_limit_is_enforced() {
    let data = lcg_bytes(5, 100_000);
    let lim = Limits {
        max_output_bytes: 128,
        ..Limits::default()
    };
    let mut compressed = Vec::new();
    match encode(&data[..], &mut compressed, &lim) {
        Err(Error::OutputLimitExceeded) => {}
        other => panic!("expected OutputLimitExceeded, got {:?}", other.map(|_| ())),
    }
}

#[test]
fn encode_input_limit_is_enforced() {
    let data = vec![0u8; 1_000];
    let lim = Limits {
        max_input_bytes: 999,
        ..Limits::default()
    };
    let mut compressed = Vec::new();
    match encode(&data[..], &mut compressed, &lim) {
        Err(Error::InputLimitExceeded) => {}
        other => panic!("expected InputLimitExceeded, got {:?}", other.map(|_| ())),
    }
}

#[test]
fn corrupt_stream_never_panics() {
    // 垃圾输入：在输出限制下必须有限终止（Ok 或 Err 均可），不得 panic
    let garbage = lcg_bytes(0xC0FFEE, 4_096);
    let lim = Limits {
        max_output_bytes: 1_024,
        ..Limits::default()
    };
    let mut out = Vec::new();
    let _ = decode(&garbage[..], &mut out, &lim);
    assert!(out.len() <= 1_024);
}

#[test]
fn cli_encode_decode_roundtrip() {
    use std::io::Write as _;
    use std::process::{Command, Stdio};

    let exe = env!("CARGO_BIN_EXE_aac");
    let data = b"cli roundtrip \x00\x01\xFE\xFF payload".repeat(100);
    let request = serde_json::json!({
        "op": "encode",
        "input_base64": base64::Engine::encode(
            &base64::engine::general_purpose::STANDARD, &data),
    })
    .to_string();

    let mut child = Command::new(exe)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(request.as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    let response: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(response["ok"], true);
    assert_eq!(response["input_bytes"], data.len() as u64);

    // 解码回去
    let compressed_b64 = response["output_base64"].as_str().unwrap();
    let request = serde_json::json!({"op": "decode", "input_base64": compressed_b64}).to_string();
    let mut child = Command::new(exe)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(request.as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    let response: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(response["ok"], true);
    let restored = base64::Engine::decode(
        &base64::engine::general_purpose::STANDARD,
        response["output_base64"].as_str().unwrap(),
    )
    .unwrap();
    assert_eq!(restored, data);
}

#[test]
fn cli_rejects_malformed_request() {
    use std::io::Write as _;
    use std::process::{Command, Stdio};

    let exe = env!("CARGO_BIN_EXE_aac");
    let mut child = Command::new(exe)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(b"{\"op\":\"explode\"}")
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(2));
    let response: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(response["ok"], false);
    assert_eq!(response["error"], "malformed_request");
}
