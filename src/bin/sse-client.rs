//! sse-client：用本库的增量 [`Decoder`] 实现的最小 EventSource 风格客户端。
//!
//! 特性：
//!
//! * 用很小的读取块（默认 64 字节）喂给增量解码器，天然压测跨块边界；
//! * 断线自动重连，重连带最后已知的 `Last-Event-ID`；
//! * 收到服务端 `event: reset` 时明确报告“游标失效、已按重置点对齐”，
//!   并把本地游标切换为 reset 帧携带的 id；
//! * 每个事件以一行 JSON 打到 stdout，日志打到 stderr。
//!
//! 用法：
//!
//! ```text
//! sse-client --url http://127.0.0.1:18080/events [--max-events N] \
//!            [--reconnects N] [--chunk 64]
//! ```
//!
//! `--max-events 0` 表示无限；`--reconnects` 为首次连接之后最多再连次数。

use sse_resume::decode::Decoder;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

struct Args {
    host: String,
    port: u16,
    path: String,
    max_events: usize,
    reconnects: usize,
    chunk: usize,
    initial_last_id: Option<String>,
}

impl Default for Args {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".into(),
            port: 18080,
            path: "/events".into(),
            max_events: 0,
            reconnects: 3,
            chunk: 64,
            initial_last_id: None,
        }
    }
}

fn parse_args() -> Args {
    let mut a = Args::default();
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let val = |name: &str, it: &mut dyn Iterator<Item = String>| {
            it.next().unwrap_or_else(|| panic!("{name} 需要值"))
        };
        match arg.as_str() {
            "--url" => {
                let u = val("--url", &mut it);
                let rest = u.strip_prefix("http://").unwrap_or(&u);
                let (hostport, path) = match rest.split_once('/') {
                    Some((hp, p)) => (hp, format!("/{p}")),
                    None => (rest, "/events".into()),
                };
                let (h, p) = hostport.rsplit_once(':').unwrap_or((hostport, "80"));
                a.host = h.to_string();
                a.port = p.parse().expect("url 端口非法");
                a.path = path;
            }
            "--max-events" => a.max_events = val("--max-events", &mut it).parse().unwrap(),
            "--reconnects" => a.reconnects = val("--reconnects", &mut it).parse().unwrap(),
            "--chunk" => a.chunk = val("--chunk", &mut it).parse().unwrap(),
            "--last-event-id" => a.initial_last_id = Some(val("--last-event-id", &mut it)),
            "--help" | "-h" => {
                println!(
                    "用法: sse-client --url http://HOST:PORT/events\n\
                     \t[--max-events N(0=无限)] [--reconnects N] [--chunk BYTES]\n\
                     \t[--last-event-id ID]"
                );
                std::process::exit(0);
            }
            other => panic!("未知参数: {other}"),
        }
    }
    a
}

fn main() {
    let args = parse_args();
    let mut last_id: Option<String> = args.initial_last_id.clone();
    let mut received = 0usize;

    for attempt in 0..=args.reconnects {
        eprintln!(
            "[client] 第 {} 次连接 {}:{}{} (Last-Event-ID: {:?})",
            attempt + 1,
            args.host,
            args.port,
            args.path,
            last_id
        );
        let remaining = if args.max_events == 0 {
            0 // 0 = 无限
        } else {
            args.max_events.saturating_sub(received)
        };
        let outcome = run_connection(&args, last_id.as_deref(), remaining);
        match outcome {
            Ok(o) => {
                received += o.events;
                last_id = o.last_id;
                if o.reached_target {
                    eprintln!("[client] 已收到 {received} 个事件，退出");
                    return;
                }
                eprintln!(
                    "[client] {}，本次收到 {} 个事件（累计 {}）",
                    if o.eof { "服务端关闭连接(EOF)" } else { "连接中断(读错误)" },
                    o.events,
                    received
                );
            }
            Err(e) => eprintln!("[client] 连接失败: {e}"),
        }

        if args.max_events > 0 && received >= args.max_events {
            eprintln!("[client] 已收到 {received} 个事件，退出");
            return;
        }
        if attempt == args.reconnects {
            eprintln!("[client] 达到最大重连次数 {}，退出", args.reconnects);
            return;
        }
        eprintln!("[client] 1s 后重连…");
        std::thread::sleep(Duration::from_secs(1));
    }
}

struct ConnOutcome {
    events: usize,
    last_id: Option<String>,
    /// true=干净 EOF；false=读取错误（断线模拟）。
    eof: bool,
    /// 本次连接内是否已收满目标数（0 目标时恒 false）。
    reached_target: bool,
}

