//! 真实 TCP 回环的服务端到端测试：起一个绑定随机端口的本地监听，
//! 用裸 socket 发 HTTP 请求，验证状态码、错误标签与正文指纹。
//! 其中“慢速”用例把请求体**逐字节**写入，服务端 `read_size=1`，
//! 从而强制每条边界都横跨 TCP 读取块。

use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

use minimultipart::server::{handle_connection, ServerConfig};
use minimultipart::sha256::Sha256;
use minimultipart::Limits;

/// 起一个一次性测试服务，返回 (端口, 终止信号)。每个连接单线程处理。
fn spawn_test_server(config: ServerConfig) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    thread::spawn(move || {
        for stream in listener.incoming() {
            match stream {
                Ok(s) => handle_connection(s, config),
                Err(_) => break,
            }
        }
    });
    port
}

fn raw_request(port: u16, content_type: &str, body: &[u8]) -> (u16, Vec<u8>) {
    let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let head = format!(
        "POST /upload HTTP/1.1\r\nHost: x\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    s.write_all(head.as_bytes()).unwrap();
    s.write_all(body).unwrap();
    read_response(&mut s)
}

/// 逐字节慢速发送（Nagle 可能合并，但服务端每次只读 1 字节，解析器看到的仍是 1 字节块）。
fn slow_request(port: u16, content_type: &str, body: &[u8]) -> (u16, Vec<u8>) {
    let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    s.set_nodelay(true).unwrap();
    let head = format!(
        "POST /upload HTTP/1.1\r\nHost: x\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    let mut all = head.into_bytes();
    all.extend_from_slice(body);
    for b in all {
        s.write_all(&[b]).unwrap();
    }
    read_response(&mut s)
}

fn read_response(s: &mut std::net::TcpStream) -> (u16, Vec<u8>) {
    let mut all = Vec::new();
    let mut buf = [0u8; 1024];
    loop {
        match s.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => all.extend_from_slice(&buf[..n]),
            Err(_) => break,
        }
    }
    let status = std::str::from_utf8(&all)
        .ok()
        .and_then(|t| t.split_whitespace().nth(1))
        .and_then(|c| c.parse().ok())
        .unwrap_or(0);
    let body = all
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .map(|p| all[p + 4..].to_vec())
        .unwrap_or_default();
    (status, body)
}

fn multipart_body(boundary: &str, parts: &[(&str, Option<&str>, &[u8])]) -> Vec<u8> {
    let mut v = Vec::new();
    for (name, filename, data) in parts {
        v.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        match filename {
            Some(f) => v.extend_from_slice(
                format!(
                    "Content-Disposition: form-data; name=\"{name}\"; filename=\"{f}\"\r\n\r\n"
                )
                .as_bytes(),
            ),
            None => v.extend_from_slice(
                format!("Content-Disposition: form-data; name=\"{name}\"\r\n\r\n").as_bytes(),
            ),
        }
        v.extend_from_slice(data);
        v.extend_from_slice(b"\r\n");
    }
    v.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
    v
}

#[test]
fn valid_request_normal_and_byte_at_a_time() {
    // 服务端每次只读 1 字节：read_size=1
    let config = ServerConfig {
        limits: Limits::default(),
        read_size: 1,
        timeout: Duration::from_secs(5),
    };
    let port = spawn_test_server(config);

    // 二进制正文，内含裸边界与“后缀不符”的假边界（但不能出现真的 \r\n--B--）
    let binary: Vec<u8> = b"bin--Bdata\r\n--B-x\r\n--Bz-end\x00\xff".to_vec();
    let body = multipart_body(
        "B",
        &[
            ("a", None, b"hello"),
            ("empty", None, b""),
            ("up", Some("f.bin"), &binary),
        ],
    );
    let ct = "multipart/form-data; boundary=B";

    let (status, resp) = raw_request(port, ct, &body);
    assert_eq!(status, 200, "普通发送: {}", String::from_utf8_lossy(&resp));
    let text = String::from_utf8(resp).unwrap();
    assert!(text.contains("\"ok\":true"));
    assert!(text.contains("\"part_count\":3"));
    assert!(text.contains("\"name\":\"a\""));
    assert!(text.contains("\"size\":5"));
    assert!(text.contains("\"name\":\"empty\""));
    assert!(text.contains("\"filename\":\"f.bin\""));
    assert!(text.contains(&format!("\"size\":{}", binary.len())));
    assert!(text.contains(&Sha256::hex(&binary)));

    // 完全相同的请求，逐字节发送，结果必须一致
    let (status2, resp2) = slow_request(port, ct, &body);
    assert_eq!(
        status2,
        200,
        "逐字节发送: {}",
        String::from_utf8_lossy(&resp2)
    );
    assert_eq!(resp2, text.as_bytes());
}

#[test]
fn missing_close_boundary_returns_400_truncated() {
    let config = ServerConfig {
        limits: Limits::default(),
        read_size: 3,
        timeout: Duration::from_secs(5),
    };
    let port = spawn_test_server(config);
    let mut body = multipart_body("B", &[("a", None, b"v")]);
    // 去掉关闭边界（连带前面的 CRLF 一并改成“永远等下一段”的状态）
    let marker = b"\r\n--B--\r\n";
    let cut = body.len() - marker.len();
    body.truncate(cut + 2); // 只保留正文后的 CRLF，没有边界
    let (status, resp) = raw_request(port, "multipart/form-data; boundary=B", &body);
    assert_eq!(status, 400);
    let text = String::from_utf8_lossy(&resp);
    assert!(text.contains("\"error\":\"truncated\""), "响应: {text}");
}

#[test]
fn too_many_parts_returns_413() {
    let limits = Limits {
        max_parts: 1,
        ..Limits::tiny()
    };
    let config = ServerConfig {
        limits,
        read_size: 2,
        timeout: Duration::from_secs(5),
    };
    let port = spawn_test_server(config);
    let body = multipart_body("B", &[("a", None, b"v"), ("b", None, b"w")]);
    let (status, resp) = raw_request(port, "multipart/form-data; boundary=B", &body);
    assert_eq!(status, 413);
    let text = String::from_utf8_lossy(&resp);
    assert!(
        text.contains("\"error\":\"too_many_parts\""),
        "响应: {text}"
    );
}

#[test]
fn oversized_part_returns_413() {
    let limits = Limits {
        max_parts: 8,
        ..Limits::tiny()
    }; // max_part_size=10
    let config = ServerConfig {
        limits,
        read_size: 4,
        timeout: Duration::from_secs(5),
    };
    let port = spawn_test_server(config);
    let big = vec![b'x'; 50];
    let body = multipart_body("B", &[("a", None, &big)]);
    let (status, resp) = raw_request(port, "multipart/form-data; boundary=B", &body);
    assert_eq!(status, 413);
    assert!(String::from_utf8_lossy(&resp).contains("part_too_large"));
}

#[test]
fn wrong_content_type_and_method_rejected() {
    let config = ServerConfig::default();
    let port = spawn_test_server(config);

    // 不是 multipart/form-data
    let (status, resp) = raw_request(port, "application/x-www-form-urlencoded", b"a=b");
    assert_eq!(status, 400);
    assert!(String::from_utf8_lossy(&resp).contains("invalid_boundary"));

    // GET 请求
    let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
    s.write_all(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n").unwrap();
    let (status, _) = read_response(&mut s);
    assert_eq!(status, 405);
}

#[test]
fn early_eof_before_content_length_is_truncated() {
    let config = ServerConfig::default();
    let port = spawn_test_server(config);
    let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    // 声称有 100 字节正文，实际只发 5 字节就半关闭
    s.write_all(
        b"POST /upload HTTP/1.1\r\nHost: x\r\nContent-Type: multipart/form-data; boundary=B\r\n\
          Content-Length: 100\r\n\r\n--B\r",
    )
    .unwrap();
    s.shutdown(std::net::Shutdown::Write).unwrap();
    let (status, resp) = read_response(&mut s);
    assert_eq!(status, 400);
    assert!(
        String::from_utf8_lossy(&resp).contains("truncated_request")
            || String::from_utf8_lossy(&resp).contains("truncated")
    );
}
