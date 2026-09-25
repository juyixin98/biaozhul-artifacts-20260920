//! Multiplexing RPC server (std-only, one thread per accepted request).
//!
//! Per connection:
//! - a **reader thread** owns the [`IncrementalDecoder`], validates semantic
//!   frame fields and registers in-flight requests;
//! - a **writer thread** owns all writes so frames are never interleaved;
//! - a **worker thread per accepted request** (bounded by `max_inflight`)
//!   executes the test service and pushes exactly one terminal frame.
//!
//! Fatal framing errors (bad magic/version, oversize length, CRC mismatch,
//! mid-frame EOF) close the connection; recoverable protocol errors get an
//! ERROR frame and the connection continues.

use std::collections::HashMap;
use std::io;
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use crate::error::{decode_error_payload, encode_error_payload, ErrorCode};
use crate::frame::{
    encode_frame, flags, Command, Frame, IncrementalDecoder, HEADER_LEN, TRAILER_LEN,
};
use crate::service::{ServiceRequest, ServiceResponse};
use crate::sync::{bounded_channel, BoundedSender, Semaphore, SemaphorePermit, Token};

const READ_CHUNK: usize = 16 * 1024;
/// SLOW requests sleep in slices so cancel/kill wakes them promptly.
const SLEEP_SLICE: Duration = Duration::from_millis(20);

#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub bind: std::net::SocketAddr,
    pub max_payload: u32,
    pub max_inflight: usize,
    pub writer_queue_capacity: usize,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            bind: "127.0.0.1:0".parse().unwrap(),
            max_payload: crate::frame::DEFAULT_MAX_PAYLOAD,
            max_inflight: 64,
            writer_queue_capacity: 128,
        }
    }
}

/// Handle to a running server. [`RunningServer::shutdown`] stops accepting and
/// kills every connection.
pub struct RunningServer {
    pub local_addr: std::net::SocketAddr,
    kill: Token,
    accept_thread: Option<JoinHandle<()>>,
    conns: Arc<Mutex<HashMap<usize, Token>>>,
    listener: Option<TcpListener>,
}

impl RunningServer {
    pub fn local_addr(&self) -> std::net::SocketAddr {
        self.local_addr
    }

    /// Stop the accept loop and cancel all connections. Underlying threads
    /// finish on their own (read timeout / cancel slices / queue drain).
    pub fn shutdown(mut self) {
        self.kill.cancel();
        if let Some(l) = self.listener.take() {
            drop(l);
        }
        let conns: Vec<Token> = self.conns.lock().unwrap().drain().map(|(_, t)| t).collect();
        for t in conns {
            t.cancel();
        }
        if let Some(h) = self.accept_thread.take() {
            let _ = h.join();
        }
    }
}

/// Start the server and begin accepting.
pub fn start(config: ServerConfig) -> io::Result<RunningServer> {
    let listener = TcpListener::bind(config.bind)?;
    listener.set_nonblocking(true)?;
    let local_addr = listener.local_addr()?;

    let kill = Token::new();
    let conns: Arc<Mutex<HashMap<usize, Token>>> = Arc::new(Mutex::new(HashMap::new()));
    let conn_id = Arc::new(AtomicUsize::new(0));

    let accept_kill = kill.clone();
    let accept_conns = conns.clone();
    let accept_id = conn_id.clone();
    let accept_cfg = config.clone();

    let accept_thread = thread::spawn(move || {
        accept_loop(listener, accept_cfg, accept_kill, accept_conns, accept_id);
    });

    // The accept thread owns the listener; the handle keeps a duplicate only
    // so shutdown can unblock an OS-level accept on platforms that need it
    // (on Linux we poll non-blocking, but try_clone is cheap insurance).
    Ok(RunningServer {
        local_addr,
        kill,
        accept_thread: Some(accept_thread),
        conns,
        listener: None,
    })
}

