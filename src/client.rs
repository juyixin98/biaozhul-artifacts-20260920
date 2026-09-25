//! Multiplexing RPC client (std-only).
//!
//! Two background threads per connection:
//! - a **reader thread** owns the [`IncrementalDecoder`] and demultiplexes
//!   frames to per-request one-shot slots by `request_id`;
//! - a **writer thread** owns all socket writes.
//!
//! Concurrency guarantees implemented here:
//! - Many requests may be outstanding at once (`max_inflight` bounds them).
//! - Responses may arrive in **any order**; each is routed by request id.
//! - Request ids are allocated monotonically and are **never reused while the
//!   connection lives** (and cannot wrap: allocation past `u32::MAX - 1`
//!   fails), so a slow peer can never attribute a late response to the wrong
//!   request — it is recorded as late instead.
//! - Timeout sends a best-effort CANCEL and locally retires the request; the
//!   server's eventual answer is then recognisable as a **late response**.

use std::collections::HashMap;
use std::io;
use std::net::{SocketAddr, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use crate::error::{decode_error_payload, encode_error_payload, ErrorCode, RpcError};
use crate::frame::{encode_frame, Command, Frame, IncrementalDecoder, HEADER_LEN, TRAILER_LEN};
use crate::sync::{
    bounded_channel, BoundedReceiver, BoundedSender, Semaphore, SemaphorePermit, Token,
};

const READ_CHUNK: usize = 16 * 1024;
/// Late-response history kept per connection (bounded memory).
const LATE_HISTORY: usize = 256;
/// Id 0 is reserved (never allocated).
const FIRST_REQUEST_ID: u32 = 1;

#[derive(Debug, Clone)]
pub struct ClientConfig {
    pub max_payload: u32,
    pub max_inflight: usize,
    pub writer_queue_capacity: usize,
}

impl Default for ClientConfig {
    fn default() -> Self {
        ClientConfig {
            max_payload: crate::frame::DEFAULT_MAX_PAYLOAD,
            max_inflight: 64,
            writer_queue_capacity: 128,
        }
    }
}

/// A response frame that arrived after its request had already been retired
/// (timeout/cancel/duplicate), or for an id the client never issued.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LateReport {
    pub request_id: u32,
    pub command: String,
    pub note: String,
}

struct Inner {
    config: ClientConfig,
    next_id: AtomicU32,
    pending: Mutex<HashMap<u32, BoundedSender<Frame>>>,
    slots: Arc<Semaphore>,
    writer_tx: BoundedSender<Vec<u8>>,
    alive: AtomicBool,
    shutdown: Token,
    late: Mutex<Vec<LateReport>>,
    late_dropped: AtomicUsize,
}

/// A multiplexed RPC connection. Cheap to clone? No: it owns background
/// threads; drop it (or call [`Connection::close`]) to tear them down.
pub struct Connection {
    inner: Arc<Inner>,
}

/// One outstanding RPC. Dropping a `Call` locally abandons it (its slot is
/// freed); a best-effort CANCEL is sent so the server may stop its work.
pub struct Call {
    id: u32,
    rx: BoundedReceiver<Frame>,
    inner: Arc<Inner>,
    _permit: SemaphorePermit,
    finished: bool,
}

impl std::fmt::Debug for Call {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Call")
            .field("id", &self.id)
            .field("finished", &self.finished)
            .finish()
    }
}

impl Connection {
    pub fn connect(addr: SocketAddr, config: ClientConfig) -> io::Result<Connection> {
        Self::connect_with_stream(TcpStream::connect(addr)?, config)
    }

    /// Build a connection over an already-connected stream (used by tests
    /// driving scripted peers).
    pub fn connect_with_stream(stream: TcpStream, config: ClientConfig) -> io::Result<Connection> {
        stream.set_nodelay(true)?;
        stream.set_read_timeout(Some(Duration::from_millis(100)))?;
        let write_stream = stream.try_clone()?;

        let (writer_tx, writer_rx) = bounded_channel::<Vec<u8>>(config.writer_queue_capacity);
        let inner = Arc::new(Inner {
            config: config.clone(),
            next_id: AtomicU32::new(FIRST_REQUEST_ID),
            pending: Mutex::new(HashMap::new()),
            slots: Arc::new(Semaphore::new(config.max_inflight)),
            writer_tx,
            alive: AtomicBool::new(true),
            shutdown: Token::new(),
            late: Mutex::new(Vec::new()),
            late_dropped: AtomicUsize::new(0),
        });

        let reader_inner = inner.clone();
        thread::spawn(move || reader_loop(stream, reader_inner));
        let writer_shutdown = inner.shutdown.clone();
        thread::spawn(move || writer_loop(write_stream, writer_rx, writer_shutdown));

        Ok(Connection { inner })
    }

