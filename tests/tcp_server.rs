//! 真实 TCP 端到端测试：启动 `ipfrag-server` 二进制（127.0.0.1，系统分配端口），
//! 通过 TCP 连接收发 JSONL，验证网络路径与命令行客户端链路。
//!
//! 端口发现：服务器 stderr 打印 `listening on 127.0.0.1:PORT`。
//! TTL：启动时给一个很短的 `--ttl-ms`，用 `--purge-interval-ms` 关闭
//! 插入期机会式清理（设得比 TTL 大），再用 `purge` 请求精确控制时间。

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

struct ServerHandle {
    child: Child,
    port: u16,
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn start_server(extra: &[&str]) -> ServerHandle {
    let bin = env!("CARGO_BIN_EXE_ipfrag-server");
    let mut child = match Command::new(bin)
        .args(["--port", "0", "--ttl-ms", "300"])
        .args(extra)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(e) => panic!("spawn ipfrag-server: {e}"),
    };

    // 从 stderr 读取监听端口；任一步失败都先回收子进程再 panic。
    let announce = (|| -> std::io::Result<u16> {
        let stderr = child.stderr.take().expect("stderr piped");
        let mut reader = BufReader::new(stderr);
        let mut line = String::new();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            line.clear();
            let n = reader.read_line(&mut line)?;
            if n == 0 {
                if std::time::Instant::now() >= deadline {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        "server did not announce port",
                    ));
                }
                std::thread::sleep(Duration::from_millis(10));
                continue;
            }
            if let Some(idx) = line.find("listening on 127.0.0.1:") {
                let rest = &line[idx + "listening on 127.0.0.1:".len()..];
                let port: u16 = rest.trim().parse().expect("parse port");
                // 让后台线程继续排空 stderr，避免管道写满
                std::thread::spawn(move || {
                    let mut sink = Vec::new();
                    let _ = reader.read_to_end(&mut sink);
                });
                return Ok(port);
            }
        }
    })();

    match announce {
        Ok(port) => ServerHandle { child, port },
        Err(e) => {
            let _ = child.kill();
            let _ = child.wait();
            panic!("failed to discover server port: {e}");
        }
    }
}