fn accept_loop(
    listener: TcpListener,
    config: ServerConfig,
    kill: Token,
    conns: Arc<Mutex<HashMap<usize, Token>>>,
    conn_id: Arc<AtomicUsize>,
) {
    while !kill.is_cancelled() {
        match listener.accept() {
            Ok((stream, peer)) => {
                stream.set_nodelay(true).ok();
                let id = conn_id.fetch_add(1, Ordering::SeqCst);
                let conn_kill = Token::new();
                conns.lock().unwrap().insert(id, conn_kill.clone());
                let cfg = config.clone();
                let conns2 = conns.clone();
                thread::spawn(move || {
                    if let Err(e) = run_connection(stream, cfg, conn_kill.clone()) {
                        eprintln!("[server] connection {id} ({peer}) ended: {e}");
                    }
                    conns2.lock().unwrap().remove(&id);
                });
            }
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(5));
            }
            Err(ref e)
                if e.kind() == io::ErrorKind::ConnectionAborted
                    || e.kind() == io::ErrorKind::ConnectionReset =>
            {
                // RST during accept handshake; keep serving.
                continue;
            }
            Err(_e) => {
                if kill.is_cancelled() {
                    break;
                }
                thread::sleep(Duration::from_millis(5));
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Per-connection machinery
// ---------------------------------------------------------------------------

struct Conn {
    config: ServerConfig,
    /// Request id -> cancel token for the executing worker.
    inflight: Mutex<HashMap<u32, Token>>,
    inflight_slots: Arc<Semaphore>,
    writer_tx: BoundedSender<Vec<u8>>,
    alive: AtomicBool,
    kill: Token,
}

fn run_connection(stream: TcpStream, config: ServerConfig, kill: Token) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_millis(100)))?;
    let write_stream = stream.try_clone()?;

    let (writer_tx, writer_rx) = bounded_channel::<Vec<u8>>(config.writer_queue_capacity);
    let conn = Arc::new(Conn {
        config: config.clone(),
        inflight: Mutex::new(HashMap::new()),
        inflight_slots: Arc::new(Semaphore::new(config.max_inflight)),
        writer_tx,
        alive: AtomicBool::new(true),
        kill,
    });

    // Writer thread: single owner of all socket writes.
    let writer_kill = conn.kill.clone();
    let writer = thread::spawn(move || writer_loop(write_stream, writer_rx, writer_kill));

    let reader_result = reader_loop(stream, &conn);

    // Reader exited: tear the connection down. Cancel every in-flight worker
    // so blocked SLOW jobs release their slots/threads promptly.
    conn.alive.store(false, Ordering::SeqCst);
    let tokens: Vec<Token> = conn.inflight.lock().unwrap().values().cloned().collect();
    for t in tokens {
        t.cancel();
    }
    // Dropping the conn's sender allows the writer to finish once workers
    // (which hold their own clones) drain.
    let _ = reader_result;
    drop(conn);
    let _ = writer.join();
    Ok(())
}

fn reader_loop(mut stream: TcpStream, conn: &Arc<Conn>) -> io::Result<()> {
    use std::io::Read;
    let mut decoder = IncrementalDecoder::new(conn.config.max_payload);
    let mut chunk = vec![0u8; READ_CHUNK];
    loop {
        if conn.kill.is_cancelled() {
            return Ok(());
        }
        let n = match stream.read(&mut chunk) {
            Ok(0) => {
                if let Err(e) = decoder.finish() {
                    eprintln!("[server] peer closed mid-frame: {e}");
                }
                return Ok(());
            }
            Ok(n) => n,
            Err(ref e)
                if e.kind() == io::ErrorKind::WouldBlock
                    || e.kind() == io::ErrorKind::TimedOut
                    || e.kind() == io::ErrorKind::Interrupted =>
            {
                continue
            }
            Err(e) => return Err(e),
        };
        if let Err(e) = decoder.feed(&chunk[..n]) {
            eprintln!("[server] fatal framing on feed: {e}");
            return Ok(());
        }
        loop {
            match decoder.next_frame() {
                Ok(frame) => handle_frame(conn, &frame),
                Err(crate::error::ParseError::NeedMore) => break,
                Err(fatal) => {
                    eprintln!("[server] fatal framing error, closing: {fatal}");
                    return Ok(());
                }
            }
        }
    }
}

