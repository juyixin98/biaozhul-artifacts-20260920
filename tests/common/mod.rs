//! 集成测试共享工具：启动服务、发起最小 HTTP GET、从 SSE 流里收集事件。
#![allow(dead_code)]

use sse_resume::decode::{Decoder, Event};
use sse_resume::encode::OutEvent;
use sse_resume::server::Server;
use sse_resume::server::Running;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::{Duration, Instant};

/// 在随机本地端口启动一个指定历史容量的服务。
pub fn start_server(history: usize) -> Running {
    let addr: SocketAddr = ([127, 0, 0, 1], 0).into();
    Server::bind(addr, history).unwrap().spawn().unwrap()
}

/// 发布 n 个数据形如 `msg-<id>` 的 message 事件，返回分配到的 id 列表。
pub fn publish_msgs(server: &Running, n: u64) -> Vec<u64> {
    let mut ids = Vec::new();
    for k in 0..n {
        // id 由服务端分配；这里用 k 仅让数据可读。
        let ev = OutEvent::data_line(format!("msg-{k}"));
        ids.push(server.publish(ev).unwrap());
    }
    ids
}

/// 一次原始 HTTP 连接：返回（状态行，已读完头部的裸 TcpStream）。
///
/// 头部逐字节读取，因此头部之后的事件字节不会被提前吞掉。
pub fn connect_raw(
    addr: SocketAddr,
    path: &str,
    last_event_id: Option<&str>,
) -> (String, TcpStream) {
    let mut stream = TcpStream::connect(addr).unwrap();
    stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut req = format!("GET {path} HTTP/1.1\r\nHost: localhost\r\n");
    if let Some(id) = last_event_id {
        req.push_str(&format!("Last-Event-ID: {id}\r\n"));
    }
    req.push_str("Connection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).unwrap();
    stream.flush().unwrap();

    let mut buf = Vec::new();
    let mut one = [0u8; 1];
    loop {
        let n = stream.read(&mut one).unwrap();
        assert!(n > 0, "等待响应头时遇到 EOF");
        buf.push(one[0]);
        if buf.ends_with(b"\r\n\r\n") {
            break;
        }
        assert!(buf.len() < 16 * 1024, "响应头过大");
    }
    let head = String::from_utf8(buf).unwrap();
    let status = head.lines().next().unwrap_or("").to_string();
    (status, stream)
}

/// 从流中解码事件，直到满足 `want` 谓词或超时。`chunk_size` 控制每次 read
/// 的大小，用于压测跨块切分。
pub fn collect_events_until<F: Fn(&Event) -> bool>(
    stream: &mut TcpStream,
    chunk_size: usize,
    timeout: Duration,
    want: F,
) -> Vec<Event> {
    let mut decoder = Decoder::new();
    let mut events = Vec::new();
    let mut buf = vec![0u8; chunk_size];
    let start = Instant::now();
    loop {
        if events.iter().any(&want) {
            return events;
        }
        assert!(start.elapsed() < timeout, "等待事件超时，已收到 {events:?}");
        let n = match stream.read(&mut buf) {
            Ok(0) => panic!("服务端提前 EOF，已收到 {events:?}"),
            Ok(n) => n,
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(e) => panic!("读错误: {e}"),
        };
        for ev in decoder.push(&buf[..n]).unwrap() {
            events.push(ev);
        }
    }
}

/// 从流中解码事件，直到收到恰好 `n` 个事件（用极小读取块，强制跨块）。
pub fn collect_n(stream: &mut TcpStream, n: usize) -> Vec<Event> {
    let mut decoder = Decoder::new();
    let mut events = Vec::new();
    let mut buf = [0u8; 3];
    let start = Instant::now();
    while events.len() < n {
        assert!(
            start.elapsed() < Duration::from_secs(5),
            "等待 {n} 个事件超时，已收到 {events:?}"
        );
        let got = match stream.read(&mut buf) {
            Ok(0) => panic!("服务端提前 EOF，已收到 {} / {n}: {events:?}", events.len()),
            Ok(g) => g,
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(e) => panic!("读错误: {e}"),
        };
        for ev in decoder.push(&buf[..got]).unwrap() {
            events.push(ev);
        }
    }
    events
}

/// 从 reset 事件 data（我们自定义的 JSON 子集）中抽取字符串字段值。
pub fn json_field<'a>(json: &'a str, key: &str) -> &'a str {
    let pat = format!("\"{key}\":\"");
    let start = json.find(&pat).unwrap_or_else(|| panic!("缺少字段 {key}: {json}"))
        + pat.len();
    let rest = &json[start..];
    let end = rest.find('"').unwrap();
    &rest[..end]
}

/// 抽取数字字段值，返回 u64。
pub fn json_number(json: &str, key: &str) -> Option<u64> {
    let pat = format!("\"{key}\":");
    let start = json.find(&pat)? + pat.len();
    let rest = &json[start..];
    if rest.starts_with("null") {
        return None;
    }
    let end = rest.find([',', '}']).unwrap_or(rest.len());
    rest[..end].parse().ok()
}

/// 简易确定性 LCG，供模糊测试使用（避免引入 rand 依赖）。
pub struct Lcg(pub u64);

impl Lcg {
    pub fn next_u64(&mut self) -> u64 {
        // MMIX 参数
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.0
    }

    /// 返回 [lo, hi] 之间的数。
    pub fn range(&mut self, lo: usize, hi: usize) -> usize {
        lo + (self.next_u64() as usize % (hi - lo + 1))
    }
}