/// 建立并消费一次连接。任何读错误都被翻译成“中断”结果而非直接退出，
/// 这样调用方可以携带最后 id 重连。`target` 为本次连接最多还要收的事件数，
/// 0 表示不限。
fn run_connection(
    args: &Args,
    last_event_id: Option<&str>,
    target: usize,
) -> std::io::Result<ConnOutcome> {
    let mut stream = TcpStream::connect((args.host.as_str(), args.port))?;
    stream.set_read_timeout(Some(Duration::from_secs(30)))?;
    stream.set_nodelay(true)?;

    // 发最小 GET 请求。
    let mut req = format!(
        "GET {} HTTP/1.1\r\nHost: {}:{}\r\n",
        args.path, args.host, args.port
    );
    if let Some(id) = last_event_id {
        req.push_str(&format!("Last-Event-ID: {id}\r\n"));
    }
    req.push_str("Accept: text/event-stream\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes())?;
    stream.flush()?;

    // 逐字节找到响应头结束位置。逐字节意味着头后粘连的事件字节此刻还没被读走，
    // 它们会在下面的块读取中自然到达，不存在切片/丢字节问题。
    let mut header_buf = Vec::with_capacity(2048);
    let mut byte = [0u8; 1];
    loop {
        match stream.read(&mut byte) {
            Ok(0) => {
                return Ok(ConnOutcome {
                    events: 0,
                    last_id: last_event_id.map(str::to_string),
                    eof: true,
                    reached_target: false,
                })
            }
            Ok(_) => {
                header_buf.push(byte[0]);
                if header_buf.ends_with(b"\r\n\r\n") {
                    break;
                }
                if header_buf.len() > 16 * 1024 {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "响应头过大",
                    ));
                }
            }
            Err(e) => return Err(e),
        }
    }
    if !header_buf.starts_with(b"HTTP/1.1 200") {
        let head = String::from_utf8_lossy(&header_buf);
        return Err(std::io::Error::other(format!(
            "非 200 响应: {}",
            head.lines().next().unwrap_or("")
        )));
    }

    let mut decoder = Decoder::new();
    let mut events = 0usize;
    let mut last_id = last_event_id.map(str::to_string);
    let mut chunk = vec![0u8; args.chunk];

    loop {
        let n = match stream.read(&mut chunk) {
            Ok(0) => {
                // EOF：派发解码器中残留的未终止事件。
                for ev in decoder.finish().map_err(decode_err)? {
                    emit(&ev, &mut last_id, &mut events);
                }
                return Ok(ConnOutcome {
                    events,
                    last_id,
                    eof: true,
                    reached_target: target > 0 && events >= target,
                });
            }
            Ok(n) => n,
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            // 读超时/连接重置等：作为“断线”返回，交由上层用最后 id 重连。
            Err(_e) => {
                return Ok(ConnOutcome {
                    events,
                    last_id,
                    eof: false,
                    reached_target: target > 0 && events >= target,
                });
            }
        };
        for ev in decoder.push(&chunk[..n]).map_err(decode_err)? {
            emit(&ev, &mut last_id, &mut events);
        }
        // 已收满本次连接目标：直接关闭并退出，避免服务端持续推送导致挂住。
        if target > 0 && events >= target {
            return Ok(ConnOutcome {
                events,
                last_id,
                eof: true,
                reached_target: true,
            });
        }
        if let Some(ms) = decoder.take_retry() {
            eprintln!("[client] 服务端提示重连等待 {ms}ms");
        }
    }
}

fn decode_err(e: sse_resume::DecodeError) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, e.to_string())
}

/// 处理一个解码事件：输出 JSON、更新游标、识别 reset。
fn emit(ev: &sse_resume::Event, last_id: &mut Option<String>, events: &mut usize) {
    *events += 1;
    if let Some(id) = &ev.id {
        *last_id = Some(id.clone());
    }
    if ev.event == "reset" {
        eprintln!(
            "[client] ⚠ 收到服务端重置事件（游标已失效）: {}；本地游标对齐为 {:?}",
            ev.data, ev.id
        );
    }
    println!(
        "{{\"event\":\"{}\",\"id\":{},\"data\":\"{}\"}}",
        json_escape(&ev.event),
        match &ev.id {
            Some(v) => format!("\"{}\"", json_escape(v)),
            None => "null".into(),
        },
        json_escape(&ev.data)
    );
    let _ = std::io::stdout().flush();
}

fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}
