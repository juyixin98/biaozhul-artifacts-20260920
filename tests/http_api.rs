//! HTTP 端到端测试：在随机端口启动真实 Axum 服务，用 TCP 发请求验证。
//! 不引入 reqwest/tower 测试依赖，直接用 std::net + 手拼 HTTP/1.1。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::{Arc, Mutex};

use ehindex::hash::HashKind;
use ehindex::index::Index;
use ehindex::server::{app, AppState};

fn nano() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

async fn spawn_server(kind: HashKind, cap: u32) -> (String, std::path::PathBuf) {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "ehindex-http-{}-{}.ehdb",
        std::process::id(),
        nano()
    ));
    let index = Index::create(&path, cap, kind).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let state = AppState {
        index: Arc::new(Mutex::new(index)),
    };
    // 在后台手动驱动 axum（用当前测试的 tokio 运行时）。
    tokio::spawn(async move {
        let _ = axum::serve(listener, app(state)).await;
    });
    (format!("http://{addr}"), path)
}

fn http(method: &str, url: &str, body: &str, ctype: &str) -> (u16, String) {
    let host = url
        .strip_prefix("http://")
        .and_then(|s| s.split('/').next())
        .unwrap();
    let path = url[("http://".len() + host.len())..].to_string();
    let path = if path.is_empty() { "/" } else { &path };
    let mut stream = TcpStream::connect(host).unwrap();
    stream
        .set_read_timeout(Some(std::time::Duration::from_secs(5)))
        .unwrap();
    stream
        .set_write_timeout(Some(std::time::Duration::from_secs(5)))
        .unwrap();
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes()).unwrap();
    let mut raw = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&chunk[..n]),
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => break,
            Err(e) => panic!("读响应失败：{e}"),
        }
    }
    let raw = String::from_utf8_lossy(&raw).into_owned();
    let mut parts = raw.splitn(2, "\r\n");
    let status_line = parts.next().unwrap();
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    let rest = parts.next().unwrap_or("");
    let body = rest
        .split_once("\r\n\r\n")
        .map(|x| x.1)
        .unwrap_or("")
        .to_string();
    (status, body)
}

fn post_json(base: &str, path: &str, json: &str) -> (u16, serde_json::Value) {
    let (s, b) = http("POST", &format!("{base}{path}"), json, "application/json");
    (
        s,
        serde_json::from_str(&b).unwrap_or(serde_json::Value::Null),
    )
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn http_crud_and_stats() {
    let (base, path) = spawn_server(HashKind::Fx, 4).await;

    let (s, _) = post_json(&base, "/put", r#"{"key":"alpha","value":"1"}"#);
    assert_eq!(s, 200);
    let (s, _) = post_json(&base, "/put", r#"{"key":"beta","value":"2"}"#);
    assert_eq!(s, 200);
    // 更新
    let (s, _) = post_json(&base, "/put", r#"{"key":"alpha","value":"11"}"#);
    assert_eq!(s, 200);

    let (s, v) = post_json(&base, "/get", r#"{"key":"alpha"}"#);
    assert_eq!(s, 200);
    assert_eq!(v["value"], "11");

    let (s, v) = post_json(&base, "/get", r#"{"key":"missing"}"#);
    assert_eq!(s, 404);
    assert_eq!(v["kind"], "not_found");

    let (s, v) = post_json(&base, "/delete", r#"{"key":"beta"}"#);
    assert_eq!(s, 200);
    assert_eq!(v["existed"], true);
    let (s, _) = post_json(&base, "/get", r#"{"key":"beta"}"#);
    assert_eq!(s, 404);

    let (s, body) = http("GET", &format!("{base}/stats"), "", "");
    assert_eq!(s, 200);
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["total_records"], 1);
    assert_eq!(v["hash"], "fx");
    assert_eq!(v["bucket_capacity"], 4);

    let (s, _) = http("GET", &format!("{base}/healthz"), "", "");
    assert_eq!(s, 200);

    drop(path); // 文件随临时目录清理（此处不显式删除也无妨）
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn http_full_collision_returns_507() {
    let (base, _path) = spawn_server(HashKind::Const(0), 2).await;
    let (s, _) = post_json(&base, "/put", r#"{"key":"a","value":"1"}"#);
    assert_eq!(s, 200);
    let (s, _) = post_json(&base, "/put", r#"{"key":"b","value":"2"}"#);
    assert_eq!(s, 200);
    let (s, v) = post_json(&base, "/put", r#"{"key":"c","value":"3"}"#);
    assert_eq!(s, 507, "全碰撞桶满应返回 507 Insufficient Storage");
    assert_eq!(v["kind"], "bucket_capacity_exhausted");
    assert!(
        v["error"].as_str().unwrap().contains("散列"),
        "错误信息应说明全碰撞：{}",
        v["error"]
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn http_bad_json_and_empty_key() {
    let (base, _path) = spawn_server(HashKind::Fx, 4).await;
    let (s, _) = http(
        "POST",
        &format!("{base}/put"),
        "not json",
        "application/json",
    );
    assert_eq!(s, 400);
    let (s, v) = post_json(&base, "/put", r#"{"key":"","value":"x"}"#);
    assert_eq!(s, 400);
    assert_eq!(v["kind"], "bad_request");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn http_raw_put_is_byte_safe() {
    let (base, _path) = spawn_server(HashKind::Fx, 4).await;
    let bin = vec![0u8, 1, 2, 255, 128];
    let mut stream = TcpStream::connect(base.strip_prefix("http://").unwrap()).unwrap();
    let req = format!(
        "POST /raw/put?key=bin HTTP/1.1\r\nHost: x\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        bin.len()
    );
    stream.write_all(req.as_bytes()).unwrap();
    stream.write_all(&bin).unwrap();
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).unwrap();
    let text = String::from_utf8_lossy(&raw);
    assert!(text.starts_with("HTTP/1.1 200"), "{text}");

    // 取回（GET 返回以 lossy utf8 表示，这里只验证计数与存在）。
    let (s, v) = post_json(&base, "/get", r#"{"key":"bin"}"#);
    assert_eq!(s, 200);
    assert_eq!(v["key"], "bin");
}
