//! demo-reconnect：一个**可实际运行**的断线续流演示。
//!
//! 它在真实 TCP 回环上：
//!
//! 1. 起一个历史窗口很小的服务；
//! 2. 客户端 A 先连，收前 3 个事件后被强制断开（模拟网络中断）；
//! 3. 断线期间服务端继续发事件；
//! 4. 客户端 A 带 `Last-Event-ID` 重连，验证**无遗漏补回**；
//! 5. 再用「落后 1 个」的游标演示**允许的重复边界**（at-least-once）；
//! 6. 用早已滚出历史窗口的游标演示**过期 reset**。
//!
//! 全程使用本库的 [`sse_resume::decode::Decoder`] 做增量解码。
//! 运行：`cargo run --release --bin demo-reconnect`

use sse_resume::decode::Decoder;
use sse_resume::encode::OutEvent;
use sse_resume::server::Server;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::thread;
use std::time::{Duration, Instant};

fn main() {
    let addr: SocketAddr = ([127, 0, 0, 1], 0).into();
    // 历史窗口足以覆盖断连期间的事件，用于演示“无遗漏补回”；
    // 场景 3 再主动制造过期。
    let server = Server::bind(addr, 20).unwrap().spawn().unwrap();
    let addr = server.addr();
    println!("== 演示服务启动于 {addr}，历史窗口=20 ==\n");

    // ---- 场景 1：断线后无遗漏续传 ----
    println!("\n--- 场景 1：断线重连，无遗漏 ---");
    // 先建立订阅（无游标 = 只收实时事件），再发布事件，确保客户端能收到。
    let (mut c1, mut dec1) = open(addr, None);
    let mut ids = Vec::new();
    for i in 0..3 {
        ids.push(
            server
                .publish(OutEvent::data_line(format!("msg-{i}")).with_event("message"))
                .unwrap(),
        );
    }
    println!("连接后服务端发布 id={:?}", ids);

    let got1 = read_n(&mut c1, &mut dec1, 3);
    println!("客户端第一次连接收到: {}", summarize(&got1));
    let last = got1.last().unwrap().id.clone().unwrap();
    drop(c1);
    println!("!! 模拟断线（丢弃连接），客户端记住 Last-Event-ID={last}");

    // 断线期间又发 4 个。
    let mut gap = Vec::new();
    for i in 3..7 {
        gap.push(
            server
                .publish(OutEvent::data_line(format!("msg-{i}")))
                .unwrap(),
        );
    }
    println!("断线期间服务端又发布 id={:?}", gap);

    let (mut c2, mut dec2) = open(addr, Some(&last));
    let got2 = read_n(&mut c2, &mut dec2, 4);
    let recovered: Vec<u64> = got2
        .iter()
        .map(|e| e.id.as_deref().unwrap().parse().unwrap())
        .collect();
    println!("重连后补收到 id={:?}", recovered);
    assert_eq!(recovered, gap, "续传必须无遗漏、且严格大于旧游标");
    println!("✔ 无遗漏：补回的正是断线期间的全部事件，且无重复");

    // ---- 场景 2：允许的重复边界（游标落后 1）----
    println!("\n--- 场景 2：游标落后 1（at-least-once 重复边界）---");
    // 目前历史窗口 3：最新为 id=7；窗口里是 5,6,7。
    let boundary = server
        .publish(OutEvent::data_line("msg-7"))
        .unwrap();
    let stale = (boundary - 1).to_string();
    println!(
        "客户端实际已收到 id={boundary}，但游标只持久化到 {stale}（崩溃在更新游标前）"
    );
    let (mut c3, mut dec3) = open(addr, Some(&stale));
    let got3 = read_n(&mut c3, &mut dec3, 1);
    let dup: u64 = got3[0].id.as_deref().unwrap().parse().unwrap();
    println!("重连后服务端重投 id={dup}");
    assert_eq!(dup, boundary, "边界事件允许重投一次");
    println!("✔ 允许的重复：边界事件 {boundary} 被再投递一次，消费方需幂等去重");

    // ---- 场景 3：过期游标 -> reset ----
    println!("\n--- 场景 3：游标已过期（滚出有限历史窗口）---");
    // 再发 25 个（窗口=20），把旧 id=1 推出历史窗口。
    for i in 0..25 {
        server
            .publish(OutEvent::data_line(format!("fresh-{i}")))
            .unwrap();
    }
    let ancient = ids[0].to_string();
    let (mut c4, mut dec4) = open(addr, Some(&ancient));
    let got4 = read_until_event(&mut c4, &mut dec4, "reset");
    let reset = got4.iter().find(|e| e.event == "reset").unwrap();
    println!("旧游标 {ancient} 重连，收到: event=reset, id={:?}", reset.id);
    println!("         data={}", reset.data);
    assert!(reset.data.contains("\"reason\":\"cursor-expired\""));
    println!("✔ 明确重置：客户端据此做全量再同步，而不会误以为没有缺口");

    println!("\n== 全部演示断言通过 ==");
}

