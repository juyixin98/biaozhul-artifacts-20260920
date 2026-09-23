//! 本地 TCP SSE 测试服务（纯 `std::net`，无 HTTP 框架）。
//!
//! # HTTP 子集
//!
//! 只实现测试所需的最小 HTTP/1.1 子集：
//!
//! * `GET /events` → `200 text/event-stream`，长连接；
//! * `Last-Event-ID: <u64>` 请求头用于断线续流；
//! * 其余路径 `404`、其余方法 `405`、畸形请求行/超长请求头 `400`；
//! * 响应固定 `Connection: close`，EOF 即连接结束，不做 keep-alive/分块编码。
//!
//! # 续流与历史
//!
//! 服务端为每个事件分配**单调递增的 u64 序号**作为 SSE `id`，并保留最近
//! `history_capacity` 个已编码帧（有限历史，见 [`Server::publish`]）。
//!
//! 重连时对 `Last-Event-ID` 的判定：
//!
//! | 客户端游标 | 结果 |
//! |---|---|
//! | 无该头 | 从**当前最新位置**开始（只收到新事件），不重放 |
//! | `n` 且历史中存在 `> n` 的事件 | 重放这些事件（可能重复，见下） |
//! | `n` 早于历史最旧 id（已过期） | 先发明确的 **`reset` 事件**，再走实时流 |
//! | `n` 不是数字 | 同上，reason=`cursor-invalid` |
//! | `n` 比最新 id 还大 | 同上，reason=`cursor-ahead` |
//!
//! `reset` 事件形如：
//!
//! ```text
//! id: 199
//! event: reset
//! data: {"reason":"cursor-expired","yourLastEventId":"5","earliestId":100,"latestId":199,"resumeAfter":199}
//!
//! ```
//!
//! 客户端看到 `event: reset` 就知道：从 `n` 到 `resumeAfter` 之间的事件**永久
//! 丢失**（服务端只保留有限历史），必须按业务做全量重新同步，而不能假装无缺口。
//!
//! # 关于“允许的重复边界”
//!
//! 服务端在**提交帧之前**崩溃/断网时，客户端可能已收到事件却没（或没来得及）
//! 更新游标。重连带旧游标时，服务端会**再发一次**该事件——这就是规范允许的
//! at-least-once 重复。本服务不做去重；去重/幂等是消费方的责任，集成测试
//! `tests/reconnect.rs` 明确断言了这个边界。

use crate::encode::{encode_comment_into, encode_into, OutEvent};
use std::collections::VecDeque;
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Duration;

/// 心跳注释发送间隔（无新事件时保活）。
pub const PING_INTERVAL: Duration = Duration::from_secs(15);
/// 请求头（含请求行）允许的总字节数，防御性上限。
const MAX_HEADER_BYTES: usize = 16 * 1024;
/// 读请求头的超时。
const HEADER_READ_TIMEOUT: Duration = Duration::from_secs(10);
/// 初始 `retry` 提示（毫秒）。
const INITIAL_RETRY_MS: u64 = 3000;

/// 一条已编码的历史帧：(序号 id, 帧字节)。
type Frame = (u64, Vec<u8>);

struct State {
    /// 下一个事件将分配到的序号（同时也是“最新已分配序号”的上界）。
    seq: u64,
    /// 有限历史；front 最旧，back 最新。
    history: VecDeque<Frame>,
}

struct Inner {
    state: Mutex<State>,
    /// publish 时通知所有挂起的连接。
    cv: Condvar,
    history_capacity: usize,
}

/// SSE 测试服务句柄。用 [`Server::bind`] 创建，[`Server::spawn`] 起后台服务。
pub struct Server {
    inner: Arc<Inner>,
    listener: Option<TcpListener>,
    addr: SocketAddr,
}