    pub fn is_alive(&self) -> bool {
        self.inner.alive.load(Ordering::SeqCst)
    }

    /// Drain and return late/unsolicited responses recorded since the last
    /// call. Bounded to the most recent [`LATE_HISTORY`].
    pub fn take_late(&self) -> Vec<LateReport> {
        std::mem::take(&mut *self.inner.late.lock().unwrap())
    }

    /// Number of late responses discarded because the history buffer was full.
    pub fn late_dropped_count(&self) -> usize {
        self.inner.late_dropped.load(Ordering::SeqCst)
    }

    /// Start a request: allocate an id, register the response slot and send
    /// the frame. Returns a [`Call`] used to wait, time out or cancel.
    pub fn call(&self, payload: Vec<u8>) -> Result<Call, RpcError> {
        let inner = &self.inner;
        if !inner.alive.load(Ordering::SeqCst) {
            return Err(RpcError::new(
                ErrorCode::ConnectionClosed,
                "connection is closed",
            ));
        }
        let permit = match inner.slots.try_acquire() {
            Some(p) => p,
            None => {
                return Err(RpcError::new(
                    ErrorCode::TooManyInflight,
                    format!("{} requests already in flight", inner.config.max_inflight),
                ))
            }
        };
        let id = inner.next_id.fetch_add(1, Ordering::SeqCst);
        if id == u32::MAX {
            // Monotonic space exhausted (only after ~4.3e9 requests): refuse
            // rather than wrap and risk id reuse.
            return Err(RpcError::new(
                ErrorCode::TooManyInflight,
                "request id space exhausted",
            ));
        }

        let (tx, rx) = bounded_channel::<Frame>(1);
        inner.pending.lock().unwrap().insert(id, tx);

        let frame = Frame::new(Command::Request, id, payload);
        if let Err(e) = self.write_frame(frame) {
            inner.pending.lock().unwrap().remove(&id);
            return Err(e);
        }
        Ok(Call {
            id,
            rx,
            inner: self.inner.clone(),
            _permit: permit,
            finished: false,
        })
    }

    /// Convenience: send, wait up to `timeout`, return raw RESPONSE payload.
    pub fn call_wait(&self, payload: Vec<u8>, timeout: Duration) -> Result<Vec<u8>, RpcError> {
        let call = self.call(payload)?;
        call.wait_timeout(timeout)
    }

    /// Send a PING and wait for its PONG. A ping occupies one in-flight slot
    /// (the pending table is therefore bounded by `max_inflight`).
    pub fn ping(&self, timeout: Duration) -> Result<(), RpcError> {
        let inner = &self.inner;
        if !inner.alive.load(Ordering::SeqCst) {
            return Err(RpcError::new(
                ErrorCode::ConnectionClosed,
                "connection is closed",
            ));
        }
        let permit = match inner.slots.try_acquire() {
            Some(p) => p,
            None => {
                return Err(RpcError::new(
                    ErrorCode::TooManyInflight,
                    format!("{} requests already in flight", inner.config.max_inflight),
                ))
            }
        };
        let id = inner.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = bounded_channel::<Frame>(1);
        inner.pending.lock().unwrap().insert(id, tx);
        if let Err(e) = self.write_frame(Frame::new(Command::Ping, id, Vec::new())) {
            inner.pending.lock().unwrap().remove(&id);
            return Err(e);
        }
        let result = match rx.recv_timeout(timeout) {
            crate::sync::RecvTimeout::Item(f) if f.command == Command::Pong => Ok(()),
            crate::sync::RecvTimeout::Item(f) => Err(frame_to_error(&f, "expected PONG")),
            crate::sync::RecvTimeout::Closed => Err(RpcError::new(
                ErrorCode::ConnectionClosed,
                "connection closed while waiting for PONG",
            )),
            crate::sync::RecvTimeout::TimedOut => {
                Err(RpcError::new(ErrorCode::Timeout, "ping timed out"))
            }
        };
        inner.pending.lock().unwrap().remove(&id);
        drop(permit);
        result
    }

    fn write_frame(&self, frame: Frame) -> Result<(), RpcError> {
        let mut buf = Vec::with_capacity(HEADER_LEN + frame.payload.len() + TRAILER_LEN);
        encode_frame(&frame, self.inner.config.max_payload, &mut buf).map_err(
            |(declared, limit)| {
                RpcError::new(
                    ErrorCode::PayloadTooLarge,
                    format!("payload {declared} exceeds limit {limit}"),
                )
            },
        )?;
        self.inner
            .writer_tx
            .send(buf)
            .map_err(|_| RpcError::new(ErrorCode::ConnectionClosed, "writer shut down"))
    }

