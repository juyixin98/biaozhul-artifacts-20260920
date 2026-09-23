//! HTTP 验证入口冒烟测试：构建表 → 启动 `sst serve` → 发真实 HTTP 请求。

use sst::hex::to_hex;
use sst::{FileWriter, TableOptions, TableWriter};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command};
use std::time::{Duration, Instant};

struct ServerGuard(Child);
impl Drop for ServerGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn http_get(port: u16, path: &str) -> (String, String) {
    let mut s = TcpStream::connect(("127.0.0.1", port)).expect("connect");
    s.write_all(format!("GET {path} HTTP/1.1\r\nhost: x\r\nconnection: close\r\n\r\n").as_bytes())
        .unwrap();
    let mut buf = String::new();
    s.read_to_string(&mut buf).unwrap();
    let (head, body) = buf.split_once("\r\n\r\n").unwrap_or((&buf, ""));
    let status = head.lines().next().unwrap_or("").to_string();
    (status, body.to_string())
}

#[test]
fn http_endpoints_smoke() {
    // 1) 构建一个临时表文件
    let mut path = std::env::temp_dir();
    path.push(format!("sst-http-smoke-{}-{}.sst", std::process::id(), 1));
    let entries: Vec<(Vec<u8>, Vec<u8>)> = (0..200)
        .map(|i| {
            (
                format!("user/{:04}", i).into_bytes(),
                format!("payload-{i}").into_bytes(),
            )
        })
        .collect();
    {
        let mut w = TableWriter::new(
            FileWriter::create(&path).unwrap(),
            TableOptions {
                block_size: 128,
                restart_interval: 4,
            },
        );
        for (k, v) in &entries {
            w.add(k, v).unwrap();
        }
        w.finish().unwrap();
    }

    // 2) 选一个空闲端口并启动服务
    let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    let child = Command::new(env!("CARGO_BIN_EXE_sst"))
        .arg("serve")
        .arg("--table")
        .arg(&path)
        .arg("--addr")
        .arg(format!("127.0.0.1:{port}"))
        .spawn()
        .expect("spawn sst serve");
    let _guard = ServerGuard(child);

    // 等服务就绪
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            break;
        }
        assert!(Instant::now() < deadline, "server did not start in time");
        std::thread::sleep(Duration::from_millis(50));
    }

    // 3) /health 与 /stats
    let (status, body) = http_get(port, "/health");
    assert!(status.contains("200"), "{status}");
    assert!(body.contains("\"ok\":true"), "{body}");

    let (status, body) = http_get(port, "/stats");
    assert!(status.contains("200"), "{status}");
    assert!(body.contains("\"entries\":200"), "{body}");

    // 4) /get 命中与未命中
    let key_hex = to_hex(b"user/0042");
    let (status, body) = http_get(port, &format!("/get?key={key_hex}"));
    assert!(status.contains("200"), "{status}");
    assert!(body.contains("\"found\":true"), "{body}");
    assert!(body.contains(&to_hex(b"payload-42")), "{body}");

    let (status, body) = http_get(port, &format!("/get?key={}", to_hex(b"user/9999")));
    assert!(status.contains("200"), "{status}");
    assert!(body.contains("\"found\":false"), "{body}");

    // 5) /scan 跨块范围
    let (status, body) = http_get(
        port,
        &format!(
            "/scan?start={}&end={}",
            to_hex(b"user/0010"),
            to_hex(b"user/0015")
        ),
    );
    assert!(status.contains("200"), "{status}");
    assert!(body.contains("\"count\":5"), "{body}");
    assert!(body.contains(&to_hex(b"user/0010")), "{body}");
    assert!(!body.contains(&to_hex(b"user/0015")), "{body}"); // 区间右开

    // 6) /verify
    let (status, body) = http_get(port, "/verify");
    assert!(status.contains("200"), "{status}");
    assert!(body.contains("\"ok\":true"), "{body}");
    assert!(body.contains("\"entries\":200"), "{body}");

    // 7) 错误处理：未知路径 404、坏 hex 400
    let (status, _) = http_get(port, "/nope");
    assert!(status.contains("404"), "{status}");
    let (status, _) = http_get(port, "/get?key=zz");
    assert!(status.contains("400"), "{status}");

    let _ = std::fs::remove_file(&path);
}
