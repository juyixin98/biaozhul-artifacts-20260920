//! HTTP 接口端到端测试: 启动真实服务二进制, 用原生 TCP 发 HTTP/1.1 请求。
//! 覆盖: 追加/读取/状态、JSON 与 raw 两种载荷、重启持久化、序号不复用、
//! 以及中段损坏时接口返回明确错误 (不静默跳过)。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use base64::Engine;

fn bin() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_seglog"))
}

fn fresh_dir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "seglog-http-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

struct Server {
    child: Child,
    port: u16,
    _stderr: std::fs::File,
    stderr_path: PathBuf,
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_file(&self.stderr_path);
    }
}

fn spawn_server(dir: &PathBuf, seg_bytes: u64) -> Server {
    let stderr_path = std::env::temp_dir().join(format!(
        "seglog-http-stderr-{}-{}.log",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let stderr = std::fs::File::create(&stderr_path).unwrap();
    let child = Command::new(bin())
        .env("SEGLOG_DATA_DIR", dir)
        .env("SEGLOG_SEGMENT_BYTES", seg_bytes.to_string())
        .env("SEGLOG_BIND", "127.0.0.1:0")
        .stdout(Stdio::null())
        .stderr(stderr.try_clone().unwrap())
        .spawn()
        .unwrap();

    // 从 stderr 日志里解析内核分配的实际端口。
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut port = None;
    while Instant::now() < deadline {
        if let Ok(text) = std::fs::read_to_string(&stderr_path) {
            if let Some(idx) = text.find("listening on http://") {
                let rest = &text[idx + "listening on http://".len()..];
                let addr = rest.split_whitespace().next().unwrap();
                port = Some(addr.rsplit(':').next().unwrap().parse().unwrap());
                break;
            }
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let port = port.expect("server did not print listening address");
    wait_healthy(port);
    Server {
        child,
        port,
        _stderr: stderr,
        stderr_path,
    }
}

fn wait_healthy(port: u16) {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Ok((status, _)) = try_http(port) {
            if status == 200 {
                return;
            }
        }
        assert!(Instant::now() < deadline, "server never became healthy");
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn try_http(port: u16) -> std::io::Result<(u16, String)> {
    let mut stream = TcpStream::connect(("127.0.0.1", port))?;
    use std::io::Write;
    let req = "GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
    stream.write_all(req.as_bytes())?;
    let mut raw = Vec::new();
    use std::io::Read;
    stream.read_to_end(&mut raw)?;
    let text = String::from_utf8_lossy(&raw);
    let status: u16 = text
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let body = text.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
    Ok((status, body))
}

fn http(
    port: u16,
    method: &str,
    path: &str,
    content_type: Option<&str>,
    body: &[u8],
) -> (u16, String) {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
    let mut req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Length: {}\r\n",
        body.len()
    );
    if let Some(ct) = content_type {
        req.push_str(&format!("Content-Type: {ct}\r\n"));
    }
    req.push_str("\r\n");
    stream.write_all(req.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    stream.flush().unwrap();

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).unwrap();
    let text = String::from_utf8_lossy(&raw);
    let status: u16 = text
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let body = text.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
    (status, body)
}

#[test]
fn healthz_ok() {
    let dir = fresh_dir("health");
    let srv = spawn_server(&dir, 4096);
    let (status, body) = http(srv.port, "GET", "/healthz", None, b"");
    assert_eq!(status, 200);
    assert!(body.contains("\"ok\""));
}

#[test]
fn append_raw_and_read_back() {
    let dir = fresh_dir("raw");
    let srv = spawn_server(&dir, 4096);

    for (i, msg) in ["alpha", "beta", "gamma"].iter().enumerate() {
        let (status, body) = http(
            srv.port,
            "POST",
            "/logs/demo/records",
            Some("text/plain; charset=utf-8"),
            msg.as_bytes(),
        );
        assert_eq!(status, 200, "body={body}");
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["seq"], (i + 1) as u64);
        assert_eq!(v["bytes"], msg.len() as u64);
    }

    let (status, body) = http(srv.port, "GET", "/logs/demo/records", None, b"");
    assert_eq!(status, 200);
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["count"], 3);
    assert_eq!(v["next_seq"], 4);
    let texts: Vec<String> = v["records"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["text"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(texts, vec!["alpha", "beta", "gamma"]);

    let (status, body) = http(srv.port, "GET", "/logs/demo", None, b"");
    assert_eq!(status, 200);
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["record_count"], 3);
    assert_eq!(v["persisted_next_seq"], 4);
}

#[test]
fn append_json_text_and_base64() {
    let dir = fresh_dir("json");
    let srv = spawn_server(&dir, 4096);

    let (status, _) = http(
        srv.port,
        "POST",
        "/logs/bin/records",
        Some("application/json"),
        br#"{"data":"hello json"}"#,
    );
    assert_eq!(status, 200);

    // 0x00 0x01 0xFE 非 UTF-8, 用 base64。
    let raw = [0u8, 1, 254];
    let b64 = base64::engine::general_purpose::STANDARD.encode(raw);
    let req = format!(r#"{{"payload_base64":"{b64}"}}"#);
    let (status, body) = http(
        srv.port,
        "POST",
        "/logs/bin/records",
        Some("application/json"),
        req.as_bytes(),
    );
    assert_eq!(status, 200, "body={body}");

    let (_, body) = http(srv.port, "GET", "/logs/bin/records", None, b"");
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["count"], 2);
    assert_eq!(v["records"][1]["payload_base64"], b64);
    assert!(v["records"][1]["text"].is_null());
}

#[test]
fn bad_requests_are_rejected() {
    let dir = fresh_dir("bad");
    let srv = spawn_server(&dir, 4096);

    let (status, _) = http(
        srv.port,
        "POST",
        "/logs/../etc/records",
        Some("text/plain"),
        b"x",
    );
    // 路径穿越串会被 axum 路由/名字校验拦下 (400 或 404), 绝不能写成任意路径。
    assert!(status == 400 || status == 404, "got {status}");

    let (status, body) = http(
        srv.port,
        "POST",
        "/logs/ok/records",
        Some("application/json"),
        br#"{"nope":1}"#,
    );
    assert_eq!(status, 400, "body={body}");
}

#[test]
fn restart_persists_and_continues_sequence() {
    let dir = fresh_dir("restart");

    let srv = spawn_server(&dir, 4096);
    for i in 1..=4u64 {
        let (status, body) = http(
            srv.port,
            "POST",
            "/logs/L/records",
            Some("text/plain"),
            format!("msg-{i}").as_bytes(),
        );
        assert_eq!(status, 200, "body={body}");
    }
    // SIGKILL 掉服务 (记录此前都已 fsync 确认)。
    drop(srv);

    let srv = spawn_server(&dir, 4096);
    let (_, body) = http(srv.port, "GET", "/logs/L/records", None, b"");
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["count"], 4);
    assert_eq!(v["next_seq"], 5);

    let (status, body) = http(srv.port, "POST", "/logs/L/records", Some("text/plain"), b"msg-5");
    assert_eq!(status, 200, "body={body}");
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["seq"], 5, "sequence must continue, not restart at 1");
}

#[test]
fn middle_segment_corruption_is_rejected_not_skipped() {
    let dir = fresh_dir("corrupt");
    let seg_bytes = 40u64;

    let srv = spawn_server(&dir, seg_bytes);
    for i in 1..=9u64 {
        let (status, _) = http(
            srv.port,
            "POST",
            "/logs/C/records",
            Some("text/plain"),
            format!("ord-{i:03}").as_bytes(), // 7B 载荷, 帧 23B
        );
        assert_eq!(status, 200);
    }
    drop(srv);

    // 翻转第一个(封口)段中的载荷字节, 造成中段 CRC 损坏。
    // 服务的具名日志存放在 <data_dir>/<name>/ 下。
    let log_dir = dir.join("C");
    let mut segs: Vec<PathBuf> = std::fs::read_dir(&log_dir)
        .unwrap()
        .filter_map(|e| {
            let p = e.unwrap().path();
            (p.extension().is_some_and(|x| x == "seg")).then_some(p)
        })
        .collect();
    segs.sort();
    assert!(segs.len() >= 2, "precondition: multiple segments, got {}", segs.len());
    let target = &segs[0];
    let mut bytes = std::fs::read(target).unwrap();
    bytes[16 + 2] ^= 0xFF;
    std::fs::write(target, bytes).unwrap();

    let srv = spawn_server(&dir, seg_bytes);
    // 打开该日志必须返回 500 log_corrupt, 而不是静默跳过损坏记录。
    let (status, body) = http(srv.port, "GET", "/logs/C/records", None, b"");
    assert_eq!(status, 500);
    assert!(body.contains("log_corrupt"), "body={body}");
    assert!(body.contains("crc mismatch"), "body={body}");

    // 对损坏日志的追加同样被拒。
    let (status, _) = http(
        srv.port,
        "POST",
        "/logs/C/records",
        Some("text/plain"),
        b"more",
    );
    assert_eq!(status, 500);
}
