//! 本地 TCP 测试服务：极简 HTTP/1.1 握手 + SSE 事件流。
//!
//! 握手子集（手写解析，非完整 HTTP）：
//! - 读取到 `\r\n\r\n` 为止的请求头（上限 16 KiB）；
//! - 仅识别 `Last-Event-ID` 头（大小写不敏感），其余忽略；
//! - 响应固定 `200 OK` + `Content-Type: text/event-stream`。
//!
//! 事件由内置生成器产生：`id` 从 1 递增，`event: message`，`data: event-<id>`。
//! `drop_after` 用于模拟断线：每条连接发送 N 个事件后主动关闭。

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Duration;

use crate::encoder::{encode_event, ServerEvent};
use crate::history::{History, Replay, StoredEvent};

/// 服务配置。
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// 生成事件总数（达到后新连接只能拿到历史重放，随后正常关闭）。
    pub total: u64,
    /// 事件生成间隔（毫秒）。
    pub interval_ms: u64,
    /// 历史缓冲容量（Last-Event-ID 续传窗口）。
    pub history_cap: usize,
    /// 每条连接发送 N 个事件后主动断开（模拟断线）；`None` 不主动断。
    pub drop_after: Option<u64>,
    /// 连接建立后下发的 `retry:` 字段（毫秒）。
    pub retry_ms: Option<u64>,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            total: 100,
            interval_ms: 50,
            history_cap: 16,
            drop_after: None,
            retry_ms: Some(100),
        }
    }
}

struct Shared {
    history: History,
    produced: u64,
}

/// 绑定地址并运行服务（阻塞，直到 `stop` 置位）。
pub fn run_server(addr: &str, cfg: ServerConfig, stop: Arc<AtomicBool>) -> std::io::Result<()> {
    let listener = TcpListener::bind(addr)?;
    serve(listener, cfg, stop)
}

/// 在已绑定的 listener 上运行服务（测试用：先绑 0 端口拿到空闲端口）。
pub fn serve(listener: TcpListener, cfg: ServerConfig, stop: Arc<AtomicBool>) -> std::io::Result<()> {
    listener.set_nonblocking(true)?;
    let shared = Arc::new((
        Mutex::new(Shared {
            history: History::new(cfg.history_cap, 1),
            produced: 0,
        }),
        Condvar::new(),
    ));

    // 事件生成器线程。
    {
        let shared = Arc::clone(&shared);
        let cfg = cfg.clone();
        let stop = Arc::clone(&stop);
        thread::spawn(move || {
            let mut next_id: u64 = 1;
            loop {
                if stop.load(Ordering::Relaxed) {
                    return;
                }
                {
                    let (lock, _) = &*shared;
                    let guard = lock.lock().unwrap();
                    if guard.produced >= cfg.total {
                        return;
                    }
                }
                thread::sleep(Duration::from_millis(cfg.interval_ms));
                let raw = encode_event(&ServerEvent {
                    id: Some(next_id.to_string()),
                    event: Some("message".to_string()),
                    data: format!("event-{next_id}"),
                    retry: None,
                })
                .expect("generated event encodes");
                let (lock, cv) = &*shared;
                let mut guard = lock.lock().unwrap();
                guard.history.push(StoredEvent {
                    id: next_id,
                    raw: Arc::new(raw),
                });
                guard.produced += 1;
                next_id += 1;
                cv.notify_all();
            }
        });
    }

    // 接受循环。
    while !stop.load(Ordering::Relaxed) {
        match listener.accept() {
            Ok((stream, _)) => {
                let shared = Arc::clone(&shared);
                let cfg = cfg.clone();
                let stop = Arc::clone(&stop);
                thread::spawn(move || {
                    let _ = handle_conn(stream, shared, cfg, stop);
                });
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10));
            }
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// 游标过期时下发的明确重置事件（客户端收到后应清空去重状态）。
fn reset_event_bytes() -> Vec<u8> {
    encode_event(&ServerEvent {
        id: None,
        event: Some("reset".to_string()),
        data: "cursor-expired".to_string(),
        retry: None,
    })
    .expect("reset event encodes")
}

fn handle_conn(
    mut stream: TcpStream,
    shared: Arc<(Mutex<Shared>, Condvar)>,
    cfg: ServerConfig,
    stop: Arc<AtomicBool>,
) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    let last_id = read_request_headers(&mut stream)?;

    stream.write_all(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n",
    )?;
    if let Some(ms) = cfg.retry_ms {
        stream.write_all(format!("retry: {ms}\n\n").as_bytes())?;
    }

    let mut sent: u64 = 0;
    let mut last_sent: u64;

    // 重放阶段。
    {
        let (lock, _) = &*shared;
        let guard = lock.lock().unwrap();
        match guard.history.replay_after(last_id) {
            Replay::Expired => {
                stream.write_all(&reset_event_bytes())?;
                let kept = guard.history.events_after(0);
                for ev in &kept {
                    stream.write_all(&ev.raw)?;
                    sent += 1;
                }
                last_sent = kept.last().map(|e| e.id).unwrap_or(0);
            }
            Replay::Events(events) => {
                for ev in &events {
                    stream.write_all(&ev.raw)?;
                    sent += 1;
                }
                last_sent = events.last().map(|e| e.id).or(last_id).unwrap_or(0);
            }
        }
    }

    // 实时跟随阶段。
    loop {
        if stop.load(Ordering::Relaxed) {
            return Ok(());
        }
        if let Some(limit) = cfg.drop_after {
            if sent >= limit {
                // 模拟断线：直接关闭连接。
                return Ok(());
            }
        }
        let (lock, cv) = &*shared;
        let new_events = {
            let guard = lock.lock().unwrap();
            let (guard, _) = cv
                .wait_timeout(guard, Duration::from_millis(100))
                .unwrap();
            let done = guard.produced >= cfg.total;
            let evs = guard.history.events_after(last_sent);
            if evs.is_empty() && done {
                return Ok(()); // 全部生成且已送达：正常结束。
            }
            evs
        };
        for ev in new_events {
            stream.write_all(&ev.raw)?;
            last_sent = ev.id;
            sent += 1;
            if let Some(limit) = cfg.drop_after {
                if sent >= limit {
                    return Ok(());
                }
            }
        }
    }
}

/// 读取请求头直到 `\r\n\r\n`，解析出 Last-Event-ID（u64）。
fn read_request_headers(stream: &mut TcpStream) -> std::io::Result<Option<u64>> {
    const MAX_HEADER: usize = 16 * 1024;
    let mut buf = Vec::new();
    let mut tmp = [0u8; 1024];
    loop {
        if buf.len() > MAX_HEADER {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "request headers too large",
            ));
        }
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "connection closed before headers complete",
            ));
        }
        buf.extend_from_slice(&tmp[..n]);
        if buf.windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
    }
    let text = String::from_utf8_lossy(&buf);
    for line in text.split("\r\n").skip(1) {
        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("last-event-id") {
                let v = value.trim();
                return Ok(v.parse::<u64>().ok());
            }
        }
    }
    Ok(None)
}