fn handle_frame(conn: &Arc<Conn>, frame: &Frame) {
    // Reserved flag bits: recoverable per-frame error, framing remains valid.
    if frame.flags & !flags::KNOWN_MASK != 0 {
        send_error(
            conn,
            frame.request_id,
            ErrorCode::UnknownFlag,
            format!("reserved flag bits set: 0x{:02x}", frame.flags),
        );
        return;
    }

    match frame.command {
        Command::Request => handle_request(conn, frame),
        Command::Cancel => handle_cancel(conn, frame),
        Command::Ping => send_frame(
            conn,
            &Frame::new(Command::Pong, frame.request_id, frame.payload.clone()),
        ),
        // Server->client only commands arriving on the server channel.
        Command::Response | Command::Error | Command::Pong => send_error(
            conn,
            frame.request_id,
            ErrorCode::InvalidDirection,
            format!("{:?} frame is not valid client->server", frame.command),
        ),
        Command::Unknown(byte) => send_error(
            conn,
            frame.request_id,
            ErrorCode::UnknownCommand,
            format!("unknown command byte {byte}"),
        ),
    }
}

fn handle_request(conn: &Arc<Conn>, frame: &Frame) {
    let id = frame.request_id;

    // Duplicate in-flight id is a protocol error; the request is not executed.
    {
        let inflight = conn.inflight.lock().unwrap();
        if inflight.contains_key(&id) {
            drop(inflight);
            send_error(
                conn,
                id,
                ErrorCode::DuplicateRequest,
                format!("request {id} is already in flight"),
            );
            return;
        }
    }

    let service_req = match ServiceRequest::decode(&frame.payload) {
        Ok(req) => req,
        Err(msg) => {
            send_error(conn, id, ErrorCode::AppError, msg);
            return;
        }
    };

    // Bound live worker threads / memory: reject immediately when saturated.
    let permit = match conn.inflight_slots.try_acquire() {
        Some(p) => p,
        None => {
            send_error(
                conn,
                id,
                ErrorCode::ServerBusy,
                format!("{} in-flight requests already", conn.config.max_inflight),
            );
            return;
        }
    };

    let token = Token::new();
    conn.inflight.lock().unwrap().insert(id, token.clone());
    spawn_worker(conn.clone(), id, service_req, token, permit);
}

fn handle_cancel(conn: &Arc<Conn>, frame: &Frame) {
    let id = frame.request_id;
    let token = conn.inflight.lock().unwrap().get(&id).cloned();
    match token {
        // Signal the worker; it emits the single terminal frame. Nothing is
        // sent for the CANCEL itself (no ack channel exists in v1).
        Some(t) => t.cancel(),
        None => send_error(
            conn,
            id,
            ErrorCode::NoSuchRequest,
            format!("cannot cancel: request {id} is not in flight"),
        ),
    }
}

fn spawn_worker(
    conn: Arc<Conn>,
    id: u32,
    req: ServiceRequest,
    cancel: Token,
    permit: SemaphorePermit,
) {
    thread::spawn(move || {
        let outcome = execute(&req, &cancel, &conn);
        // Remove the registration exactly once, here, before sending the
        // terminal frame: this makes the "response vs cancel" race a simple
        // boolean check rather than a map race.
        conn.inflight.lock().unwrap().remove(&id);

        match outcome {
            WorkerOutcome::Respond(resp) => {
                let frame = Frame::new(Command::Response, id, resp.encode());
                send_frame(&conn, &frame);
            }
            WorkerOutcome::Canceled => {
                send_error(&conn, id, ErrorCode::Cancelled, "request cancelled");
            }
            WorkerOutcome::Dead => {
                // Connection is gone; do not touch the writer.
            }
        }
        drop(permit);
    });
}

