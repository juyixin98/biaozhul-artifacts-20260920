//! 本地 TCP 测试服务：一个极简的 HTTP/1.1 接收端，用 [`crate::MultipartReader`]
//! **逐块**读取并解析 POST 上来的 `multipart/form-data`，返回每个 part 的
//! 名称/文件名/大小/SHA-256（流式计算，不落盘、不缓存正文）。
//!
//! 仅用于本地验收：不支持 keep-alive（每连接处理一个请求即关闭）、
//! 不支持 chunked 请求体、不做鉴权。请勿暴露到不可信网络。

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::Duration;

use crate::event::Event;
use crate::http;
use crate::mime;
use crate::reader::MultipartReader;
use crate::sha256::Sha256;
use crate::{Error, Limits};

/// 服务运行配置。
#[derive(Debug, Clone, Copy)]
pub struct ServerConfig {
    pub limits: Limits,
    /// 每次从 TCP 读取正文的字节数——刻意保持很小，强制边界频繁跨块。
    pub read_size: usize,
    /// 读写空闲超时。
    pub timeout: Duration,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            limits: Limits::default(),
            read_size: 512,
            timeout: Duration::from_secs(10),
        }
    }
}

#[derive(Debug)]
struct PartStat {
    name: String,
    filename: Option<String>,
    size: u64,
    sha: Sha256,
}

/// 在给定 listener 上接受连接并逐个处理（阻塞，直到出错或被 drop）。
/// 每个连接处理一个请求后关闭。
pub fn serve_listener(listener: TcpListener, config: ServerConfig) -> std::io::Result<()> {
    listener.set_nonblocking(false)?;
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                if let Err(e) = stream
                    .set_read_timeout(Some(config.timeout))
                    .and_then(|_| stream.set_write_timeout(Some(config.timeout)))
                {
                    eprintln!("set_timeout failed: {e}");
                    continue;
                }
                // 单线程顺序处理：本地验收场景足够，行为可预测。
                handle_connection(stream, config);
            }
            Err(e) => eprintln!("accept failed: {e}"),
        }
    }
    Ok(())
}

/// 处理一条连接（头 → 流式正文 → 响应）。对集成测试暴露。
pub fn handle_connection(mut stream: TcpStream, config: ServerConfig) {
    let result = handle_inner(&mut stream, config);
    let (status, body) = match result {
        Ok(json) => (200u16, json),
        Err((status, tag)) => (
            status,
            format!("{{\"ok\":false,\"error\":\"{}\"}}", json_escape(tag)),
        ),
    };
    let _ = write_response(&mut stream, status, body.as_bytes());
}

fn handle_inner(
    stream: &mut TcpStream,
    config: ServerConfig,
) -> Result<String, (u16, &'static str)> {
    let head = http::read_head(stream, 64 * 1024).map_err(|e| (e.status, e.message))?;

    let ct = head
        .content_type
        .as_deref()
        .ok_or((400u16, "missing_content_type"))?;
    let boundary = mime::parse_multipart_content_type(ct).map_err(|e| (400u16, e.tag()))?;
    let mut reader =
        MultipartReader::new(&boundary, config.limits).map_err(|e| (400u16, e.tag()))?;

    let mut stats: Vec<PartStat> = Vec::new();
    let mut current: Option<PartStat> = None;
    let mut remaining = head.content_length;
    let mut chunk = vec![0u8; config.read_size.max(1)];

    // 严格按 Content-Length 读取；先到 EOF 视为请求截断（→ truncated）。
    while remaining > 0 {
        let want = (remaining as usize).min(chunk.len());
        let n = stream
            .read(&mut chunk[..want])
            .map_err(|_| (400u16, "read_error"))?;
        if n == 0 {
            return Err((400u16, "truncated_request"));
        }
        remaining -= n as u64;

        let events = reader
            .feed(&chunk[..n])
            .map_err(|e| (status_for(&e), e.tag()))?;
        for ev in events {
            match ev {
                Event::PartBegin(meta) => {
                    current = Some(PartStat {
                        name: meta.name,
                        filename: meta.filename,
                        size: 0,
                        sha: Sha256::new(),
                    });
                }
                Event::Body(data) => {
                    let p = current.as_mut().ok_or((400u16, "parser_state_error"))?;
                    p.size += data.len() as u64;
                    p.sha.update(&data);
                }
                Event::PartEnd => {
                    if let Some(p) = current.take() {
                        stats.push(p);
                    }
                }
                Event::End => { /* 统计在 finish 前已经就绪 */ }
            }
        }
    }

    reader.finish().map_err(|e| (status_for(&e), e.tag()))?;
    if let Some(p) = current.take() {
        // 理论上不会到达：关闭边界前每个 part 都有 PartEnd；稳妥起见仍处理。
        stats.push(p);
    }

    let total_parts = stats.len();
    let total_bytes: u64 = stats.iter().map(|p| p.size).sum();
    let mut out = String::new();
    out.push_str("{\"ok\":true,\"parts\":[");
    for (i, p) in stats.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        let digest = p.sha.clone().finalize();
        let hex: String = digest.iter().map(|b| format!("{:02x}", b)).collect();
        out.push_str(&format!(
            "{{\"name\":\"{}\",\"filename\":{},\"size\":{},\"sha256\":\"{}\"}}",
            json_escape(&p.name),
            match &p.filename {
                Some(f) => format!("\"{}\"", json_escape(f)),
                None => "null".to_string(),
            },
            p.size,
            hex
        ));
    }
    out.push_str(&format!(
        "],\"part_count\":{},\"total_bytes\":{}}}",
        total_parts, total_bytes
    ));
    Ok(out)
}

fn write_response(stream: &mut TcpStream, status: u16, body: &[u8]) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        405 => "Method Not Allowed",
        413 => "Payload Too Large",
        431 => "Request Header Fields Too Large",
        _ => "Error",
    };
    let head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        status,
        reason,
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body)?;
    stream.flush()
}

/// 解析错误 → HTTP 状态码：超限类 413，其余 400。
fn status_for(e: &Error) -> u16 {
    if e.is_size_limit() {
        413
    } else {
        400
    }
}

/// 最小 JSON 字符串转义（控制字符、引号、反斜杠）。
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
