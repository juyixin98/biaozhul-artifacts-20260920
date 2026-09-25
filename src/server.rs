//! Local TCP RPC server: demux + workers + per-conn cancellation.
//!
//! Per connection:
//! * a **reader task** runs the hand-written [`FrameDecoder`]; framing errors
//!   close the connection immediately (no application processing), while
//!   application errors are sent back as normal response frames;
//! * a bounded pool of **worker threads** executes handlers concurrently, so
//!   a slow request never blocks later ones (responses may overtake);
//! * a **writer task** is the sole owner of the sending half;
//! * an in-flight registry supports CANCEL and bounds per-connection memory
//!   to `max_inflight` requests.

use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use crate::decode::FrameDecoder;
use crate::frame::{Frame, FrameKind, DEFAULT_MAX_PAYLOAD};
use crate::payload::{decode_method, encode_err, encode_ok, read_i64, read_u32, AppCode, Method};

#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub max_payload: usize,
    /// Max outstanding requests allowed on one connection.
    pub max_inflight: usize,
    /// Bounded outbound queue per connection.
    pub write_queue_depth: usize,
    pub read_timeout: Option<Duration>,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            max_payload: DEFAULT_MAX_PAYLOAD,
            max_inflight: 128,
            write_queue_depth: 1024,
            read_timeout: Some(Duration::from_millis(200)),
        }
    }
}

/// Process-wide counters (shared across connections).
#[derive(Debug, Default)]
pub struct Stats {
    pub requests: AtomicUsize,
    pub responses_ok: AtomicUsize,
    pub responses_app_err: AtomicUsize,
    pub cancelled: AtomicUsize,
    pub rejected_inflight: AtomicUsize,
    pub framing_errors: AtomicUsize,
    pub unknown_cancel: AtomicUsize,
}