fn open(addr: SocketAddr, last_event_id: Option<&str>) -> (TcpStream, Decoder) {
    let mut s = TcpStream::connect(addr).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut req = String::from("GET /events HTTP/1.1\r\nHost: demo\r\n");
    if let Some(id) = last_event_id {
        req.push_str(&format!("Last-Event-ID: {id}\r\n"));
    }
    req.push_str("Connection: close\r\n\r\n");
    s.write_all(req.as_bytes()).unwrap();
    s.flush().unwrap();

    // 读掉响应头（容忍偶发 WouldBlock 重试）。
    let mut one = [0u8; 1];
    let mut head = Vec::new();
    loop {
        match s.read(&mut one) {
            Ok(0) => panic!("等待响应头时 EOF"),
            Ok(_) => {
                head.push(one[0]);
                if head.ends_with(b"\r\n\r\n") {
                    break;
                }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(2));
            }
            Err(e) => panic!("读响应头失败: {e}"),
        }
    }
    (s, Decoder::new())
}

fn read_n(s: &mut TcpStream, dec: &mut Decoder, n: usize) -> Vec<sse_resume::Event> {
    let mut out = Vec::new();
    let mut buf = [0u8; 16]; // 故意小，压跨块
    let start = Instant::now();
    while out.len() < n {
        assert!(start.elapsed() < Duration::from_secs(5), "读事件超时: {out:?}");
        let k = match s.read(&mut buf) {
            Ok(0) => panic!("EOF 过早，已收到 {out:?}"),
            Ok(k) => k,
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(2));
                continue;
            }
            Err(e) => panic!("读事件失败: {e}"),
        };
        out.append(&mut dec.push(&buf[..k]).unwrap());
    }
    out
}

fn read_until_event(
    s: &mut TcpStream,
    dec: &mut Decoder,
    event: &str,
) -> Vec<sse_resume::Event> {
    let mut out = Vec::new();
    let mut buf = [0u8; 16];
    let start = Instant::now();
    loop {
        if out.iter().any(|e: &sse_resume::Event| e.event == event) {
            return out;
        }
        assert!(start.elapsed() < Duration::from_secs(5), "等 reset 超时");
        let k = match s.read(&mut buf) {
            Ok(0) => panic!("EOF 等不到 {event}"),
            Ok(k) => k,
            Err(_) => {
                thread::sleep(Duration::from_millis(5));
                continue;
            }
        };
        out.append(&mut dec.push(&buf[..k]).unwrap());
    }
}

fn summarize(evs: &[sse_resume::Event]) -> String {
    evs.iter()
        .map(|e| format!("[id={:?},event={},data={}]", e.id, e.event, e.data))
        .collect::<Vec<_>>()
        .join(" ")
}
