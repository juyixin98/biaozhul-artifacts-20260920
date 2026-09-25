//! 客户端：连接、增量解码、断线自动重连续传。
//!
//! - [`stream_once`]：建立一次连接，把流式输出交给回调，直到服务端关闭。
//! - [`run_client`]：断线后用 Last-Event-ID 自动重连，按 id 去重，
//!   收到 `reset` 事件时清空去重状态重新同步。

use std::collections::BTreeSet;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use crate::parser::{Event, Output, Parser};

/// 客户端侧消息。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClientMsg {
    /// 普通事件。
    Event(Event),
    /// 服务端下发的重连间隔（毫秒）。
    Retry(u64),
    /// 游标过期，服务端要求重置（本地去重状态已无效）。
    Reset,
}

/// 建立一次连接并消费到 EOF。`last_id` 非空时以 Last-Event-ID 头发送。
pub fn stream_once(
    addr: &str,
    last_id: Option<&str>,
    handler: &mut dyn FnMut(ClientMsg),
) -> std::io::Result<()> {
    let mut stream = TcpStream::connect(addr)?;
    let mut req = format!("GET /events HTTP/1.1\r\nHost: {addr}\r\n");
    if let Some(id) = last_id {
        req.push_str(&format!("Last-Event-ID: {id}\r\n"));
    }
    req.push_str("\r\n");
    stream.write_all(req.as_bytes())?;

    // 先读掉响应头（到 \r\n\r\n 为止），余下字节即 SSE 流。
    let mut head = Vec::new();
    let mut tmp = [0u8; 4096];
    let body_start = loop {
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "connection closed before response headers",
            ));
        }
        head.extend_from_slice(&tmp[..n]);
        if let Some(pos) = head.windows(4).position(|w| w == b"\r\n\r\n") {
            break pos + 4;
        }
        if head.len() > 16 * 1024 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "response headers too large",
            ));
        }
    };
    let head_text = String::from_utf8_lossy(&head[..body_start]);
    if !head_text.starts_with("HTTP/1.1 200") {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("unexpected response: {}", head_text.lines().next().unwrap_or("")),
        ));
    }

    let mut parser = Parser::new();
    let mut pump = |chunk: &[u8], parser: &mut Parser| -> std::io::Result<()> {
        let outputs = parser
            .feed(chunk)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        for out in outputs {
            match out {
                Output::Event(ev) if ev.event == "reset" => handler(ClientMsg::Reset),
                Output::Event(ev) => handler(ClientMsg::Event(ev)),
                Output::Retry(ms) => handler(ClientMsg::Retry(ms)),
            }
        }
        Ok(())
    };

    pump(&head[body_start..], &mut parser)?;
    loop {
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            break; // EOF：服务端关闭（正常结束或断线）。
        }
        pump(&tmp[..n], &mut parser)?;
    }
    // 未派发完的半个事件随连接丢弃，等待重放。
    let _ = parser.finish();
    Ok(())
}

/// 重连续传配置。
#[derive(Debug, Clone)]
pub struct ReconnectConfig {
    /// 收到 N 个不同 id 的事件后正常退出。
    pub expect_total: u64,
    /// 最大重连次数（防死循环）。
    pub max_reconnects: u64,
    /// 重连间隔（毫秒）。
    pub retry_delay_ms: u64,
}

/// 一次完整客户端会话的统计。
#[derive(Debug, Default, Clone)]
pub struct ClientOutcome {
    /// 按接收顺序记录的不同事件 id。
    pub received: Vec<u64>,
    /// 重复收到的事件 id（仅允许出现在断线边界）。
    pub duplicates: Vec<u64>,
    /// 收到 reset 的次数。
    pub resets: u64,
    /// 实际重连次数。
    pub reconnects: u64,
}

/// 自动重连客户端：断线后携带 Last-Event-ID 重连，直到收齐
/// `expect_total` 个不同事件或超过最大重连次数。
pub fn run_client(
    addr: &str,
    cfg: ReconnectConfig,
    handler: &mut dyn FnMut(ClientMsg),
) -> std::io::Result<ClientOutcome> {
    let mut outcome = ClientOutcome::default();
    let mut seen: BTreeSet<u64> = BTreeSet::new();
    let mut last_id: Option<String> = None;

    loop {
        let mut resets_this_round = 0u64;
        // 快照本次连接要发送的游标，避免与闭包内的可变借用冲突。
        let send_id = last_id.clone();
        let result = stream_once(addr, send_id.as_deref(), &mut |msg| {
            match &msg {
                ClientMsg::Reset => {
                    // 游标过期：去重状态作废，重新同步。
                    seen.clear();
                    last_id = None;
                    resets_this_round += 1;
                }
                ClientMsg::Event(ev) => {
                    if let Ok(n) = ev.id.parse::<u64>() {
                        if !seen.insert(n) {
                            outcome.duplicates.push(n);
                        } else {
                            outcome.received.push(n);
                        }
                    }
                    last_id = Some(ev.id.clone());
                }
                ClientMsg::Retry(_) => {}
            }
            handler(msg);
        });
        outcome.resets += resets_this_round;
        result?;

        if seen.len() as u64 >= cfg.expect_total {
            return Ok(outcome);
        }
        outcome.reconnects += 1;
        if outcome.reconnects > cfg.max_reconnects {
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                format!(
                    "exceeded max reconnects ({}), received {}/{}",
                    cfg.max_reconnects,
                    seen.len(),
                    cfg.expect_total
                ),
            ));
        }
        std::thread::sleep(Duration::from_millis(cfg.retry_delay_ms));
    }
}
