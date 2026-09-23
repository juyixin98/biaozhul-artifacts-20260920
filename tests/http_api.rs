//! HTTP 接口集成测试：真实绑定端口启动 Axum 服务，用原生 TCP 发请求，
//! 不引入额外 HTTP 客户端依赖。
//!
//! 关键点：同步的 std 客户端请求放在 `tokio::task::spawn_blocking` 里执行，
//! 避免在异步工作线程上做阻塞读而与 Tokio 反应器相互干扰。

use bptree_index::bptree::BPTree;
use bptree_index::http;
use bptree_index::pager::MemoryPager;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// 在独立 OS 线程 + 独立 current-thread Tokio 运行时中启动服务，
/// 与测试自身的异步运行时彻底隔离，避免反应器/fd 标志相互干扰。
fn spawn_server() -> String {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    listener.set_nonblocking(true).unwrap();

    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            let tree = Arc::new(Mutex::new(BPTree::new(MemoryPager::new(64))));
            let app = http::router(tree);
            let listener = tokio::net::TcpListener::from_std(listener).unwrap();
            axum::serve(listener, app).await.unwrap();
        });
    });

    // 等服务就绪（简单轮询 accept 端口可连）。
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        if std::net::TcpStream::connect(addr).is_ok() {
            break;
        }
        if std::time::Instant::now() > deadline {
            panic!("测试服务器未能在 5 秒内就绪");
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    format!("http://{addr}")
}

/// 在独立阻塞线程里发送一个最朴素的 HTTP/1.0 请求（响应后服务端关闭连接，
/// 可干净读到 EOF），返回 (状态码, 响应体字符串)。
///
/// 读取循环同时兼容阻塞与非阻塞 fd：收到 WouldBlock 就短暂退避重试，
/// 以 Content-Length / EOF 判定完整响应，并以“距上一字节 5 秒”为空闲超时。
fn raw_request(base: &str, method: &str, path: &str, body: Option<&str>) -> (u16, String) {
    let addr = base.trim_start_matches("http://");
    let mut stream = TcpStream::connect(addr).unwrap();
    let _ = stream.set_read_timeout(Some(Duration::from_millis(200)));
    let body = body.unwrap_or("");
    let req = format!(
        "{method} {path} HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes()).unwrap();

    let mut raw = Vec::new();
    let mut buf = [0u8; 4096];
    let mut last_data = Instant::now();
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break, // EOF：服务端关闭
            Ok(n) => {
                raw.extend_from_slice(&buf[..n]);
                last_data = Instant::now();
                if response_complete(&raw) {
                    break;
                }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                if response_complete(&raw) {
                    break;
                }
                if last_data.elapsed() > Duration::from_secs(5) {
                    panic!("读取响应空闲超时（已读 {} 字节）", raw.len());
                }
                std::thread::sleep(Duration::from_millis(2));
            }
            Err(e) => panic!("read error: {e}"),
        }
    }
    let raw = String::from_utf8(raw).unwrap();
    let status: u16 = raw
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let body = raw
        .split_once("\r\n\r\n")
        .map(|(_, b)| b.to_string())
        .unwrap_or_default();
    (status, body)
}

/// 根据响应头 Content-Length（或已到 EOF 的调用方）判断响应体是否收全。
fn response_complete(raw: &[u8]) -> bool {
    let sep = b"\r\n\r\n";
    let Some(idx) = raw.windows(4).position(|w| w == sep) else {
        return false;
    };
    let header = &raw[..idx];
    let body_start = idx + 4;
    let mut len: Option<usize> = None;
    for line in String::from_utf8_lossy(header).split("\r\n") {
        if let Some(rest) = line.to_ascii_lowercase().strip_prefix("content-length:") {
            len = rest.trim().parse().ok();
        }
    }
    match len {
        Some(n) => raw.len() - body_start >= n,
        None => false, // 无 Content-Length（一般不会），交给 EOF
    }
}

/// 把同步客户端调用挪到专用阻塞线程。
async fn request(base: &str, method: &str, path: &str, body: Option<String>) -> (u16, String) {
    let base = base.to_string();
    let method = method.to_string();
    let path = path.to_string();
    tokio::task::spawn_blocking(move || raw_request(&base, &method, &path, body.as_deref()))
        .await
        .unwrap()
}

#[tokio::test]
async fn full_http_workflow() {
    let base = spawn_server();

    let (s, b) = request(&base, "GET", "/healthz", None).await;
    assert_eq!(s, 200);
    assert!(b.contains("ok"));

    // 初始为空
    let (s, b) = request(&base, "GET", "/get?key=10", None).await;
    assert_eq!(s, 200, "{b}");
    assert!(b.contains("\"found\":false"), "{b}");

    // 插入（64B 页，叶容量 3，插 40 个必然多次根分裂）
    for k in 1..=40i64 {
        let payload = format!("{{\"key\":{k},\"value\":{}}}", k * 100);
        let (s, b) = request(&base, "POST", "/insert", Some(payload)).await;
        assert_eq!(s, 200, "insert {k}: {b}");
        assert!(b.contains("\"inserted\":true"));
    }
    // 重复键 -> inserted=false（覆盖）
    let (s, b) = request(
        &base,
        "POST",
        "/insert",
        Some("{\"key\":10,\"value\":1}".into()),
    )
    .await;
    assert_eq!(s, 200, "{b}");
    assert!(b.contains("\"inserted\":false"), "{b}");

    let (s, b) = request(&base, "GET", "/get?key=10", None).await;
    assert_eq!(s, 200);
    assert!(b.contains("\"value\":1"), "覆盖后值应为 1：{b}");

    let (s, b) = request(&base, "GET", "/range?lo=15&hi=18", None).await;
    assert_eq!(s, 200, "{b}");
    assert!(b.contains("\"count\":4"), "{b}");
    for k in [15, 16, 17, 18] {
        assert!(b.contains(&format!("\"key\":{k}")), "范围结果缺少 {k}: {b}");
    }

    let (s, b) = request(&base, "GET", "/stats", None).await;
    assert_eq!(s, 200, "{b}");
    let stats: serde_json::Value = serde_json::from_str(&b).unwrap();
    assert_eq!(stats["item_count"], 40);
    assert!(
        stats["height"].as_u64().unwrap() >= 2,
        "40 键在 64B 页下应已长高：{b}"
    );
    assert_eq!(stats["page_size"], 64);

    // lo > hi -> 400
    let (s, _) = request(&base, "GET", "/range?lo=100&hi=1", None).await;
    assert_eq!(s, 400);

    // 非法 JSON -> 4xx
    let (s, _) = request(&base, "POST", "/insert", Some("{not json".into())).await;
    assert!(s == 400 || s == 422, "非法 JSON 应被拒绝，实际 {s}");

    // 删光，树高应降回 1
    for k in 1..=40i64 {
        let payload = format!("{{\"key\":{k}}}");
        let (s, b) = request(&base, "POST", "/delete", Some(payload)).await;
        assert_eq!(s, 200, "delete {k}: {b}");
    }
    let (s, b) = request(&base, "GET", "/stats", None).await;
    assert_eq!(s, 200, "{b}");
    let stats: serde_json::Value = serde_json::from_str(&b).unwrap();
    assert_eq!(stats["item_count"], 0);
    assert_eq!(stats["height"], 1, "清空后树高应降回 1：{b}");
    assert_eq!(stats["internal_pages"], 0);

    // 删除不存在的键 -> found=false
    let (s, b) = request(&base, "POST", "/delete", Some("{\"key\":12345}".into())).await;
    assert_eq!(s, 200);
    assert!(b.contains("\"found\":false"));
}