/// 运行中的服务；drop 时停止 accept 循环并回收线程（已建立的连接随客户端关闭）。
pub struct Running {
    server: Server,
    shutdown: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Running {
    /// 服务监听地址（绑到 0 端口时用它拿实际端口）。
    pub fn addr(&self) -> SocketAddr {
        self.server.addr
    }

    /// 发布一个事件，返回服务端分配的序号 id。
    ///
    /// 序号会**覆盖** `ev.id`；历史按 [`Server::bind`] 给定的容量裁剪。
    /// 若事件字段值含裸 CR/LF，返回 [`crate::error::EncodeError`] 对应的
    /// `InvalidData` io 错误，且不消耗序号。
    pub fn publish(&self, mut ev: OutEvent) -> std::io::Result<u64> {
        self.server.publish(&mut ev)
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::SeqCst);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

impl Server {
    /// 绑定本地 TCP 端口。`history_capacity` 为 0 时表示**不保留任何历史**
    /// （任何非空 Last-Event-ID 都会得到 reset）。
    pub fn bind(addr: SocketAddr, history_capacity: usize) -> std::io::Result<Server> {
        let listener = TcpListener::bind(addr)?;
        let addr = listener.local_addr()?;
        Ok(Server {
            inner: Arc::new(Inner {
                state: Mutex::new(State {
                    seq: 0,
                    history: VecDeque::new(),
                }),
                cv: Condvar::new(),
                history_capacity,
            }),
            listener: Some(listener),
            addr,
        })
    }

    /// 实际监听地址。
    pub fn local_addr(&self) -> SocketAddr {
        self.addr
    }

    /// 在后台线程启动 accept 循环，返回可 drop 的运行句柄。
    pub fn spawn(mut self) -> std::io::Result<Running> {
        let listener = self.listener.take().expect("listener already taken");
        // 非阻塞轮询，便于 Drop 时及时退出。
        listener.set_nonblocking(true)?;
        let inner = self.inner.clone();
        let shutdown = Arc::new(AtomicBool::new(false));
        let sd = shutdown.clone();
        let t = thread::Builder::new()
            .name("sse-accept".into())
            .spawn(move || serve_loop(listener, inner, sd))?;
        Ok(Running {
            server: self,
            shutdown,
            thread: Some(t),
        })
    }

    /// 当前（阻塞式）accept 循环；通常用 [`Server::spawn`]，此方法留给自带
    /// 线程管理的调用方。
    pub fn run(self) -> std::io::Result<()> {
        let listener = self.listener.expect("listener already taken");
        serve_loop_blocking(listener, self.inner)
    }

    fn publish(&self, ev: &mut OutEvent) -> std::io::Result<u64> {
        // 先编码验证：非法事件不消耗序号。
        let (id, frame) = {
            let st = &mut *self.inner.state.lock().unwrap();
            st.seq += 1;
            let id = st.seq;
            ev.id = Some(id.to_string());
            let mut buf = Vec::with_capacity(128);
            encode_into(&mut buf, ev).map_err(|e| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, e.to_string())
            })?;
            (id, buf)
        };
        {
            let st = &mut *self.inner.state.lock().unwrap();
            st.history.push_back((id, frame));
            while st.history.len() > self.inner.history_capacity {
                st.history.pop_front();
            }
        }
        self.inner.cv.notify_all();
        Ok(id)
    }
}

fn serve_loop(listener: TcpListener, inner: Arc<Inner>, shutdown: Arc<AtomicBool>) {
    while !shutdown.load(Ordering::SeqCst) {
        match listener.accept() {
            Ok((stream, _peer)) => {
                // listener 是非阻塞的，accept 出的连接继承非阻塞模式；
                // 在 spawn 前立即切回阻塞，消除任何“处理线程尚未设置”的时间窗。
                if let Err(e) = stream.set_nonblocking(false) {
                    eprintln!("[sse] 连接设置阻塞模式失败: {e}");
                    continue;
                }
                let inner = inner.clone();
                thread::spawn(move || {
                    match handle_connection(stream, inner) {
                        // 客户端断开 (BrokenPipe/ConnectionReset/读头超时) 是正常情况，
                        // 只在调试构建里打印。
                        #[cfg(debug_assertions)]
                        Err(e) => eprintln!("[sse] 连接结束: {e}"),
                        #[cfg(not(debug_assertions))]
                        Err(_e) => {}
                        Ok(()) => {}
                    }
                });
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10));
            }
            Err(e) => {
                eprintln!("[sse] accept 失败，服务循环退出: {e}");
                break;
            }
        }
    }
}

