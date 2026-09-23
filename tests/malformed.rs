//! 畸形报文：重复扩展、嵌套长度不符、各种硬错误与长度上限。

use tls_observer::record::CONTENT_HANDSHAKE;
use tls_observer::test_support::*;
use tls_observer::{Config, Observer, ParseError};

fn run(data: &[u8]) -> ParseError {
    let mut obs = Observer::new(Config::default());
    obs.feed(data).unwrap_err()
}

#[test]
fn duplicate_extension_is_rejected() {
    let msg = ClientHelloBuilder::new()
        .with_sni("a.example")
        .with_sni("b.example")
        .build_message();
    let err = run(&record(CONTENT_HANDSHAKE, 0x0301, &msg));
    match err {
        ParseError::DuplicateExtension { ext_type: 0x0000 } => {}
        other => panic!("expected DuplicateExtension(0x0000), got {other:?}"),
    }
}

#[test]
fn duplicate_grease_extension_is_allowed() {
    // 两个相同的 GREASE 槽位是合法的（GREASE 不参与去重）。
    let msg = ClientHelloBuilder::new()
        .with_grease_extension(0x2A2A)
        .with_grease_extension(0x2A2A)
        .with_sni("ok.example")
        .build_message();
    let mut obs = Observer::new(Config::default());
    obs.feed(&record(CONTENT_HANDSHAKE, 0x0301, &msg)).unwrap();
    assert!(obs.client_hello().is_some());
}

#[test]
fn nested_length_mismatch_in_sni() {
    // 扩展外层 data=8 字节，内层 list 却声明 100。
    let mut data = Vec::new();
    data.extend_from_slice(&100u16.to_be_bytes());
    data.push(0);
    data.extend_from_slice(&3u16.to_be_bytes());
    data.extend_from_slice(b"abc");
    let msg = ClientHelloBuilder::new()
        .with_raw_extension(extension(0x0000, &data))
        .build_message();
    let err = run(&record(CONTENT_HANDSHAKE, 0x0301, &msg));
    assert!(
        matches!(
            err,
            ParseError::LengthMismatch { field, declared: 100, .. } if field == "server_name_list"
        ),
        "got {err:?}"
    );
}

#[test]
fn extensions_block_declares_more_than_body_has() {
    // 手工组装 body，精确掌握扩展块长度字段的位置，再把声明值改大。
    let ext_block = sni_extension("x.example"); // 长度确定：18 字节
    let mut body = Vec::new();
    body.extend_from_slice(&0x0303u16.to_be_bytes()); // legacy_version
    body.extend_from_slice(&[0x11; 32]); // random
    body.push(0); // session_id len 0
    body.extend_from_slice(&2u16.to_be_bytes()); // cipher_suites 长度
    body.extend_from_slice(&0x1301u16.to_be_bytes());
    body.push(1); // compression_methods 长度
    body.push(0);
    let ext_len_offset = body.len();
    body.extend_from_slice(&(ext_block.len() as u16).to_be_bytes());
    body.extend_from_slice(&ext_block);

    // 把扩展块声明长度改大 256（越出 body 实际边界）。
    body[ext_len_offset] = body[ext_len_offset].wrapping_add(1);
    let err = run(&record(CONTENT_HANDSHAKE, 0x0301, &handshake(1, &body)));
    assert!(
        matches!(
            err,
            ParseError::LengthMismatch {
                field: "extensions",
                ..
            }
        ),
        "got {err:?}"
    );
}

#[test]
fn record_too_large_is_rejected_immediately() {
    // 只发 5 字节头（声明 16385），即使一个 fragment 字节都没来也要立即失败。
    let data = record_with_declared_len(CONTENT_HANDSHAKE, 0x0301, 16385, &[]);
    let mut obs = Observer::new(Config::default());
    let err = obs.feed(&data[..5]).unwrap_err();
    assert!(matches!(
        err,
        ParseError::RecordTooLarge {
            len: 16385,
            max: 16384
        }
    ));
}

