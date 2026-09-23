//! 观察器状态机与跨记录/跨 TCP 分片重组测试。

use tls_observer::record::CONTENT_HANDSHAKE;
use tls_observer::test_support::*;
use tls_observer::{Conclusion, Config, Observer};

fn good_hello_message() -> Vec<u8> {
    ClientHelloBuilder::new()
        .with_grease_extension(0x2A2A)
        .with_sni("example.com")
        .with_alpn(&["h2", "http/1.1"])
        .with_supported_versions(&[0x2A2A, 0x0304])
        .with_unknown_extension(0x0017, &[1, 2, 3])
        .build_message()
}

fn feed_all(obs: &mut Observer, data: &[u8], chunk: usize) {
    for part in data.chunks(chunk.max(1)) {
        obs.feed(part).unwrap();
    }
}

#[test]
fn single_record_valid_client_hello() {
    let data = record(CONTENT_HANDSHAKE, 0x0301, &good_hello_message());
    let mut obs = Observer::new(Config::default());
    obs.feed(&data).unwrap();
    let conclusion = obs.finish().unwrap();
    let ch = match conclusion {
        Conclusion::ClientHello(ch) => ch,
        other => panic!("expected ClientHello, got {other:?}"),
    };
    assert_eq!(ch.sni.as_deref(), Some("example.com"));
    assert_eq!(ch.alpn, vec!["h2".to_string(), "http/1.1".to_string()]);
    assert!(ch.supported_versions.contains(&0x0304));
    // GREASE 已从版本/套件中剔除，但单独留痕。
    assert!(!ch.supported_versions.contains(&0x2A2A));
    assert!(ch.grease_versions.contains(&0x2A2A));
    assert!(ch.grease_extensions.contains(&0x2A2A));
    assert!(ch.grease_cipher_suites.contains(&0x2A2A));
    assert!(!ch.cipher_suites.contains(&0x2A2A));
    // 未知扩展登记，预览前 3 字节。
    let unknown = ch
        .unknown_extensions
        .iter()
        .find(|u| u.ext_type == 0x0017)
        .expect("unknown ext 0x0017");
    assert_eq!(unknown.data_len, 3);
    assert_eq!(unknown.data_preview, vec![1, 2, 3]);
    // 该记录被当作明文。
    assert!(obs.records()[0].parsed_as_plaintext);
}

#[test]
fn handshake_split_across_three_records() {
    let msg = good_hello_message();
    // 切点：2 字节（握手头前 2）、7（跨过长度字段）、100（body 内部）。
    let data = split_across_records(&msg, &[2, 7, 100]);
    let mut obs = Observer::new(Config::default());
    obs.feed(&data).unwrap();
    let ch = match obs.finish().unwrap() {
        Conclusion::ClientHello(ch) => ch,
        other => panic!("expected ClientHello, got {other:?}"),
    };
    assert_eq!(ch.sni.as_deref(), Some("example.com"));
    assert_eq!(ch.alpn.len(), 2);
}

#[test]
fn arbitrary_tcp_segmentation_gives_same_result() {
    let msg = good_hello_message();
    let data = record(CONTENT_HANDSHAKE, 0x0301, &msg);
    for chunk_size in [1, 2, 3, 5, 7, 13, 64, 1000, 4096] {
        let mut obs = Observer::new(Config::default());
        feed_all(&mut obs, &data, chunk_size);
        let conclusion = obs
            .finish()
            .unwrap_or_else(|e| panic!("chunk_size={chunk_size} unexpected error: {e}"));
        match conclusion {
            Conclusion::ClientHello(ch) => {
                assert_eq!(ch.sni.as_deref(), Some("example.com"));
            }
            other => panic!("chunk_size={chunk_size}: {other:?}"),
        }
    }
}