    /// Tear down: stop background threads. In-flight calls then fail with
    /// `ConnectionClosed`.
    pub fn close(&self) {
        self.inner.alive.store(false, Ordering::SeqCst);
        self.inner.shutdown.cancel();
        self.inner.writer_tx.shutdown();
        // Release every waiter.
        let waiters: Vec<_> = self
            .inner
            .pending
            .lock()
            .unwrap()
            .drain()
            .map(|(_, tx)| tx)
            .collect();
        drop(waiters);
    }
}

impl Drop for Connection {
    fn drop(&mut self) {
        self.close();
    }
}

impl Call {
    pub fn id(&self) -> u32 {
        self.id
    }

    /// Block until the terminal frame arrives or the connection breaks.
    pub fn wait(mut self) -> Result<Vec<u8>, RpcError> {
        let result = match self.rx.recv() {
            Some(f) => frame_to_result(&f),
            None => Err(RpcError::new(
                ErrorCode::ConnectionClosed,
                "connection closed before response arrived",
            )),
        };
        self.finished = true;
        result
    }

    /// Wait up to `timeout`. On timeout the request is retired locally and a
    /// CANCEL is sent; the eventual answer becomes a late response visible via
    /// [`Connection::take_late`].
    pub fn wait_timeout(mut self, timeout: Duration) -> Result<Vec<u8>, RpcError> {
        match self.rx.recv_timeout(timeout) {
            crate::sync::RecvTimeout::Item(f) => {
                self.finished = true;
                frame_to_result(&f)
            }
            crate::sync::RecvTimeout::Closed => {
                self.finished = true;
                Err(RpcError::new(
                    ErrorCode::ConnectionClosed,
                    "connection closed before response arrived",
                ))
            }
            crate::sync::RecvTimeout::TimedOut => {
                // Retire locally, best-effort notify server.
                self.retire();
                self.send_cancel();
                Err(RpcError::new(
                    ErrorCode::Timeout,
                    format!("request {} timed out after {:?}", self.id, timeout),
                ))
            }
        }
    }

    /// Explicitly cancel: send CANCEL, then keep waiting — the worker may
    /// already have finished, in which case the real response wins the race.
    pub fn cancel(mut self) -> Result<Vec<u8>, RpcError> {
        self.send_cancel();
        match self.rx.recv() {
            Some(f) => {
                self.finished = true;
                frame_to_result(&f)
            }
            None => {
                self.finished = true;
                Err(RpcError::new(
                    ErrorCode::ConnectionClosed,
                    "connection closed while cancelling",
                ))
            }
        }
    }

    fn retire(&mut self) {
        self.finished = true;
        self.inner.pending.lock().unwrap().remove(&self.id);
    }

    fn send_cancel(&self) {
        let frame = Frame::new(Command::Cancel, self.id, Vec::new());
        let mut buf = Vec::with_capacity(HEADER_LEN + TRAILER_LEN);
        if encode_frame(&frame, self.inner.config.max_payload, &mut buf).is_ok() {
            // Failure to deliver just means the server will answer into a
            // retired slot; the reader records it as late.
            let _ = self.inner.writer_tx.try_send(buf);
        }
    }
}

impl Drop for Call {
    fn drop(&mut self) {
        if !self.finished {
            // Abandoned by the caller without a terminal frame: retire and
            // give the server a chance to stop doing the work.
            self.inner.pending.lock().unwrap().remove(&self.id);
            self.send_cancel();
        }
    }
}

fn frame_to_result(frame: &Frame) -> Result<Vec<u8>, RpcError> {
    match frame.command {
        Command::Response => Ok(frame.payload.clone()),
        Command::Error => {
            let (code, message) = decode_error_payload(&frame.payload);
            Err(match code {
                Ok(c) => RpcError::new(c, message),
                Err(raw) => RpcError::new(
                    ErrorCode::UnknownCommand,
                    format!("unknown error code {raw}: {message}"),
                ),
            })
        }
        other => Err(RpcError::new(
            ErrorCode::InvalidDirection,
            format!("expected RESPONSE/ERROR, got {other:?}"),
        )),
    }
}

fn frame_to_error(frame: &Frame, ctx: &str) -> RpcError {
    let mut err = frame_to_result(frame)
        .err()
        .unwrap_or_else(|| RpcError::new(ErrorCode::InvalidDirection, ctx.to_string()));
    if err.message.is_empty() {
        err.message = ctx.to_string();
    }
    err
}

// ---------------------------------------------------------------------------
// Background threads
// ---------------------------------------------------------------------------