#[test]
fn custom_small_record_limit() {
    let cfg = Config {
        max_record_fragment: 100,
        ..Config::default()
    };
    let msg = ClientHelloBuilder::new()
        .with_sni("long.example")
        .build_message();
    let data = record(CONTENT_HANDSHAKE, 0x0301, &msg);
    let mut obs = Observer::new(cfg);
    // 正常 hello 远大于 100 字节 fragment。
    let err = obs.feed(&data).unwrap_err();
    assert!(matches!(err, ParseError::RecordTooLarge { max: 100, .. }));
}

#[test]
fn handshake_message_too_large() {
    // 声明 1MB+1 的握手体（u24），只给极少字节也要立即失败。
    let data = record(
        CONTENT_HANDSHAKE,
        0x0301,
        &handshake_with_declared_len(1, (1 << 20) + 1, &[0xAB; 4]),
    );
    let mut obs = Observer::new(Config::default());
    let err = obs.feed(&data).unwrap_err();
    assert!(matches!(
        err,
        ParseError::HandshakeTooLarge { len: 1048577, .. }
    ));
}

#[test]
fn bad_content_type_and_version() {
    assert!(matches!(
        run(&record_with_declared_len(30, 0x0301, 0, &[])),
        ParseError::BadRecordContentType(30)
    ));
    assert!(matches!(
        run(&record_with_declared_len(CONTENT_HANDSHAKE, 0x0002, 0, &[])),
        ParseError::BadRecordVersion {
            version: 0x0002,
            ..
        }
    ));
    // 0x0305 超出接受范围 0x0301..=0x0304。
    assert!(matches!(
        run(&record_with_declared_len(CONTENT_HANDSHAKE, 0x0305, 0, &[])),
        ParseError::BadRecordVersion {
            version: 0x0305,
            ..
        }
    ));
}

#[test]
fn first_handshake_message_not_client_hello() {
    let mut body = Vec::new();
    body.extend_from_slice(&0x0303u16.to_be_bytes());
    body.extend_from_slice(&[0x22; 32]);
    body.push(0);
    body.extend_from_slice(&0x1301u16.to_be_bytes());
    body.push(0);
    let data = record(CONTENT_HANDSHAKE, 0x0303, &handshake(2, &body));
    let mut obs = Observer::new(Config::default());
    obs.feed(&data).unwrap();
    match obs.finish().unwrap() {
        tls_observer::Conclusion::NoClientHello(
            tls_observer::NoHelloReason::FirstHandshakeNotClientHello { handshake_type: 2 },
        ) => {}
        other => panic!("unexpected: {other:?}"),
    }
}

#[test]
fn invalid_client_hello_values_are_hard_errors() {
    // cipher_suites 长度为奇数。
    let mut b = ClientHelloBuilder::new();
    b.cipher_suites = vec![0x1301, 0x13]; // 序列化后长度字段仍是 4 字节=>2 套件，不畸形；
                                          // 改为手工破坏：直接构造 cipher_suites 长度为 3 的 body。
    let _ = b;
    let mut body = Vec::new();
    body.extend_from_slice(&0x0303u16.to_be_bytes());
    body.extend_from_slice(&[0x11; 32]);
    body.push(0); // session_id len 0
    body.extend_from_slice(&3u16.to_be_bytes()); // cipher_suites 长度 3（奇数）
    body.extend_from_slice(&[0x13, 0x01, 0x13]);
    body.push(1); // compression_methods 长度 1
    body.push(0);
    body.extend_from_slice(&0u16.to_be_bytes()); // 空扩展块
    let err = run(&record(CONTENT_HANDSHAKE, 0x0301, &handshake(1, &body)));
    assert!(
        matches!(err, ParseError::InvalidValue { field, .. } if field == "cipher_suites"),
        "got {err:?}"
    );
}

#[test]
fn observer_is_sticky_after_hard_error() {
    let bad = record_with_declared_len(30, 0x0301, 0, &[]);
    let mut obs = Observer::new(Config::default());
    let first = obs.feed(&bad).unwrap_err();
    // 再喂正常报文也不会“恢复”。
    let good = record(
        CONTENT_HANDSHAKE,
        0x0301,
        &ClientHelloBuilder::new()
            .with_sni("a.example")
            .build_message(),
    );
    let second = obs.feed(&good).unwrap_err();
    assert_eq!(first, second);
}
