//! 端到端测试：启动 mpstream-server，通过 TCP 发送真实字节流验证。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const BOUNDARY: &str = "srvBoundary-42";

struct ServerGuard {
    child: Child,
    addr: String,
}

impl ServerGuard {
    fn start(extra_args: &[&str]) -> Self {
        // 让服务端绑定空闲端口，从 stdout 第一行读回实际地址，避免端口竞争。
        let mut child = Command::new(env!("CARGO_BIN_EXE_mpstream-server"))
            .args(["--addr", "127.0.0.1:0"])
            .args(extra_args)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("failed to spawn mpstream-server");
        let mut stdout = child.stdout.take().unwrap();
        let mut line = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let mut b = [0u8; 1];
            match stdout.read(&mut b) {
                Ok(1) => {
                    if b[0] == b'\n' {
                        break;
                    }
                    line.push(b[0]);
                }
                _ => panic!("server exited before reporting its address"),
            }
            assert!(Instant::now() < deadline, "server did not start in time");
        }
        let line = String::from_utf8(line).unwrap();
        let addr = line
            .strip_prefix("listening on ")
            .expect("unexpected server greeting")
            .trim()
            .to_string();
        ServerGuard { child, addr }
    }
}

impl Drop for ServerGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// 发送字节流（分片），关闭写方向，读回全部响应。
fn exchange(addr: &str, chunks: &[&[u8]]) -> String {
    let mut s = TcpStream::connect(addr).unwrap();
    for c in chunks {
        s.write_all(c).unwrap();
        s.flush().unwrap();
        std::thread::sleep(Duration::from_millis(5)); // 强制真实分片
    }
    s.shutdown(std::net::Shutdown::Write).unwrap();
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).unwrap();
    String::from_utf8_lossy(&buf).into_owned()
}

fn sample_message() -> Vec<u8> {
    let mut m = format!("BOUNDARY {BOUNDARY}\r\n").into_bytes();
    m.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
    m.extend_from_slice(b"Content-Disposition: form-data; name=\"title\"\r\n\r\n");
    m.extend_from_slice(b"hello world");
    m.extend_from_slice(format!("\r\n--{BOUNDARY}\r\n").as_bytes());
    m.extend_from_slice(
        b"Content-Disposition: form-data; name=\"bin\"; filename=\"x.bin\"\r\n\r\n",
    );
    // 二进制正文，含近似边界。
    m.extend_from_slice(b"\x00\x01\xff\r\n--srvBoundary-4");
    m.extend_from_slice(b"\r\n--srvBoundary-42!");
    m.extend_from_slice(&[0u8; 100]);
    m.extend_from_slice(format!("\r\n--{BOUNDARY}--\r\n").as_bytes());
    m
}

#[test]
fn server_parses_multipart_over_tcp() {
    let srv = ServerGuard::start(&[]);
    let msg = sample_message();
    // 切成 17 字节的小片发送。
    let chunks: Vec<&[u8]> = msg.chunks(17).collect();
    let resp = exchange(&srv.addr, &chunks);
    assert!(resp.starts_with("OK"), "unexpected response: {resp:?}");
    assert!(resp.contains("parts=2"), "missing part count: {resp:?}");
    assert!(
        resp.contains("part 0 name=\"title\" filename=\"\" bytes=11"),
        "part 0 mismatch: {resp:?}"
    );
    let expect_bin_len = b"\x00\x01\xff\r\n--srvBoundary-4".len()
        + b"\r\n--srvBoundary-42!".len()
        + 100;
    assert!(
        resp.contains(&format!("part 1 name=\"bin\" filename=\"x.bin\" bytes={expect_bin_len}")),
        "part 1 mismatch: {resp:?}"
    );
}

#[test]
fn server_reports_missing_final_boundary() {
    let srv = ServerGuard::start(&[]);
    let mut msg = format!("BOUNDARY {BOUNDARY}\r\n").into_bytes();
    msg.extend_from_slice(format!("--{BOUNDARY}\r\n\r\nabc\r\n").as_bytes());
    // 没有结束边界。
    let resp = exchange(&srv.addr, &[&msg]);
    assert!(
        resp.contains("ERROR") && resp.contains("missing final boundary"),
        "unexpected response: {resp:?}"
    );
}

#[test]
fn server_enforces_total_limit() {
    let srv = ServerGuard::start(&["--max-total", "256"]);
    let msg = sample_message();
    let resp = exchange(&srv.addr, &[&msg]);
    assert!(
        resp.contains("ERROR") && resp.contains("total size exceeded"),
        "unexpected response: {resp:?}"
    );
}