fn reader_loop(mut stream: TcpStream, inner: Arc<Inner>) {
    use std::io::Read;
    let mut decoder = IncrementalDecoder::new(inner.config.max_payload);
    let mut chunk = vec![0u8; READ_CHUNK];
    loop {
        if inner.shutdown.is_cancelled() {
            break;
        }
        let n = match stream.read(&mut chunk) {
            Ok(0) => {
                if let Err(e) = decoder.finish() {
                    eprintln!("[client] server closed mid-frame: {e}");
                }
                break;
            }
            Ok(n) => n,
            Err(ref e)
                if e.kind() == io::ErrorKind::WouldBlock
                    || e.kind() == io::ErrorKind::Interrupted
                    || e.kind() == io::ErrorKind::TimedOut =>
            {
                continue
            }
            Err(e) => {
                eprintln!("[client] read error: {e}");
                break;
            }
        };
        if let Err(e) = decoder.feed(&chunk[..n]) {
            eprintln!("[client] fatal framing on feed: {e}");
            break;
        }
        loop {
            match decoder.next_frame() {
                Ok(frame) => dispatch(&inner, frame),
                Err(crate::error::ParseError::NeedMore) => break,
                Err(fatal) => {
                    eprintln!("[client] fatal framing error, closing: {fatal}");
                    // Drain all waiters once; closed below.
                    let waiters: Vec<_> = inner.pending.lock().unwrap().drain().collect();
                    drop(waiters);
                    inner.alive.store(false, Ordering::SeqCst);
                    inner.shutdown.cancel();
                    inner.writer_tx.shutdown();
                    return;
                }
            }
        }
    }
    // Clean close: fail every outstanding call.
    inner.alive.store(false, Ordering::SeqCst);
    inner.writer_tx.shutdown();
    let waiters: Vec<_> = inner
        .pending
        .lock()
        .unwrap()
        .drain()
        .map(|(_, t)| t)
        .collect();
    drop(waiters); // dropping the oneshot senders makes recv() return None
}

fn dispatch(inner: &Arc<Inner>, frame: Frame) {
    // Reserved flag bits on an otherwise valid frame are tolerated (v1
    // defines REQ_ACK and future versions may add more); the payload is still
    // routed. Structural protocol mistakes get an ERROR reply.
    match frame.command {
        Command::Response | Command::Error | Command::Pong => route(inner, frame),
        Command::Ping => {
            // Servers normally never ping, but answer symmetrically.
            reply_frame(
                inner,
                Frame::new(Command::Pong, frame.request_id, frame.payload.clone()),
            );
        }
        Command::Request | Command::Cancel => {
            send_client_error(
                inner,
                frame.request_id,
                ErrorCode::InvalidDirection,
                &format!("{:?} frame is not valid server->client", frame.command),
            );
        }
        Command::Unknown(byte) => {
            send_client_error(
                inner,
                frame.request_id,
                ErrorCode::UnknownCommand,
                &format!("unknown command byte {byte}"),
            );
        }
    }
}

fn route(inner: &Arc<Inner>, frame: Frame) {
    let id = frame.request_id;
    // Remove the slot before delivering: the first terminal frame completes
    // the call; any duplicate/second frame is recognisably late.
    let tx = inner.pending.lock().unwrap().remove(&id);
    match tx {
        Some(tx) => {
            if tx.try_send(frame).is_err() {
                note_late(inner, id, "RESPONSE", "slot closed concurrently");
            }
        }
        None => note_late(
            inner,
            id,
            "RESPONSE",
            "no outstanding request with this id (timeout/cancel/never issued)",
        ),
    }
}

fn note_late(inner: &Arc<Inner>, id: u32, command: &str, note: &str) {
    let mut late = inner.late.lock().unwrap();
    if late.len() >= LATE_HISTORY {
        late.remove(0);
        inner.late_dropped.fetch_add(1, Ordering::SeqCst);
    }
    late.push(LateReport {
        request_id: id,
        command: command.to_string(),
        note: note.to_string(),
    });
}

fn reply_frame(inner: &Arc<Inner>, frame: Frame) {
    let mut buf = Vec::with_capacity(HEADER_LEN + frame.payload.len() + TRAILER_LEN);
    if encode_frame(&frame, inner.config.max_payload, &mut buf).is_ok() {
        let _ = inner.writer_tx.try_send(buf);
    }
}

fn send_client_error(inner: &Arc<Inner>, request_id: u32, code: ErrorCode, message: &str) {
    let payload = encode_error_payload(code, message);
    reply_frame(inner, Frame::new(Command::Error, request_id, payload));
}

fn writer_loop(mut stream: TcpStream, rx: BoundedReceiver<Vec<u8>>, shutdown: Token) {
    use std::io::Write;
    while let Some(buf) = rx.recv() {
        if shutdown.is_cancelled() {
            return;
        }
        if stream.write_all(&buf).is_err() {
            return;
        }
    }
}

/// Helper used by tests/demos: monotonically allocated ids start here.
pub fn first_request_id() -> u32 {
    FIRST_REQUEST_ID
}