fn serve_loop_blocking(listener: TcpListener, inner: Arc<Inner>) -> std::io::Result<()> {
    for stream in listener.incoming() {
        let stream = stream?;
        let inner = inner.clone();
        thread::spawn(move || {
            let _ = handle_connection(stream, inner);
        });
    }
    Ok(())
}

/// 解析后的最小 HTTP 请求。
struct Request {
    #[allow(dead_code)]
    method: String,
    path: String,
    last_event_id: Option<String>,
}

fn handle_connection(stream: TcpStream, inner: Arc<Inner>) -> std::io::Result<()> {
    // accept 自非阻塞 listener 的连接会继承非阻塞模式；本处理线程按阻塞 I/O
    // （配合读超时）使用，必须显式恢复阻塞，否则 read 会报 WouldBlock。
    stream.set_nonblocking(false)?;
    stream.set_read_timeout(Some(HEADER_READ_TIMEOUT))?;
    stream.set_nodelay(true)?;

    let req = {
        let mut reader = BufReader::new(&stream);
        match read_request(&mut reader)? {
            Some(req) => req,
            None => {
                // EOF before any bytes — client probe; just close.
                return Ok(());
            }
        }
        // reader dropped here; server never reads the request body.
    };

    if req.path != "/events" {
        return send_simple(&stream, 404, "Not Found", "only GET /events is served\n");
    }
    if req.method != "GET" {
        return send_simple(&stream, 405, "Method Not Allowed", "use GET\n");
    }

    // 建立 SSE 输出。
    let write_stream = stream.try_clone()?;
    let mut out = BufWriter::new(write_stream);

    // 在锁内一次性算出重放/重置决策并快照需要发送的帧。
    let decision = {
        let st = &*inner.state.lock().unwrap();
        decide(st, req.last_event_id.as_deref())
    };

    write_headers(&mut out)?;
    // 初始提示与连通注释。
    writeln!(out, "retry: {INITIAL_RETRY_MS}")?;
    encode_comment_into(&mut out, "connected").map_err(crate::error::EncodeError::into_io)?;

    let mut last_seq = match decision {
        Decision::Live { next } => {
            out.flush()?;
            next
        }
        Decision::Replay { frames, next } => {
            encode_comment_into(&mut out, &format!("resume replay {} event(s)", frames.len())).map_err(crate::error::EncodeError::into_io)?;
            for (_id, frame) in &frames {
                out.write_all(frame)?;
            }
            out.flush()?;
            next
        }
        Decision::Reset { frame, next } => {
            out.write_all(&frame)?;
            out.flush()?;
            next
        }
    };

    // 实时循环：挂起在条件变量上，publish 唤醒；超时发心跳。
    loop {
        let snap = {
            let guard = inner.state.lock().unwrap();
            let (guard, _timeout) = inner
                .cv
                .wait_timeout_while(guard, PING_INTERVAL, |s| s.seq == last_seq)
                .unwrap();
            let s = &*guard;
            if s.seq == last_seq {
                LiveSnap::Ping
            } else if let Some((oldest, _)) = s.history.front() {
                if *oldest > last_seq.saturating_add(1) {
                    // 极端情况：两次唤醒之间历史整体滚动，出现不可补缺口。
                    let latest = s.history.back().map(|(id, _)| *id).unwrap_or(s.seq);
                    let frame = build_reset_frame(
                        "cursor-expired",
                        &last_seq.to_string(),
                        Some(*oldest),
                        latest,
                    );
                    LiveSnap::Gap { frame, latest }
                } else {
                    let frames: Vec<Vec<u8>> = s
                        .history
                        .iter()
                        .filter(|(id, _)| *id > last_seq)
                        .map(|(_, f)| f.clone())
                        .collect();
                    LiveSnap::Events {
                        frames,
                        latest: s.seq,
                    }
                }
            } else {
                // seq 前进了但历史为空（capacity=0）：无法补，直接重置。
                LiveSnap::Gap {
                    frame: build_reset_frame("cursor-expired", &last_seq.to_string(), None, s.seq),
                    latest: s.seq,
                }
            }
        };

        match snap {
            LiveSnap::Ping => {
                encode_comment_into(&mut out, "ping").map_err(crate::error::EncodeError::into_io)?;
                out.flush()?;
            }
            LiveSnap::Events { frames, latest } => {
                for f in &frames {
                    out.write_all(f)?;
                }
                out.flush()?;
                last_seq = latest;
            }
            LiveSnap::Gap { frame, latest } => {
                out.write_all(&frame)?;
                out.flush()?;
                last_seq = latest;
            }
        }
    }
}

