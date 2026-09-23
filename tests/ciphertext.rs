//! 关键验收：绝不把密文/随机字节误解析为 ClientHello。
//!
//! 覆盖：
//! * ClientHello 之后的 CCS + app_data 只被计数，不再解析；
//! * 连接开头就是随机 app_data，即使字节中恰好含 0x16/0x0303 模式；
//! * 大量确定性伪随机记录的模糊测试（fuzz）：任何情况下都不得出现伪造的 ClientHello，
//!   解析器也不得 panic。

use tls_observer::record::{
    CONTENT_ALERT, CONTENT_APPLICATION_DATA, CONTENT_CHANGE_CIPHER_SPEC, CONTENT_HANDSHAKE,
};
use tls_observer::test_support::*;
use tls_observer::{Conclusion, Config, NoHelloReason, Observer};

/// 确定性 LCG 伪随机字节（与 gen-samples 独立，避免样例偏向）。
fn lcg_bytes(n: usize, seed: u64) -> Vec<u8> {
    let mut s = seed.max(1) ^ 0x9E3779B97F4A7C15;
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        s = s
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        out.push((s >> 32) as u8);
    }
    out
}

#[test]
fn app_data_after_valid_hello_is_never_reparsed() {
    let hello = ClientHelloBuilder::new()
        .with_sni("secure.example")
        .with_alpn(&["h2"])
        .build_message();
    let mut data = record(CONTENT_HANDSHAKE, 0x0301, &hello);
    data.extend_from_slice(&record(CONTENT_CHANGE_CIPHER_SPEC, 0x0301, &[0x01]));
    // 密文中刻意嵌入一个“像握手记录头”的 5 字节模式：0x16 0x03 0x03 + 长度。
    let mut ct = lcg_bytes(80, 42);
    ct[10..15].copy_from_slice(&[0x16, 0x03, 0x03, 0x10, 0x00]);
    data.extend_from_slice(&record(CONTENT_APPLICATION_DATA, 0x0303, &ct));
    data.extend_from_slice(&record(
        CONTENT_APPLICATION_DATA,
        0x0303,
        &lcg_bytes(40, 43),
    ));

    let mut obs = Observer::new(Config::default());
    // 逐字节喂入，制造最坏的增量边界。
    for b in data.chunks(1) {
        obs.feed(b).unwrap();
    }
    let conclusion = obs.finish().unwrap();
    match conclusion {
        Conclusion::ClientHello(ch) => {
            assert_eq!(ch.sni.as_deref(), Some("secure.example"));
        }
        other => panic!("expected the real ClientHello, got {other:?}"),
    }
    // 后两条 app_data 与 CCS 都不得标记为明文握手。
    let plaintext_records = obs
        .records()
        .iter()
        .filter(|r| r.parsed_as_plaintext)
        .count();
    assert_eq!(plaintext_records, 1, "only the hello record is plaintext");
    assert_eq!(obs.records().len(), 4);
}

#[test]
fn random_app_data_first_yields_no_client_hello() {
    for seed in 0..200u64 {
        let payload = lcg_bytes(200, seed.wrapping_mul(99991) + 7);
        let data = record(CONTENT_APPLICATION_DATA, 0x0303, &payload);
        let mut obs = Observer::new(Config::default());
        // 随机切分大小。
        let step = (seed % 37 + 1) as usize;
        for part in data.chunks(step) {
            obs.feed(part).unwrap();
        }
        match obs.finish().unwrap() {
            Conclusion::NoClientHello(NoHelloReason::EncryptedDataWithoutHandshake) => {}
            other => panic!("seed {seed}: unexpected {other:?}"),
        }
        assert!(
            obs.client_hello().is_none(),
            "seed {seed} fabricated a hello"
        );
    }
}

#[test]
fn fuzz_random_records_never_fabricates_hello() {
    // 混合 23/20/21 记录，payload 完全随机；任何情况下都不能解析出 ClientHello。
    for seed in 0..500u64 {
        let mut data = Vec::new();
        let mut s = seed.max(1) ^ 0xDEADBEEF;
        let n_records = (seed % 6) + 1;
        for _ in 0..n_records {
            s = s.wrapping_mul(2862933555777941757).wrapping_add(3037000493);
            let ct = match s % 3 {
                0 => CONTENT_APPLICATION_DATA,
                1 => CONTENT_CHANGE_CIPHER_SPEC,
                _ => CONTENT_ALERT,
            };
            let len = (s >> 20) % 120;
            let payload = lcg_bytes(len as usize, s ^ seed);
            data.extend_from_slice(&record(ct, 0x0303, &payload));
        }
        let mut obs = Observer::new(Config::default());
        let res = obs.feed(&data);
        // 合法记录头 + 随机 app payload：不会触发解析层错误（内容不被检查）；
        // 即便未来收紧，也绝不能产出 ClientHello。
        if res.is_ok() {
            let _ = obs.finish();
        }
        assert!(
            obs.client_hello().is_none(),
            "seed {seed}: ciphertext produced a fake ClientHello!"
        );
    }
}

#[test]
fn interleaved_record_during_handshake_is_not_guessed() {
    // ClientHello 被拆成两条 handshake 记录，中间插入一条 app_data：
    // 明文结构已破坏，必须拒绝继续，不得把后续字节当 ClientHello。
    let hello = ClientHelloBuilder::new()
        .with_sni("mid.example")
        .build_message();
    let cut = 10;
    let mut data = record(CONTENT_HANDSHAKE, 0x0301, &hello[..cut]);
    data.extend_from_slice(&record(CONTENT_APPLICATION_DATA, 0x0303, &lcg_bytes(24, 5)));
    data.extend_from_slice(&record(CONTENT_HANDSHAKE, 0x0301, &hello[cut..]));

    let mut obs = Observer::new(Config::default());
    obs.feed(&data).unwrap();
    match obs.finish().unwrap() {
        Conclusion::NoClientHello(NoHelloReason::RecordInterleaved { content_type: 23 }) => {}
        other => panic!("expected RecordInterleaved(23), got {other:?}"),
    }
}
