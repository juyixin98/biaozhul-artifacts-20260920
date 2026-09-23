//! 本地 TCP 回声测试服务（`wsecho` 二进制的核心）。
//!
//! 两种工作模式：
//! - **handshake 模式（默认）**：先完成 RFC 6455 HTTP 升级握手，再处理帧；
//! - **raw 模式（`--raw`）**：跳过握手，TCP 连上即视为已握手，方便用裸字节测试。
//!
//! 行为：
//! - 文本消息回文本帧，二进制消息回二进制帧；
//! - Ping 立即回 Pong（原样带回应用数据，§5.5.2），即使插在分片消息中间；
//! - Pong 静默忽略；
//! - 收到 Close：按 §5.5.1 回 Close（带回状态码），随后关闭连接；
//! - 任何 [`WsError`]：按映射到的 RFC 状态码发 Close，再关闭。

use std::io::{BufReader, BufWriter, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;

use crate::error::close_code;
use crate::frame::{close_payload, write_binary, write_close_raw, write_pong, write_text};
use crate::handshake::{read_handshake, response_bytes};
use crate::reassemble::{Event, Reassembler};
use crate::{Limits, WsError};

/// 服务配置。
#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub addr: String,
    /// 跳过 HTTP 握手（裸帧测试）。
    pub raw: bool,
    /// 帧/消息长度上限。
    pub limits: Limits,
    /// 把连接事件打印到 stderr。
    pub verbose: bool,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            addr: "127.0.0.1:9001".to_string(),
            raw: false,
            limits: Limits::DEFAULT,
            verbose: true,
        }
    }
}

/// 绑定端口并阻塞地接受连接（每连接一个线程）。
pub fn run(config: ServerConfig) -> std::io::Result<()> {
    let listener = TcpListener::bind(&config.addr)?;
    if config.verbose {
        eprintln!(
            "[wsecho] listening on {} ({})",
            config.addr,
            if config.raw { "raw frames, no handshake" } else { "RFC6455 handshake" }
        );
    }
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let cfg = config.clone();
                thread::spawn(move || {
                    if let Err(e) = handle_connection(stream, cfg) {
                        eprintln!("[wsecho] connection ended: {e}");
                    }
                });
            }
            Err(e) => eprintln!("[wsecho] accept failed: {e}"),
        }
    }
    Ok(())
}

/// 在一条已接受的 TCP 连接上完成（可选）握手与帧循环。
/// 暴露为公开 API 以便集成测试在随机端口上自行绑定 listener。
pub fn handle_connection(stream: TcpStream, config: ServerConfig) -> std::io::Result<()> {
    stream.set_nodelay(true).ok();

    let peer = stream.peer_addr().ok();
    if config.verbose {
        eprintln!("[wsecho] connection from {peer:?}");
    }

    let mut writer = BufWriter::new(stream.try_clone()?);
    let mut reader = BufReader::new(stream);

    if !config.raw {
        match read_handshake(&mut reader) {
            Ok(hs) => {
                writer.write_all(&response_bytes(&hs.key))?;
                writer.flush()?;
                if config.verbose {
                    eprintln!("[wsecho] handshake completed for {peer:?}");
                }
            }
            Err(e) => {
                // 不是合法的 WebSocket 握手：按 HTTP 语义回 400 后断开（§4.4 思路）。
                if config.verbose {
                    eprintln!("[wsecho] bad handshake: {e}");
                }
                let body = "400 Bad Request: invalid WebSocket handshake\r\n";
                let resp = format!(
                    "HTTP/1.1 400 Bad Request\r\nContent-Length: {}\r\nConnection: close\r\nContent-Type: text/plain\r\n\r\n{body}",
                    body.len()
                );
                let _ = writer.write_all(resp.as_bytes());
                let _ = writer.flush();
                return Ok(());
            }
        }
    }

    let result = frames_loop(&mut reader, &mut writer, config.limits, config.verbose);

    // 帧循环返回：正常关闭或协议错误。错误时发送对应关闭帧（尽力而为）。
    if let Err(ws_err) = &result {
        if let Some((code, reason)) = ws_err.close_code() {
            let mut out = Vec::new();
            write_close_raw(&mut out, &close_payload(code, reason));
            let _ = writer.write_all(&out);
            let _ = writer.flush();
        }
    }
    result.map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e.to_string()))
}

fn frames_loop<R: Read, W: Write>(
    reader: &mut R,
    writer: &mut W,
    limits: Limits,
    verbose: bool,
) -> Result<(), WsError> {
    let mut reassembler = Reassembler::new(limits);
    let mut byte = [0u8; 1];

    loop {
        // 逐字节读取并喂入——这正是“增量字节解析”的真实路径。
        let n = match reader.read(&mut byte) {
            Ok(n) => n,
            Err(_) => return Ok(()), // TCP 读错误：无法再发送任何帧
        };
        if n == 0 {
            // 对端 TCP 半关闭/断开。若正处在关闭握手之中属正常。
            return Ok(());
        }

        let event = reassembler.feed(byte[0])?;

        if let Some(ev) = event {
            let mut out = Vec::new();
            match ev {
                Event::Text(s) => {
                    if verbose {
                        eprintln!("[wsecho] text message ({} bytes): {s:?}", s.len());
                    }
                    write_text(&mut out, &s);
                }
                Event::Binary(b) => {
                    if verbose {
                        eprintln!("[wsecho] binary message ({} bytes)", b.len());
                    }
                    write_binary(&mut out, &b);
                }
                Event::Ping(p) => {
                    if verbose {
                        eprintln!("[wsecho] ping ({} bytes) -> pong", p.len());
                    }
                    write_pong(&mut out, &p);
                }
                Event::Pong(p) => {
                    if verbose {
                        eprintln!("[wsecho] pong ignored ({} bytes)", p.len());
                    }
                    continue;
                }
                Event::Close { code, reason } => {
                    let reply_code = code.unwrap_or(close_code::NORMAL);
                    if verbose {
                        eprintln!("[wsecho] close received: code={code:?} reason={reason:?}");
                    }
                    // §5.5.1：回 Close，带回状态码（并回显原因，载荷天然 ≤125）。
                    write_close_raw(&mut out, &close_payload(reply_code, &reason.unwrap_or_default()));
                    writer.write_all(&out).ok();
                    writer.flush().ok();
                    return Ok(());
                }
            }
            writer
                .write_all(&out)
                .map_err(|_| WsError::ConnectionClosed)?;
            writer.flush().map_err(|_| WsError::ConnectionClosed)?;
        }
    }
}