enum WorkerOutcome {
    Respond(ServiceResponse),
    Canceled,
    Dead,
}

fn execute(req: &ServiceRequest, cancel: &Token, conn: &Conn) -> WorkerOutcome {
    match req {
        ServiceRequest::Echo(body) => WorkerOutcome::Respond(ServiceResponse::ok(body.clone())),
        ServiceRequest::Upper(body) => {
            let upper: Vec<u8> = body.iter().map(|b| b.to_ascii_uppercase()).collect();
            WorkerOutcome::Respond(ServiceResponse::ok(upper))
        }
        ServiceRequest::Add(a, b) => {
            let sum = a.wrapping_add(*b);
            WorkerOutcome::Respond(ServiceResponse::ok(sum.to_be_bytes().to_vec()))
        }
        ServiceRequest::Fail => {
            WorkerOutcome::Respond(ServiceResponse::app_error("service forced failure"))
        }
        ServiceRequest::Slow { delay_ms, body } => {
            let total = Duration::from_millis(*delay_ms as u64);
            let mut waited = Duration::ZERO;
            while waited < total {
                if cancel.is_cancelled() {
                    return WorkerOutcome::Canceled;
                }
                if !conn.alive.load(Ordering::SeqCst) {
                    return WorkerOutcome::Dead;
                }
                let step = SLEEP_SLICE.min(total - waited);
                // wait_timeout on the cancel token (returns early on cancel);
                // connection death is re-checked via alive on the next slice.
                cancel.sleep_or_cancelled(step);
                waited += step;
            }
            if cancel.is_cancelled() {
                WorkerOutcome::Canceled
            } else if !conn.alive.load(Ordering::SeqCst) {
                WorkerOutcome::Dead
            } else {
                WorkerOutcome::Respond(ServiceResponse::ok(body.clone()))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Frame emission (all paths funnel into the single writer thread)
// ---------------------------------------------------------------------------

fn send_frame(conn: &Conn, frame: &Frame) {
    if !conn.alive.load(Ordering::SeqCst) {
        return;
    }
    let mut buf = Vec::with_capacity(HEADER_LEN + frame.payload.len() + TRAILER_LEN);
    if encode_frame(frame, conn.config.max_payload, &mut buf).is_err() {
        eprintln!(
            "[server] refusing to encode frame larger than {} bytes",
            conn.config.max_payload
        );
        return;
    }
    // Blocking push provides backpressure: workers slow down when the peer
    // cannot read, and the bounded inflight semaphore then returns SERVER_BUSY.
    if conn.writer_tx.send(buf).is_err() {
        conn.alive.store(false, Ordering::SeqCst);
    }
}

fn send_error(conn: &Conn, request_id: u32, code: ErrorCode, message: impl Into<String>) {
    let message = message.into();
    let payload = encode_error_payload(code, &message);
    send_frame(conn, &Frame::new(Command::Error, request_id, payload));
}

fn writer_loop(mut stream: TcpStream, rx: crate::sync::BoundedReceiver<Vec<u8>>, kill: Token) {
    use std::io::Write;
    while let Some(buf) = rx.recv() {
        if kill.is_cancelled() {
            return;
        }
        if stream.write_all(&buf).is_err() {
            return;
        }
    }
}

/// Best-effort human-readable summary of an ERROR frame payload (used by the
/// demo client and tests).
pub fn describe_error_frame(frame: &Frame) -> String {
    let (code, message) = decode_error_payload(&frame.payload);
    match code {
        Ok(c) => format!("{c:?}: {message}"),
        Err(raw) => format!("error code {raw}: {message}"),
    }
}