fn connect(port: u16) -> TcpStream {
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        if let Ok(s) = TcpStream::connect(("127.0.0.1", port)) {
            s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            return s;
        }
        assert!(std::time::Instant::now() < deadline, "could not connect");
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn send_recv(port: u16, requests: &[&str]) -> Vec<String> {
    let mut stream = connect(port);
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut out = Vec::new();
    for req in requests {
        writeln!(stream, "{req}").expect("send");
        stream.flush().unwrap();
        let mut line = String::new();
        let n = reader.read_line(&mut line).expect("read response");
        assert!(n > 0, "server closed connection");
        out.push(line.trim().to_string());
    }
    out
}

#[test]
fn tcp_ping_config_stats_roundtrip() {
    let h = start_server(&[]);
    let responses = send_recv(
        h.port,
        &[
            r#"{"op":"ping"}"#,
            r#"{"op":"config"}"#,
            r#"{"op":"stats"}"#,
        ],
    );
    assert!(
        responses[0].contains(r#""pong":true"#),
        "got: {}",
        responses[0]
    );
    assert!(
        responses[1].contains("reject_new_fragment"),
        "got: {}",
        responses[1]
    );
    assert!(
        responses[2].contains("fragments_received"),
        "got: {}",
        responses[2]
    );
}

#[test]
fn tcp_full_reassembly_and_duplicate_over_real_socket() {
    let h = start_server(&[]);

    // 原始载荷（确定性）
    let payload: Vec<u8> = (0..72u32).map(|i| (i * 13) as u8).collect();
    let p0: Vec<u8> = payload[0..24].to_vec();
    let p1: Vec<u8> = payload[24..48].to_vec();
    let p2: Vec<u8> = payload[48..72].to_vec();
    let h0 = hex(&p0);
    let h1 = hex(&p1);
    let h2 = hex(&p2);

    let base = r#""src":"10.2.0.1","dst":"10.2.0.2","protocol":17,"id":42"#;
    let requests = [
        format!(
            r#"{{"op":"frag",{base},"offset":48,"mf":false,"payload_hex":"{h2}","include_payload":false}}"#
        ),
        format!(r#"{{"op":"frag",{base},"offset":24,"mf":true,"payload_hex":"{h1}"}}"#),
        format!(r#"{{"op":"frag",{base},"offset":24,"mf":true,"payload_hex":"{h1}"}}"#), // 重复
        format!(r#"{{"op":"status",{base}}}"#),
        format!(
            r#"{{"op":"frag",{base},"offset":0,"mf":true,"payload_hex":"{h0}","include_payload":true}}"#
        ),
        r#"{"op":"stats"}"#.to_string(),
    ];
    let refs: Vec<&str> = requests.iter().map(|s| s.as_str()).collect();
    let responses = send_recv(h.port, &refs);

    // 末片先到：accepted，has_last=true，has_first=false
    assert!(
        responses[0].contains(r#""event":"accepted""#),
        "got: {}",
        responses[0]
    );
    assert!(
        responses[0].contains(r#""has_last_fragment":true"#),
        "got: {}",
        responses[0]
    );
    assert!(
        responses[0].contains(r#""has_first_fragment":false"#),
        "got: {}",
        responses[0]
    );

    // 重复片
    assert!(
        responses[2].contains(r#""event":"duplicate""#),
        "got: {}",
        responses[2]
    );

    // 未完成状态
    assert!(
        responses[3].contains(r#""has_first_fragment":false"#),
        "got: {}",
        responses[3]
    );

    // 首片到达 -> 完成
    let expected_digest = ipfrag::sha256_hex(&payload);
    assert!(
        !responses[4].contains(r#""event":""#) || responses[4].contains("completed"),
        "got: {}",
        responses[4]
    );
    assert!(
        responses[4].contains(&format!(r#""sha256":"{expected_digest}""#)),
        "got: {}",
        responses[4]
    );
    assert!(
        responses[4].contains(r#""total_bytes":72"#),
        "got: {}",
        responses[4]
    );
    // include_payload 回显
    assert!(
        responses[4].contains(&format!(r#""payload_hex":"{}""#, hex(&payload))),
        "payload mismatch: {}",
        responses[4]
    );

    assert!(
        responses[5].contains(r#""datagrams_completed":1"#),
        "got: {}",
        responses[5]
    );
    assert!(
        responses[5].contains(r#""duplicates":1"#),
        "got: {}",
        responses[5]
    );
}

#[test]
fn tcp_overlap_is_rejected_and_bad_json_handled() {
    // 切到 drop_assembly 策略验证另一种策略也可配置
    let h = start_server(&["--overlap", "drop_assembly"]);
    let p0 = hex(&[1u8; 24]);
    let poverlap = hex(&[2u8; 24]);
    let base = r#""src":"10.3.0.1","dst":"10.3.0.2","protocol":6,"id":7"#;
    let responses = send_recv(
        h.port,
        &[
            &format!(r#"{{"op":"frag",{base},"offset":0,"mf":true,"payload_hex":"{p0}"}}"#),
            &format!(r#"{{"op":"frag",{base},"offset":8,"mf":true,"payload_hex":"{poverlap}"}}"#),
            r#"{"op":"status"}"#, // drop_assembly 后应没有残留
        ],
    );
    assert!(responses[0].contains("accepted"), "got: {}", responses[0]);
    assert!(
        responses[1].contains(r#""error":"overlap""#),
        "got: {}",
        responses[1]
    );
    assert!(
        responses[2].contains(r#""count":0"#),
        "got: {}",
        responses[2]
    );

    // 非法 JSON 不杀连接，后续请求仍正常
    let responses = send_recv(h.port, &[r#"{not valid json"#, r#"{"op":"ping"}"#]);
    assert!(
        responses[0].contains(r#""error":"bad_json""#),
        "got: {}",
        responses[0]
    );
    assert!(responses[1].contains("pong"), "got: {}", responses[1]);
}

fn hex(data: &[u8]) -> String {
    data.iter().map(|b| format!("{b:02x}")).collect()
}