impl Stats {
    pub fn snapshot(&self) -> StatsSnapshot {
        StatsSnapshot {
            requests: self.requests.load(Ordering::Relaxed),
            responses_ok: self.responses_ok.load(Ordering::Relaxed),
            responses_app_err: self.responses_app_err.load(Ordering::Relaxed),
            cancelled: self.cancelled.load(Ordering::Relaxed),
            rejected_inflight: self.rejected_inflight.load(Ordering::Relaxed),
            framing_errors: self.framing_errors.load(Ordering::Relaxed),
            unknown_cancel: self.unknown_cancel.load(Ordering::Relaxed),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StatsSnapshot {
    pub requests: usize,
    pub responses_ok: usize,
    pub responses_app_err: usize,
    pub cancelled: usize,
    pub rejected_inflight: usize,
    pub framing_errors: usize,
    pub unknown_cancel: usize,
}

impl StatsSnapshot {
    /// Big-endian wire encoding returned by the Stats method.
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(8 * 7);
        for v in [
            self.requests,
            self.responses_ok,
            self.responses_app_err,
            self.cancelled,
            self.rejected_inflight,
            self.framing_errors,
            self.unknown_cancel,
        ] {
            out.extend_from_slice(&(v as u64).to_be_bytes());
        }
        out
    }
}

/// Running server handle.
pub struct Server {
    local_addr: std::net::SocketAddr,
    stats: Arc<Stats>,
    shutdown: Arc<AtomicUsize>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Server {
    pub fn bind(addr: &str) -> std::io::Result<Server> {
        Self::bind_with(addr, ServerConfig::default())
    }

    pub fn bind_with(addr: &str, cfg: ServerConfig) -> std::io::Result<Server> {
        let listener = TcpListener::bind(addr)?;
        listener.set_nonblocking(true)?;
        let local_addr = listener.local_addr()?;
        let stats = Arc::new(Stats::default());
        let shutdown = Arc::new(AtomicUsize::new(0));

        let thread = {
            let stats = Arc::clone(&stats);
            let shutdown = Arc::clone(&shutdown);
            thread::spawn(move || run_accept(listener, cfg, stats, shutdown))
        };

        Ok(Server {
            local_addr,
            stats,
            shutdown,
            thread: Some(thread),
        })
    }

    pub fn local_addr(&self) -> std::net::SocketAddr {
        self.local_addr
    }

    pub fn stats(&self) -> StatsSnapshot {
        self.stats.snapshot()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.shutdown.store(1, Ordering::Relaxed);
        if let Some(h) = self.thread.take() {
            let _ = h.join();
        }
    }
}

fn run_accept(
    listener: TcpListener,
    cfg: ServerConfig,
    stats: Arc<Stats>,
    shutdown: Arc<AtomicUsize>,
) {
    while shutdown.load(Ordering::Relaxed) == 0 {
        match listener.accept() {
            Ok((stream, peer)) => {
                stream.set_nodelay(true).ok();
                if let Some(t) = cfg.read_timeout {
                    stream.set_read_timeout(Some(t)).ok();
                }
                let cfg = cfg.clone();
                let stats = Arc::clone(&stats);
                let shutdown = Arc::clone(&shutdown);
                thread::spawn(move || {
                    serve_connection(stream, peer, cfg, stats, shutdown);
                });
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(20));
            }
            Err(_) => break,
        }
    }
}

// per-connection state ----------------------------------------------------------------

struct ConnState {
    /// request ids with a worker currently running (registered before spawn).
    inflight: Mutex<HashSet<u64>>,
    /// Per-id cancellation flag checked cooperatively by slow handlers.
    cancel: Mutex<HashMap<u64, Arc<AtomicUsize>>>,
    inflight_count: usize,
    alive: AtomicUsize,
}

enum OutMsg {
    Frame(Frame),
    /// Writer exits and closes its half.
    Close,
}

struct Job {
    id: u64,
    payload: Vec<u8>,
    out_tx: SyncSender<OutMsg>,
    conn: Arc<ConnState>,
    stats: Arc<Stats>,
    cancel_flag: Arc<AtomicUsize>,
}

#[allow(clippy::too_many_arguments)]
fn serve_connection(
    stream: TcpStream,
    _peer: std::net::SocketAddr,
    cfg: ServerConfig,
    stats: Arc<Stats>,
    shutdown: Arc<AtomicUsize>,
) {
    let write_stream = stream.try_clone().expect("clone stream");
    let (out_tx, out_rx) = mpsc::sync_channel::<OutMsg>(cfg.write_queue_depth.max(1));

    let writer = thread::spawn(move || run_writer(write_stream, out_rx));

    let conn = Arc::new(ConnState {
        inflight: Mutex::new(HashSet::new()),
        cancel: Mutex::new(HashMap::new()),
        inflight_count: cfg.max_inflight,
        alive: AtomicUsize::new(1),
    });

    let mut decoder = FrameDecoder::new(cfg.max_payload);
    let mut chunk = vec![0u8; 8192];
    let mut read_stream = stream;

    loop {
        if shutdown.load(Ordering::Relaxed) != 0 {
            break;
        }
        match read_stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                if let Err(e) = decoder.push(&chunk[..n]) {
                    // Fatal framing error: count and close immediately. No
                    // resynchronisation is possible on a length-prefixed
                    // stream; application handlers never see these bytes.
                    stats.framing_errors.fetch_add(1, Ordering::Relaxed);
                    let _ = e;
                    break;
                }
                loop {
                    match decoder.next_frame() {
                        Ok(Some(frame)) => {
                            if !dispatch_frame(&frame, &conn, &out_tx, &stats, &cfg) {
                                break;
                            }
                        }
                        Ok(None) => break,
                        Err(_fatal) => {
                            stats.framing_errors.fetch_add(1, Ordering::Relaxed);
                            conn.alive.store(0, Ordering::Relaxed);
                            let _ = out_tx.try_send(OutMsg::Close);
                            let _ = writer.join();
                            return;
                        }
                    }
                }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(_) => break,
        }
    }

    conn.alive.store(0, Ordering::Relaxed);
    let _ = out_tx.try_send(OutMsg::Close);
    let _ = writer.join();
}

/// Returns false to request the read loop to stop.
fn dispatch_frame(
    frame: &Frame,
    conn: &Arc<ConnState>,
    out_tx: &SyncSender<OutMsg>,
    stats: &Arc<Stats>,
    cfg: &ServerConfig,
) -> bool {
    match frame.kind {
        FrameKind::Request => {
            stats.requests.fetch_add(1, Ordering::Relaxed);

            // Bound per-connection in-flight work and therefore memory.
            let flag = Arc::new(AtomicUsize::new(0));
            {
                let mut inflight = conn.inflight.lock().unwrap();
                if inflight.len() >= conn.inflight_count {
                    stats.rejected_inflight.fetch_add(1, Ordering::Relaxed);
                    let resp = Frame::new(
                        FrameKind::Response,
                        frame.request_id,
                        encode_err(AppCode::TooManyInFlight, "server inflight cap"),
                    );
                    let _ = send_or_close(out_tx, resp);
                    return true;
                }
                inflight.insert(frame.request_id);
                conn.cancel
                    .lock()
                    .unwrap()
                    .insert(frame.request_id, Arc::clone(&flag));
            }

            let job = Job {
                id: frame.request_id,
                payload: frame.payload.clone(),
                out_tx: out_tx.clone(),
                conn: Arc::clone(conn),
                stats: Arc::clone(stats),
                cancel_flag: flag,
            };
            // Detached worker: concurrency is still bounded by max_inflight;
            // an over-eager spawn past the cap is prevented by the check above.
            let max_payload = cfg.max_payload;
            thread::spawn(move || run_job(job, max_payload));
            true
        }
        FrameKind::Cancel => {
            let exists = conn.inflight.lock().unwrap().contains(&frame.request_id);
            if exists {
                if let Some(flag) = conn.cancel.lock().unwrap().get(&frame.request_id) {
                    flag.store(1, Ordering::Relaxed);
                }
                // Response is emitted by the worker once it observes the flag
                // (or by the response that beat the cancel).
            } else {
                stats.unknown_cancel.fetch_add(1, Ordering::Relaxed);
                // Nothing to cancel: acknowledge with Cancelled anyway so a
                // racing client always unblocks.
                let resp = Frame::new(
                    FrameKind::Response,
                    frame.request_id,
                    encode_err(AppCode::Cancelled, "already done"),
                );
                let _ = send_or_close(out_tx, resp);
            }
            true
        }
        FrameKind::Response => {
            // Clients must not send responses: protocol violation → close.
            stats.framing_errors.fetch_add(1, Ordering::Relaxed);
            false
        }
    }
}

fn send_or_close(out_tx: &SyncSender<OutMsg>, frame: Frame) -> Result<(), ()> {
    out_tx.try_send(OutMsg::Frame(frame)).map_err(|_| ())
}

fn run_writer(mut stream: TcpStream, rx: Receiver<OutMsg>) {
    while let Ok(msg) = rx.recv() {
        match msg {
            OutMsg::Frame(frame) => {
                if stream.write_all(&frame.encode()).is_err() {
                    return;
                }
                let _ = stream.flush();
            }
            OutMsg::Close => {
                let _ = stream.flush();
                return;
            }
        }
    }
}

fn run_job(job: Job, _max_payload: usize) {
    let Job {
        id,
        payload,
        out_tx,
        conn,
        stats,
        cancel_flag,
    } = job;

    let response = handle(&payload, &cancel_flag, &stats, id);

    // Clean up registry before sending so a CANCEL arriving at this exact
    // point is answered "already done" rather than resurrecting state.
    let was_inflight = conn.inflight.lock().unwrap().remove(&id);
    conn.cancel.lock().unwrap().remove(&id);
    let _ = was_inflight;

    // If the connection is gone, stop: no point sending (and it proves worker
    // memory is reclaimed when the peer vanishes).
    if conn.alive.load(Ordering::Relaxed) == 0 {
        return;
    }

    let frame = Frame::new(FrameKind::Response, id, response);
    if out_tx.try_send(OutMsg::Frame(frame)).is_err() {
        conn.alive.store(0, Ordering::Relaxed);
    }
}

/// Application dispatch. Runs inside a worker thread.
fn handle(payload: &[u8], cancel: &AtomicUsize, stats: &Arc<Stats>, _id: u64) -> Vec<u8> {
    let method = match decode_method(payload) {
        Ok(m) => m,
        Err(code) => {
            stats.responses_app_err.fetch_add(1, Ordering::Relaxed);
            return encode_err(code, "bad method");
        }
    };
    let args = match crate::payload::request_args(payload) {
        Ok(a) => a,
        Err(code) => {
            stats.responses_app_err.fetch_add(1, Ordering::Relaxed);
            return encode_err(code, "short payload");
        }
    };

    match method {
        Method::Echo => {
            stats.responses_ok.fetch_add(1, Ordering::Relaxed);
            encode_ok(args)
        }
        Method::Add => {
            if args.len() < 16 {
                stats.responses_app_err.fetch_add(1, Ordering::Relaxed);
                return encode_err(AppCode::BadArgs, "add needs two i64");
            }
            let a = read_i64(args, 0).unwrap();
            let b = read_i64(args, 8).unwrap();
            stats.responses_ok.fetch_add(1, Ordering::Relaxed);
            encode_ok(&(a.wrapping_add(b)).to_be_bytes())
        }
        Method::Slow => {
            let delay_ms = read_u32(args, 0).unwrap_or(0) as u64;
            let echo = args.get(4..).unwrap_or(&[]).to_vec();
            // Cooperative, responsive cancellation: wake every 10ms.
            let mut remaining = delay_ms;
            while remaining > 0 {
                if cancel.load(Ordering::Relaxed) != 0 {
                    stats.cancelled.fetch_add(1, Ordering::Relaxed);
                    return encode_err(AppCode::Cancelled, "cancelled by client");
                }
                let step = remaining.min(10);
                thread::sleep(Duration::from_millis(step));
                remaining -= step;
            }
            if cancel.load(Ordering::Relaxed) != 0 {
                stats.cancelled.fetch_add(1, Ordering::Relaxed);
                return encode_err(AppCode::Cancelled, "cancelled by client");
            }
            stats.responses_ok.fetch_add(1, Ordering::Relaxed);
            encode_ok(&echo)
        }
        Method::Stats => {
            let snap = stats.snapshot();
            stats.responses_ok.fetch_add(1, Ordering::Relaxed);
            encode_ok(&snap.to_bytes())
        }
    }
}

// re-export payload helpers useful to binaries/tests
pub use crate::payload as app;

/// Parse one snapshot out of a Stats response body (test helper).
pub fn parse_stats_body(body: &[u8]) -> Option<StatsSnapshot> {
    if body.len() < 56 {
        return None;
    }
    let u = |at: usize| -> u64 {
        let mut a = [0u8; 8];
        a.copy_from_slice(&body[at..at + 8]);
        u64::from_be_bytes(a)
    };
    Some(StatsSnapshot {
        requests: u(0) as usize,
        responses_ok: u(8) as usize,
        responses_app_err: u(16) as usize,
        cancelled: u(24) as usize,
        rejected_inflight: u(32) as usize,
        framing_errors: u(40) as usize,
        unknown_cancel: u(48) as usize,
    })
}