enum LiveSnap {
    Ping,
    Events { frames: Vec<Vec<u8>>, latest: u64 },
    Gap { frame: Vec<u8>, latest: u64 },
}

/// 建链时的续流决策。
enum Decision {
    /// 无 Last-Event-ID：只看实时。
    Live { next: u64 },
    /// 游标有效：重放严格大于游标的历史帧。
    Replay { frames: Vec<Frame>, next: u64 },
    /// 游标无效/过期/超前：先发重置帧，再跟实时流。
    Reset { frame: Vec<u8>, next: u64 },
}

fn decide(st: &State, last_event_id: Option<&str>) -> Decision {
    let latest = st.history.back().map(|(id, _)| *id).unwrap_or(0);
    let cursor = match last_event_id {
        None => return Decision::Live { next: st.seq },
        Some(v) => v.trim(),
    };

    let parsed: Option<u64> = if !cursor.is_empty() && cursor.bytes().all(|b| b.is_ascii_digit())
    {
        cursor.parse().ok()
    } else {
        None
    };

    let n = match parsed {
        Some(n) => n,
        None => {
            return Decision::Reset {
                frame: build_reset_frame("cursor-invalid", cursor, st.history.front().map(|(id, _)| *id), latest),
                next: st.seq,
            }
        }
    };

    match st.history.front() {
        None => {
            // 历史为空（capacity=0 或尚无事件）：游标必然无法服务。
            Decision::Reset {
                frame: build_reset_frame("cursor-expired", &n.to_string(), None, latest),
                next: st.seq,
            }
        }
        Some((oldest, _)) if n + 1 < *oldest => Decision::Reset {
            frame: build_reset_frame(
                "cursor-expired",
                &n.to_string(),
                Some(*oldest),
                latest,
            ),
            next: st.seq,
        },
        _ if n > latest => Decision::Reset {
            frame: build_reset_frame(
                "cursor-ahead",
                &n.to_string(),
                st.history.front().map(|(id, _)| *id),
                latest,
            ),
            next: st.seq,
        },
        _ => {
            // 游标落在历史窗口内（含恰好等于最新）：重放 > n 的帧。
            let frames: Vec<Frame> = st
                .history
                .iter()
                .filter(|(id, _)| *id > n)
                .map(|(id, f)| (*id, f.clone()))
                .collect();
            Decision::Replay {
                frames,
                next: st.seq,
            }
        }
    }
}