#[test]
fn multiple_handshake_messages_do_not_break_first_hello() {
    // 同一记录里：ClientHello + 多余尾巴（构造上附加一段“下一条消息”）。
    let mut frag = good_hello_message();
    frag.extend_from_slice(&[0x16, 0x00, 0x00, 0x04, 0xDE, 0xAD, 0xBE, 0xEF]);
    let data = record(CONTENT_HANDSHAKE, 0x0301, &frag);
    let mut obs = Observer::new(Config::default());
    obs.feed(&data).unwrap();
    // 第一条是 ClientHello 即成功；尾巴不再解析（明文目标已达成）。
    assert!(matches!(obs.finish().unwrap(), Conclusion::ClientHello(_)));
}

#[test]
fn zero_length_handshake_body_is_handled() {
    // msg_type=1 (ClientHello) 但 body 长度=0：进入 ClientHello 解析，
    // 因缺少 legacy_version 而报结构错误（不能被“头未确定”状态吞掉）。
    let data = record(CONTENT_HANDSHAKE, 0x0301, &[0x01, 0x00, 0x00, 0x00]);
    let mut obs = Observer::new(Config::default());
    let err = obs.feed(&data).unwrap_err();
    // 体内连 2 字节版本都没有：LengthMismatch（声明 0 边界内读取失败由解析处理）。
    // body 长度 0 时解析器在第一个 u16 上得到 LengthMismatch/InvalidValue 均可，
    // 关键是必须是硬错误、且不是 Truncated。
    assert!(
        !err.is_truncated(),
        "zero-len body must be hard error: {err}"
    );
}

#[test]
fn ccs_and_alert_before_hello_are_nonfatal() {
    let mut data = record(20, 0x0301, &[0x01]);
    data.extend_from_slice(&record(21, 0x0301, &[0x01, 0x00]));
    data.extend_from_slice(&record(CONTENT_HANDSHAKE, 0x0301, &good_hello_message()));
    let mut obs = Observer::new(Config::default());
    obs.feed(&data).unwrap();
    assert!(matches!(obs.finish().unwrap(), Conclusion::ClientHello(_)));
    // 前两条不是明文握手。
    assert!(!obs.records()[0].parsed_as_plaintext);
    assert!(!obs.records()[1].parsed_as_plaintext);
    assert!(obs.records()[2].parsed_as_plaintext);
}

#[test]
fn record_header_arrives_byte_by_byte() {
    let data = record(CONTENT_HANDSHAKE, 0x0301, &good_hello_message());
    let mut obs = Observer::new(Config::default());
    // 只喂头的前 4 字节：未到齐，无记录、无错误。
    obs.feed(&data[..4]).unwrap();
    assert!(obs.records().is_empty());
    // 第 5 字节 + fragment 逐步补齐。
    obs.feed(&data[4..8]).unwrap();
    assert!(obs.records().is_empty());
    obs.feed(&data[8..]).unwrap();
    assert!(matches!(obs.finish().unwrap(), Conclusion::ClientHello(_)));
}

#[test]
fn truncated_record_at_eof_is_error() {
    let full = record(CONTENT_HANDSHAKE, 0x0301, &good_hello_message());
    let truncated = &full[..full.len() - 9];
    let mut obs = Observer::new(Config::default());
    obs.feed(truncated).unwrap();
    let err = obs.finish().unwrap_err();
    assert!(matches!(
        err,
        tls_observer::ParseError::Truncated { what: "record" }
    ));
}

#[test]
fn truncated_handshake_header_at_eof_is_error() {
    // fragment 仅 3 字节，不足 4 字节握手头。
    let data = record(CONTENT_HANDSHAKE, 0x0301, &[1, 0, 1]);
    let mut obs = Observer::new(Config::default());
    obs.feed(&data).unwrap();
    let err = obs.finish().unwrap_err();
    assert!(matches!(
        err,
        tls_observer::ParseError::Truncated {
            what: "handshake_header"
        }
    ));
}

#[test]
fn empty_connection_reports_connection_ended() {
    let mut obs = Observer::new(Config::default());
    assert!(matches!(
        obs.finish().unwrap(),
        Conclusion::NoClientHello(tls_observer::NoHelloReason::ConnectionEnded)
    ));
}
