//! 端到端：真实 TCP 回环 + tls_observer::server 的观察函数。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use tls_observer::config::Config;
use tls_observer::record::CONTENT_HANDSHAKE;
use tls_observer::server::{observe_stream, Termination};
use tls_observer::test_support::*;
use tls_observer::Conclusion;

/// 在随机本地端口上建立一对连接，`writer` 在线程里发送数据并半关闭。
fn with_connection<F>(writer: F) -> tls_observer::server::Report
where
    F: FnOnce(&mut TcpStream) + Send + 'static,
{
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();

    let handle = std::thread::spawn(move || {
        let mut client = TcpStream::connect(addr).unwrap();
        writer(&mut client);
        client.flush().unwrap();
        // shutdown 写端，触发服务端读到 EOF。
        client
            .shutdown(std::net::Shutdown::Write)
            .unwrap_or_default();
        // 保持读端一小段时间，避免 FIN 后立刻 RST。
        client
            .set_read_timeout(Some(Duration::from_millis(100)))
            .ok();
        let mut buf = [0u8; 16];
        let _ = client.read(&mut buf);
    });

    let (mut server, _) = listener.accept().unwrap();
    server
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let report = observe_stream(&mut server, &Config::default());
    handle.join().unwrap();
    report
}

#[test]
fn e2e_valid_hello_in_one_write() {
    let msg = ClientHelloBuilder::new()
        .with_sni("live.example")
        .with_alpn(&["h2", "http/1.1"])
        .build_message();
    let data = record(CONTENT_HANDSHAKE, 0x0301, &msg);

    let report = with_connection(move |s| {
        s.write_all(&data).unwrap();
    });
    assert_eq!(report.termination, Termination::Eof);
    assert!(
        report.error.is_none(),
        "unexpected error: {:?}",
        report.error
    );
    match report.conclusion.as_ref().unwrap() {
        Conclusion::ClientHello(ch) => {
            assert_eq!(ch.sni.as_deref(), Some("live.example"));
            assert_eq!(ch.alpn, vec!["h2", "http/1.1"]);
        }
        other => panic!("{other:?}"),
    }
    assert!(report.to_json().contains("live.example"));
    assert_valid_json_hex(&report.to_json());
}

/// JSON 不允许 0x 十六进制数字字面量：协议值必须是带引号字符串。
fn assert_valid_json_hex(json: &str) {
    for bad in ["[0x", ",0x", ":0x"] {
        assert!(
            !json.contains(bad),
            "bare hex literal `{bad}` in JSON output: {json}"
        );
    }
}

#[test]
fn e2e_cross_write_boundaries() {
    let msg = ClientHelloBuilder::new()
        .with_sni("split.example")
        .with_alpn(&["h2"])
        .build_message();
    let data = record(CONTENT_HANDSHAKE, 0x0301, &msg);

    let report = with_connection(move |s| {
        // 模拟极小 TCP 段：每次写 1~3 字节并稍作停顿。
        let mut i = 0;
        let mut step = 1;
        while i < data.len() {
            let end = (i + step).min(data.len());
            s.write_all(&data[i..end]).unwrap();
            s.flush().unwrap();
            std::thread::sleep(Duration::from_micros(200));
            i = end;
            step = if step == 1 { 3 } else { 1 };
        }
    });
    assert!(report.error.is_none(), "{:?}", report.error);
    match report.conclusion.as_ref().unwrap() {
        Conclusion::ClientHello(ch) => assert_eq!(ch.sni.as_deref(), Some("split.example")),
        other => panic!("{other:?}"),
    }
}

#[test]
fn e2e_ciphertext_only_reports_no_hello() {
    let ct = record(23, 0x0303, &(0..64u8).collect::<Vec<u8>>());
    let report = with_connection(move |s| {
        s.write_all(&ct).unwrap();
    });
    assert!(report.error.is_none());
    assert!(matches!(
        report.conclusion.as_ref().unwrap(),
        Conclusion::NoClientHello(tls_observer::NoHelloReason::EncryptedDataWithoutHandshake)
    ));
    let json = report.to_json();
    assert!(json.contains("encrypted_data_without_handshake"));
}

#[test]
fn e2e_parse_error_is_reported_in_json() {
    // content_type=99 => BadRecordContentType
    let bad = record_with_declared_len(99, 0x0301, 0, &[]);
    let report = with_connection(move |s| {
        s.write_all(&bad).unwrap();
    });
    assert!(report.error.is_some());
    assert_eq!(report.termination, Termination::ParseError);
    let json = report.to_json();
    assert!(json.contains("parse_error"));
    assert!(json.contains("content type"));
}
