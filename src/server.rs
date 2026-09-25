//! 本地 TCP 测试服务：完成升级握手后，进入 WebSocket 帧处理循环。
//!
//! 行为（echo 服务，便于自动化验收）：
//! - Text / Binary 消息：原样回显
//! - Ping：回 Pong（携带相同应用数据）
//! - Pong：忽略
//! - Close：原样回显关闭帧后进入关闭握手
//! - 任何协议错误：先发对应关闭码（1002/1007/1009…），再关闭连接

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::Duration;

use crate::error::Error;
use crate::frame::{encode_frame, Frame, FrameReader, Opcode, PeerRole};
use crate::handshake;
use crate::message::{Assembler, Event};

/// 服务端配置（长度上限）。
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// 单帧有效载荷上限（字节），默认 1 MiB。
    pub max_frame_payload: usize,
    /// 重组后的单条消息上限（字节），默认 1 MiB。
    pub max_message: usize,
    /// 发出 Close 后等待对端关闭的最长时间。
    pub close_drain_timeout: Duration,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            max_frame_payload: 1 << 20,
            max_message: 1 << 20,
            close_drain_timeout: Duration::from_secs(5),
        }
    }
}

/// 一个监听中的 WebSocket 测试服务。
pub struct WsServer {
    listener: TcpListener,
    config: ServerConfig,
}

impl WsServer {
    pub fn bind(addr: &str) -> std::io::Result<Self> {
        Self::bind_with_config(addr, ServerConfig::default())
    }

    pub fn bind_with_config(addr: &str, config: ServerConfig) -> std::io::Result<Self> {
        let listener = TcpListener::bind(addr)?;
        listener.set_nonblocking(false)?;
        Ok(WsServer { listener, config })
    }

    pub fn local_addr(&self) -> std::io::Result<std::net::SocketAddr> {
        self.listener.local_addr()
    }

    /// 接受连接循环（每连接一个线程）。accept 失败时返回。
    pub fn run(&self) {
        for stream in self.listener.incoming() {
            match stream {
                Ok(stream) => {
                    let cfg = self.config.clone();
                    std::thread::spawn(move || {
                        if let Err(e) = serve_connection(stream, &cfg) {
                            eprintln!("connection ended: {e}");
                        }
                    });
                }
                Err(e) => {
                    eprintln!("accept error: {e}");
                    break;
                }
            }
        }
    }
}

