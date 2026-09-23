//! mpstream-server — 本地 TCP 测试服务，用增量解析器流式接收 multipart 数据。
//!
//! 线协议（自定义，仅用于测试）：
//!   1. 客户端先发送一行 `BOUNDARY <boundary>\r\n`；
//!   2. 随后原样发送 multipart 消息体（可任意分片、可含二进制）；
//!   3. 客户端关闭写方向（shutdown write）表示输入结束；
//!   4. 服务端回送文本报告后关闭连接。
//!
//! 服务端按 4 KiB 块读取 socket 并喂给解析器，不缓存整个请求体。
//!
//! 用法: mpstream-server [--addr 127.0.0.1:18080] [--max-total N] [--max-part N]
//!                       [--max-parts N] [--max-header N]

use mpstream::{Event, Limits, MultipartParser};
use std::env;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::exit;

const READ_CHUNK: usize = 4096;

fn main() {
    let mut addr = "127.0.0.1:18080".to_string();
    let mut limits = Limits::default();

    let mut args = env::args().skip(1);
    while let Some(a) = args.next() {
        let mut take = |name: &str| -> String {
            args.next().unwrap_or_else(|| {
                eprintln!("missing value for {name}");
                exit(2);
            })
        };
        match a.as_str() {
            "--addr" => addr = take("--addr"),
            "--max-total" => limits.max_total_bytes = parse_usize(&take("--max-total")),
            "--max-part" => limits.max_part_bytes = parse_usize(&take("--max-part")),
            "--max-parts" => limits.max_parts = parse_usize(&take("--max-parts")),
            "--max-header" => limits.max_header_bytes = parse_usize(&take("--max-header")),
            "-h" | "--help" => {
                println!("{}", env!("CARGO_PKG_DESCRIPTION"));
                exit(0);
            }
            other => {
                eprintln!("unknown argument: {other}");
                exit(2);
            }
        }
    }

    let listener = TcpListener::bind(&addr).unwrap_or_else(|e| {
        eprintln!("bind {addr} failed: {e}");
        exit(1);
    });
    println!("listening on {}", listener.local_addr().unwrap());

    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let peer = s.peer_addr().map(|a| a.to_string()).unwrap_or_default();
                let report = handle_conn(s, &limits);
                println!("[{peer}] {report}");
            }
            Err(e) => eprintln!("accept failed: {e}"),
        }
    }
}

fn parse_usize(s: &str) -> usize {
    s.parse().unwrap_or_else(|_| {
        eprintln!("not a number: {s}");
        exit(2);
    })
}

/// 处理一条连接，返回一行日志摘要。
fn handle_conn(mut stream: TcpStream, limits: &Limits) -> String {
    let reply = match run_session(&mut stream, limits) {
        Ok(report) => format!("OK\n{report}"),
        Err(e) => format!("ERROR {e}\n"),
    };
    let _ = stream.write_all(reply.as_bytes());
    let _ = stream.shutdown(std::net::Shutdown::Both);
    reply.lines().next().unwrap_or("").to_string()
}

fn run_session(stream: &mut TcpStream, limits: &Limits) -> Result<String, String> {
    // 第一行：BOUNDARY <boundary>
    let boundary = read_boundary_line(stream)?;
    let mut parser =
        MultipartParser::new(&boundary, limits.clone()).map_err(|e| e.to_string())?;

    let mut report = String::new();
    let mut chunk = [0u8; READ_CHUNK];
    let mut open_part: Option<usize> = None;
    let mut part_len = 0usize;
    let mut part_name = String::new();
    let mut part_filename = String::new();

    let handle_events = |events: Vec<Event>,
                             report: &mut String,
                             open_part: &mut Option<usize>,
                             part_len: &mut usize,
                             part_name: &mut String,
                             part_filename: &mut String| {
        for ev in events {
            match ev {
                Event::PartStart { index, headers } => {
                    *open_part = Some(index);
                    *part_len = 0;
                    *part_name = find_header(&headers, "content-disposition")
                        .and_then(|v| param_of(v, "name"))
                        .unwrap_or_default();
                    *part_filename = find_header(&headers, "content-disposition")
                        .and_then(|v| param_of(v, "filename"))
                        .unwrap_or_default();
                }
                Event::PartData(data) => {
                    *part_len += data.len();
                }
                Event::PartEnd { index } => {
                    report.push_str(&format!(
                        "part {index} name={part_name:?} filename={part_filename:?} bytes={part_len}\n"
                    ));
                    *open_part = None;
                }
                Event::Done => {}
            }
        }
    };

    loop {
        let n = stream.read(&mut chunk).map_err(|e| e.to_string())?;
        if n == 0 {
            break; // 对端关闭写方向
        }
        let events = parser.feed(&chunk[..n]).map_err(|e| e.to_string())?;
        handle_events(
            events,
            &mut report,
            &mut open_part,
            &mut part_len,
            &mut part_name,
            &mut part_filename,
        );
    }
    let events = parser.finish().map_err(|e| e.to_string())?;
    handle_events(
        events,
        &mut report,
        &mut open_part,
        &mut part_len,
        &mut part_name,
        &mut part_filename,
    );

    Ok(format!(
        "parts={} total_bytes={}\n{report}",
        parser.parts_seen(),
        parser.total_seen()
    ))
}

/// 逐字节读第一行（很短），格式 `BOUNDARY <boundary>\r\n`。
fn read_boundary_line(stream: &mut TcpStream) -> Result<String, String> {
    let mut line = Vec::new();
    let mut b = [0u8; 1];
    loop {
        let n = stream.read(&mut b).map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("connection closed before BOUNDARY line".into());
        }
        if b[0] == b'\n' {
            break;
        }
        line.push(b[0]);
        if line.len() > 128 {
            return Err("BOUNDARY line too long".into());
        }
    }
    if line.last() == Some(&b'\r') {
        line.pop();
    }
    let line = String::from_utf8(line).map_err(|_| "BOUNDARY line not UTF-8".to_string())?;
    line.strip_prefix("BOUNDARY ")
        .map(|s| s.to_string())
        .ok_or_else(|| "first line must be: BOUNDARY <boundary>".to_string())
}

fn find_header<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(n, _)| n.eq_ignore_ascii_case(name))
        .map(|(_, v)| v.as_str())
}

/// 从 `form-data; name="file"; filename="a.bin"` 中取参数值（极简实现，仅测试用）。
fn param_of(disposition: &str, key: &str) -> Option<String> {
    for seg in disposition.split(';').skip(1) {
        let seg = seg.trim();
        let (k, v) = seg.split_once('=')?;
        if k.trim().eq_ignore_ascii_case(key) {
            return Some(v.trim().trim_matches('"').to_string());
        }
    }
    None
}
