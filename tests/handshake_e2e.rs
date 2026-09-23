//! 验收场景 6：RFC 6455 HTTP 握手（Accept 计算、非法握手拒绝）+ 握手后真实收发。
mod common;

use std::io::{Read, Write};

use common::*;

/// 标准握手：服务端返回 101 与 RFC 示例一致的 Sec-WebSocket-Accept。
#[test]
fn handshake_returns_rfc_accept_then_echoes() {
    let (mut s, _) = start_server(false, None);
    let resp = do_handshake(&mut s);
    assert!(resp.starts_with("HTTP/1.1 101 Switching Protocols"), "{resp}");
    assert!(resp.contains("Upgrade: websocket"), "{resp}");
    assert!(resp.contains("Connection: Upgrade"), "{resp}");
    // RFC 6455 §4.2.2 示例的确定性输出
    assert!(
        resp.contains("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo="),
        "{resp}"
    );

    // 握手完成后帧处理仍然正常
    send_byte_by_byte(&mut s, &text(true, "post-handshake", [1; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.payload, b"post-handshake");
}

/// 缺少 Sec-WebSocket-Key 的升级请求应被拒绝（400）。
#[test]
fn handshake_without_key_is_rejected() {
    let (mut s, _) = start_server(false, None);
    let bad = "GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n";
    s.write_all(bad.as_bytes()).unwrap();
    s.flush().unwrap();
    let mut resp = Vec::new();
    let mut buf = [0u8; 64];
    // 读到 EOF
    loop {
        match s.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => resp.extend_from_slice(&buf[..n]),
            Err(_) => break,
        }
    }
    let text = String::from_utf8_lossy(&resp);
    assert!(text.starts_with("HTTP/1.1 400"), "got: {text}");
}

/// 握手时的字节可能被逐字节发送，服务端按行缓冲后仍应成功。
#[test]
fn handshake_request_sent_one_byte_at_a_time() {
    let (mut s, _) = start_server(false, None);
    let key = "dGhlIHNhbXBsZSBub25jZQ==";
    let req = format!(
        "GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    );
    send_byte_by_byte(&mut s, req.as_bytes());
    let resp = do_handshake_read(&mut s);
    assert!(resp.contains("101 Switching Protocols"), "{resp}");
}

/// 与 `do_handshake` 相同的读取逻辑（单独命名以表达“这次没有提前读”）。
fn do_handshake_read(s: &mut std::net::TcpStream) -> String {
    let mut resp = Vec::new();
    let mut byte = [0u8; 1];
    while resp.windows(4).last() != Some(b"\r\n\r\n".as_ref()) {
        s.read_exact(&mut byte).unwrap();
        resp.push(byte[0]);
    }
    String::from_utf8(resp).unwrap()
}