/// 处理单条连接（握手 + 帧循环）。暴露为 pub 以便集成测试直接驱动。
pub fn serve_connection(mut stream: TcpStream, cfg: &ServerConfig) -> Result<(), Error> {
    stream.set_nodelay(true).ok();

    // ---- 握手 -------------------------------------------------------------
    let mut req_buf: Vec<u8> = Vec::with_capacity(512);
    let mut byte = [0u8; 1];
    loop {
        if stream.read(&mut byte).map_err(Error::from)? == 0 {
            return Err(Error::ConnectionClosed);
        }
        req_buf.push(byte[0]);
        if req_buf.len() > 16 * 1024 {
            stream
                .write_all(&handshake::bad_request_response("request too large"))
                .ok();
            return Err(Error::Io("handshake request too large".into()));
        }
        if req_buf.ends_with(b"\r\n\r\n") {
            break;
        }
    }

    let request = match handshake::parse_upgrade(&req_buf) {
        Ok(req) => req,
        Err(reason) => {
            eprintln!("handshake rejected: {reason}");
            stream
                .write_all(&handshake::bad_request_response(&reason))
                .ok();
            return Err(Error::Io(format!("bad handshake: {reason}")));
        }
    };

    // request.key 已在解析时校验为 16 字节；accept 值按原始 Base64 头计算
    let key_b64 = extract_header(&req_buf, "Sec-WebSocket-Key").unwrap_or_default();
    let accept = handshake::accept_key(&key_b64);
    let _ = request; // 解析结果的存在即证明头部合法
    stream
        .write_all(&handshake::upgrade_response(&accept))
        .map_err(Error::from)?;

    // ---- 帧处理循环 --------------------------------------------------------
    let mut reader = FrameReader::new(PeerRole::Client, cfg.max_frame_payload);
    let mut assembler = Assembler::new(cfg.max_message);
    let mut buf = [0u8; 4096];

    loop {
        let n = match stream.read(&mut buf) {
            Ok(0) => return Ok(()), // 对端正常关闭
            Ok(n) => n,
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(e) => return Err(Error::from(e)),
        };

        // 逐字节喂入解析器：演示"增量"语义，且任何字节都可能立即触发协议错误
        for &b in &buf[..n] {
            let frame = match reader.feed_byte(b) {
                Ok(Some(f)) => f,
                Ok(None) => continue,
                Err(e) => return fail_and_close(&mut stream, e, cfg.close_drain_timeout),
            };
            match assembler.handle(frame) {
                Ok(Event::Message(msg)) => {
                    let opcode = if msg.is_text {
                        Opcode::Text
                    } else {
                        Opcode::Binary
                    };
                    let echo = Frame::complete(opcode, msg.data);
                    if write_frame(&mut stream, &echo).is_err() {
                        return Ok(());
                    }
                }
                Ok(Event::Ping(payload)) => {
                    // RFC 6455 §5.5.3：收到 Ping 必须回 Pong（可插入分片之间）
                    let pong = Frame::complete(Opcode::Pong, payload);
                    if write_frame(&mut stream, &pong).is_err() {
                        return Ok(());
                    }
                }
                Ok(Event::Pong(_)) => { /* 主动 Ping 未实现，忽略即可 */ }
                Ok(Event::StreamProgress) => {}
                Ok(Event::Close { raw_body, .. }) => {
                    // §5.5.1：回显相同的 Close 帧，然后做关闭握手
                    let close = Frame::complete(Opcode::Close, raw_body);
                    write_frame(&mut stream, &close).ok();
                    return graceful_drain(&mut stream, cfg.close_drain_timeout);
                }
                Err(e) => return fail_and_close(&mut stream, e, cfg.close_drain_timeout),
            }
        }
    }
}

fn write_frame(stream: &mut TcpStream, frame: &Frame) -> std::io::Result<()> {
    let bytes = encode_frame(frame); // 服务端帧不掩码
    stream.write_all(&bytes)
}

/// 协议/数据错误：发 Close 帧（带对应关闭码与简短原因），随后关闭。
fn fail_and_close(stream: &mut TcpStream, e: Error, timeout: Duration) -> Result<(), Error> {
    eprintln!("protocol error: {e}");
    if let Some(code) = e.close_code() {
        let mut body = code.code().to_be_bytes().to_vec();
        // 关闭原因：仅放简短 ASCII（天然合法 UTF-8）
        let reason = match code {
            crate::CloseCode::MessageTooBig => "message too big",
            crate::CloseCode::InvalidPayloadData => "invalid utf-8",
            _ => "protocol error",
        };
        body.extend_from_slice(reason.as_bytes());
        let close = Frame::complete(Opcode::Close, body);
        write_frame(stream, &close).ok();
    }
    graceful_drain(stream, timeout)
}

/// 发出 Close 后等待对端关闭，避免触发 RST。
fn graceful_drain(stream: &mut TcpStream, timeout: Duration) -> Result<(), Error> {
    stream.flush().ok();
    stream.shutdown(std::net::Shutdown::Write).ok();
    stream.set_read_timeout(Some(timeout)).ok();
    let mut buf = [0u8; 512];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => return Ok(()),
            Ok(_) => continue, // 丢弃对端在关闭前的残余数据
            Err(_) => return Ok(()),
        }
    }
}

/// 大小写不敏感地从已缓冲的请求头中取单个头字段值。
fn extract_header(buf: &[u8], name: &str) -> Option<String> {
    let text = std::str::from_utf8(buf).ok()?;
    for line in text.split("\r\n").skip(1) {
        if let Some((k, v)) = line.split_once(':') {
            if k.trim().eq_ignore_ascii_case(name) {
                return Some(v.trim().to_string());
            }
        }
    }
    None
}