/// 构造 reset 事件帧。`latest == 0`（无任何事件）时不写 id，避免伪造游标。
fn build_reset_frame(
    reason: &str,
    your_last_event_id: &str,
    earliest: Option<u64>,
    latest: u64,
) -> Vec<u8> {
    let mut data = String::new();
    data.push_str("{\"reason\":\"");
    json_escape_into(reason, &mut data);
    data.push_str("\",\"yourLastEventId\":\"");
    json_escape_into(your_last_event_id, &mut data);
    data.push_str("\",");
    match earliest {
        Some(e) => data.push_str(&format!("\"earliestId\":{e},")),
        None => data.push_str("\"earliestId\":null,"),
    }
    data.push_str(&format!(
        "\"latestId\":{latest},\"resumeAfter\":{latest}}}"
    ));

    let ev = OutEvent {
        id: if latest > 0 {
            Some(latest.to_string())
        } else {
            None
        },
        event: Some("reset".into()),
        retry_ms: None,
        data: vec![data],
    };
    crate::encode::encode_to_vec(&ev).expect("reset 帧字段均为自控内容，编码不会失败")
}

fn json_escape_into(s: &str, out: &mut String) {
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
}

fn write_headers(out: &mut impl Write) -> std::io::Result<()> {
    write!(
        out,
        "HTTP/1.1 200 OK\r\n\
         Content-Type: text/event-stream;charset=utf-8\r\n\
         Cache-Control: no-cache\r\n\
         Connection: close\r\n\
         Access-Control-Allow-Origin: *\r\n\
         \r\n"
    )?;
    out.flush()?;
    Ok(())
}

fn send_simple(stream: &TcpStream, code: u16, reason: &str, body: &str) -> std::io::Result<()> {
    let mut out = BufWriter::new(stream.try_clone()?);
    write!(
        out,
        "HTTP/1.1 {code} {reason}\r\n\
         Content-Type: text/plain;charset=utf-8\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\
         \r\n\
         {body}",
        body.len()
    )?;
    out.flush()
}

/// 从缓冲读取器解析请求行 + 头部；读到空行为止。
fn read_request<R: Read>(reader: &mut BufReader<R>) -> std::io::Result<Option<Request>> {
    let mut total = 0usize;
    let mut line = Vec::new();

    // 请求行（解析后立即转成 owned 数据，避免后续复用 line 时的借用冲突）。
    let n = read_line_limited(reader, &mut line, &mut total)?;
    if n == 0 {
        return Ok(None); // 连接上没有任何字节
    }
    let (method, path) = {
        let text = std::str::from_utf8(&line)
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "non-utf8 request line"))?;
        let text = text.trim_end_matches(['\r', '\n']);
        let mut parts = text.split(' ');
        let method = parts.next().ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "bad request line")
        })?;
        let target = parts.next().ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "bad request line")
        })?;
        let _version = parts.next().ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "bad request line")
        })?;
        if parts.next().is_some() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "bad request line",
            ));
        }
        let path = target.split('?').next().unwrap_or(target).to_string();
        (method.to_string(), path)
    };

    // 头部。
    let mut last_event_id = None;
    loop {
        line.clear();
        let n = read_line_limited(reader, &mut line, &mut total)?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "headers incomplete",
            ));
        }
        // 复制成 String 后再解析，line 可在下轮清空复用。
        let h = match std::str::from_utf8(&line) {
            Ok(s) => s.trim_end_matches(['\r', '\n']).to_string(),
            Err(_) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "non-utf8 header",
                ))
            }
        };
        if h.is_empty() {
            break; // 头部结束
        }
        if let Some((name, value)) = h.split_once(':') {
            if name.eq_ignore_ascii_case("last-event-id") {
                last_event_id = Some(value.trim().to_string());
            }
        }
    }

    Ok(Some(Request {
        method,
        path,
        last_event_id,
    }))
}

/// 读到 `\n`，累计总字节不超 [`MAX_HEADER_BYTES`]。返回读到的字节数
/// （0 表示 EOF）。
fn read_line_limited<R: Read>(
    reader: &mut BufReader<R>,
    buf: &mut Vec<u8>,
    total: &mut usize,
) -> std::io::Result<usize> {
    let n = reader
        .by_ref()
        .take((MAX_HEADER_BYTES - *total) as u64 + 1)
        .read_until(b'\n', buf)?;
    *total += n;
    if *total > MAX_HEADER_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "headers too large",
        ));
    }
    Ok(n)
}
