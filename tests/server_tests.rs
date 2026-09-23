//! TCP 测试服务端到端：真实监听 loopback，真实 TCP 连接。

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use dns_compress::frame::{recv_frame, send_frame};
use dns_compress::message::{Message, Rdata, CLASS_IN, TYPE_A};
use dns_compress::parser::Limits;
use dns_compress::server::respond;

/// 测试用 TCP 服务：非阻塞轮询 accept，`stop` 置位后线程退出（不泄漏线程，
/// 否则无限 accept 循环会让测试进程无法结束）。
fn spawn_test_server(max_msg: usize) -> (u16, Arc<AtomicBool>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    listener.set_nonblocking(true).unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let stop2 = Arc::clone(&stop);
    thread::spawn(move || {
        while !stop2.load(Ordering::Relaxed) {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    stream.set_nonblocking(false).ok();
                    stream.set_read_timeout(Some(Duration::from_secs(1))).ok();
                    dns_compress::server::serve_connection(
                        &mut stream,
                        &Limits::default(),
                        max_msg,
                    );
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(5));
                }
                Err(_) => break,
            }
        }
    });
    (port, stop)
}

fn hex(h: &str) -> Vec<u8> {
    let s: String = h.chars().filter(|c| !c.is_whitespace()).collect();
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn query_a() -> Vec<u8> {
    let mut b = Vec::new();
    b.extend_from_slice(&0x1234u16.to_be_bytes());
    b.extend_from_slice(&0x0100u16.to_be_bytes());
    b.extend_from_slice(&1u16.to_be_bytes());
    b.extend_from_slice(&0u16.to_be_bytes());
    b.extend_from_slice(&0u16.to_be_bytes());
    b.extend_from_slice(&0u16.to_be_bytes());
    b.extend_from_slice(&hex("03 77 77 77 07 65 78 61 6d 70 6c 65 03 63 6f 6d 00"));
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b
}

fn compressed_response() -> Vec<u8> {
    let mut b = Vec::new();
    for x in [0x9abcu16, 0x8180, 1, 1, 0, 0] {
        b.extend_from_slice(&x.to_be_bytes());
    }
    b.extend_from_slice(&hex("03 77 77 77 07 65 78 61 6d 70 6c 65 03 63 6f 6d 00"));
    b.extend_from_slice(&5u16.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&hex(
        "c0 0c 00 05 00 01 00 00 01 2c 00 08 05 61 6c 69 61 73 c0 10",
    ));
    b
}

#[test]
fn respond_function_level_rules() {
    let limits = Limits::default();

    // 合法 A 查询 → NOERROR + A 记录
    let resp = respond(&query_a(), &limits);
    let msg = Message::parse(&resp, &limits).unwrap();
    assert_eq!(msg.id, 0x1234);
    assert!(msg.flags.qr);
    assert_eq!(msg.flags.rcode, 0);
    assert_eq!(msg.questions.len(), 1);
    assert!(msg.flags.ra);
    assert!(msg.flags.rd, "RD 必须回显");
    assert_eq!(msg.answers[0].ttl, 60);
    assert!(matches!(msg.answers[0].rdata, Rdata::A(_)));

    // 非法报文 → FORMERR，ID 回显
    let bad = vec![0x12, 0x34, 0x00, 0xff]; // 太短
    let resp = respond(&bad, &limits);
    assert_eq!(resp[0..2], [0x12, 0x34]);
    assert_eq!(resp[3] & 0xF, 1, "RCODE 必须是 FORMERR=1");
    assert_eq!(resp[2] >> 7, 1, "QR 必须置位");

    // 无法取 ID 的空字节
    let resp = respond(&[], &limits);
    assert_eq!(resp[3] & 0xF, 1);

    // 未知类型 → NOTIMP
    let mut q = query_a();
    q[0..2].copy_from_slice(&0x1234u16.to_be_bytes());
    // 把问题类型改成 99（位置：12+17=29）
    q[29..31].copy_from_slice(&99u16.to_be_bytes());
    let resp = respond(&q, &limits);
    let msg = Message::parse(&resp, &limits).unwrap();
    assert_eq!(msg.flags.rcode, 4, "未知类型必须回 NOTIMP");
    assert!(msg.answers.is_empty());

    // QR=1 压缩响应 → 回显，语义等价
    let raw = compressed_response();
    let resp = respond(&raw, &limits);
    let echo = Message::parse(&resp, &limits).unwrap();
    let orig = Message::parse(&raw, &limits).unwrap();
    assert_eq!(echo, orig, "回显必须保持语义（压缩→非压缩重编码）");
    assert!(echo.flags.qr);
}

/// 起一个真实的 TCP 服务线程，走完整的连接/成帧路径。
#[test]
fn end_to_end_tcp_server() {
    let (port, stop) = spawn_test_server(4096);

    // 等待监听就绪（绑定已完成，循环重试即可）。
    let mut conn = None;
    for _ in 0..50 {
        if let Ok(s) = TcpStream::connect(("127.0.0.1", port)) {
            conn = Some(s);
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    let mut conn = conn.expect("无法连接测试服务");
    conn.set_nodelay(true).unwrap();

    // 同一连接上连续发两帧（请求复用 + 流水化）。
    send_frame(&mut conn, &query_a()).unwrap();
    let resp1 = recv_frame(&mut conn, 65535).unwrap();
    let m1 = Message::parse(&resp1, &Limits::default()).unwrap();
    assert_eq!(m1.id, 0x1234);
    assert!(matches!(m1.answers[0].rdata, Rdata::A(_)));

    // 第二帧：QR=1 压缩报文，验证服务侧语义往返
    send_frame(&mut conn, &compressed_response()).unwrap();
    let resp2 = recv_frame(&mut conn, 65535).unwrap();
    let m2 = Message::parse(&resp2, &Limits::default()).unwrap();
    match &m2.answers[0].rdata {
        Rdata::Cname(n) => assert_eq!(n.to_string(), "alias.example.com."),
        other => panic!("{other:?}"),
    }

    // 第三帧：畸形报文 → FORMERR
    send_frame(&mut conn, &[0, 0, 0]).unwrap();
    let resp3 = recv_frame(&mut conn, 65535).unwrap();
    assert_eq!(resp3[3] & 0xF, 1);

    // 干净关闭：服务端不报错即通过（连接被服务端关闭）。
    drop(conn);
    stop.store(true, Ordering::Relaxed);
}

/// 声明的帧长超过服务端上限 4096：服务端应直接关闭连接。
#[test]
fn oversized_frame_closes_connection() {
    let (port, stop) = spawn_test_server(4096);

    let mut conn = TcpStream::connect(("127.0.0.1", port)).unwrap();
    conn.write_all(&[0x13, 0x88]).unwrap(); // 声明 5000 字节 > 4096
    let mut buf = [0u8; 4];
    let n = conn.read(&mut buf).unwrap();
    assert_eq!(n, 0, "超长帧应导致服务端关闭连接");
    stop.store(true, Ordering::Relaxed);
}

/// 声明的帧长合法但报文体缺失：服务端应在读超时后关闭而不是无限挂起。
#[test]
fn truncated_frame_closes_connection() {
    let (port, stop) = spawn_test_server(4096);

    let mut conn = TcpStream::connect(("127.0.0.1", port)).unwrap();
    conn.write_all(&[0x00, 0x10, 1, 2]).unwrap(); // 声明 16 字节，只给 2 字节
    let mut buf = [0u8; 4];
    let n = conn
        .set_read_timeout(Some(Duration::from_secs(8)))
        .and_then(|_| conn.read(&mut buf))
        .unwrap();
    assert_eq!(n, 0, "截断帧应导致服务端关闭连接");
    stop.store(true, Ordering::Relaxed);
}
