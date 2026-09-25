//! Multiplexing RPC client over a single TCP connection.
//!
//! Guarantees exercised by the acceptance tests:
//!
//! * **concurrency on one connection** — many outstanding requests share one
//!   socket; a writer thread serializes outbound frames, a reader thread
//!   demuxes responses by `request_id`.
//! * **IDs are never reused while outstanding** — IDs are `(slot, generation)`
//!   packed into a u64. A slot's generation is bumped every time it is
//!   released, so a late response carrying an old generation is distinguished
//!   from the new occupant and the full 64-bit id is never reissued.
//! * **out-of-order responses** — completion is keyed by full id; order does
//!   not matter.
//! * **cancellation races** — [`Call::cancel`] sends a CANCEL frame and then
//!   returns whichever frame wins. The application payload distinguishes a
//!   honoured cancel (`AppCode::Cancelled`) from a response that landed first.
//! * **late responses are identifiable** — a response whose id has no live
//!   owner (or whose generation is stale) is counted as late and dropped.
//! * **bounded memory** — at most `max_inflight` requests live at once;
//!   `start_request` applies backpressure instead of growing without bound.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{Shutdown, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use crate::decode::FrameDecoder;
use crate::frame::{Frame, FrameKind, DEFAULT_MAX_PAYLOAD};

/// Tunables for a multiplexed client connection.
#[derive(Debug, Clone)]
pub struct ClientConfig {
    /// Per-frame payload cap passed to the decoder (bounds receive memory).
    pub max_payload: usize,
    /// Max simultaneously outstanding requests (bounds inflight table memory).
    pub max_inflight: usize,
    /// Capacity of the outbound frame queue (bounds writer backlog).
    pub write_queue_depth: usize,
    pub read_timeout: Option<Duration>,
}

impl Default for ClientConfig {
    fn default() -> Self {
        ClientConfig {
            max_payload: DEFAULT_MAX_PAYLOAD,
            max_inflight: 256,
            write_queue_depth: 1024,
            read_timeout: Some(Duration::from_millis(200)),
        }
    }
}

/// Errors observed at the client transport/session level.
#[derive(Debug)]
pub enum ClientError {
    /// Connection I/O failure.
    Io(std::io::Error),
    /// `max_inflight` (or the outbound queue) is saturated; retry later.
    TooManyInFlight,
    /// Connection is closed/shut down; no new or waiting calls can proceed.
    Closed,
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClientError::Io(e) => write!(f, "io error: {e}"),
            ClientError::TooManyInFlight => write!(f, "too many in-flight requests"),
            ClientError::Closed => write!(f, "client connection closed"),
        }
    }
}
impl std::error::Error for ClientError {}

// Packed request id: high 32 bits generation, low 32 bits slot.
fn pack_id(slot: u32, generation: u32) -> u64 {
    ((generation as u64) << 32) | (slot as u64)
}
fn id_slot(id: u64) -> u32 {
    id as u32
}
fn id_gen(id: u64) -> u32 {
    (id >> 32) as u32
}

/// Split a packed id into `(slot, generation)` (sample/test helper).
pub fn id_parts(id: u64) -> (u32, u32) {
    (id_slot(id), id_gen(id))
}

/// Pack `(slot, generation)` back into an id (sample-generation helper).
pub fn pack(slot: u32, generation: u32) -> u64 {
    pack_id(slot, generation)
}

#[derive(Default)]
struct SlotState {
    /// generation currently handed out (0 = slot never used)
    generation: u32,
}

struct InflightEntry {
    tx: Sender<Result<Frame, ClientError>>,
}

enum WriteMsg {
    Frame(Frame),
    /// Flush and shut the socket down (drains reader too).
    Shutdown,
}

struct Slots {
    state: Vec<SlotState>,
    free: Vec<u32>,
    /// Number of currently allocated slots.
    used: usize,
}

impl Slots {
    fn alloc(&mut self) -> u64 {
        let slot = if let Some(s) = self.free.pop() {
            s
        } else {
            let s = self.state.len() as u32;
            self.state.push(SlotState::default());
            s
        };
        let entry = &mut self.state[slot as usize];
        // First allocation uses generation 1 so packed id 0 means "no id".
        if entry.generation == 0 {
            entry.generation = 1;
        }
        self.used += 1;
        pack_id(slot, entry.generation)
    }

    fn release(&mut self, id: u64) {
        let slot = id_slot(id);
        let entry = &mut self.state[slot as usize];
        // Bump generation: a late response for the old id is now stale and the
        // same full id is never handed out again.
        entry.generation = entry.generation.wrapping_add(1).max(1);
        self.free.push(slot);
        self.used -= 1;
    }

    fn is_current(&self, id: u64) -> bool {
        let slot = id_slot(id);
        matches!(
            self.state.get(slot as usize),
            Some(s) if s.generation == id_gen(id)
        )
    }
}

struct Inner {
    write_tx: SyncSender<WriteMsg>,
    slots: Mutex<Slots>,
    inflight: Mutex<HashMap<u64, InflightEntry>>,
    max_inflight: usize,
    late: AtomicUsize,
    closed: AtomicUsize,
}

/// Multiplexed client. Cheaply cloneable; clones share one connection.
///
/// The connection's sockets and worker threads are owned by the
/// [`Connection`] returned from [`MuxClient::connect`]; `MuxClient` is a
/// cheaply cloneable handle into that connection. Dropping the `Connection`
/// shuts sockets/threads down (outstanding [`Call`]s then fail with
/// [`ClientError::Closed`]).
#[derive(Clone)]
pub struct MuxClient {
    inner: Arc<Inner>,
}

/// Owned connection lifecycle. Dropping it closes the connection once other
/// clones/calls are also gone.
pub struct Connection {
    inner: Arc<Inner>,
    writer: Option<JoinHandle<()>>,
    reader: Option<JoinHandle<()>>,
}

impl MuxClient {
    pub fn connect<A: ToSocketAddrs>(addr: A) -> Result<Connection, ClientError> {
        Self::connect_with(addr, ClientConfig::default())
    }

    pub fn connect_with<A: ToSocketAddrs>(
        addr: A,
        cfg: ClientConfig,
    ) -> Result<Connection, ClientError> {
        let stream = TcpStream::connect(addr).map_err(ClientError::Io)?;
        stream.set_nodelay(true).map_err(ClientError::Io)?;
        if let Some(t) = cfg.read_timeout {
            stream.set_read_timeout(Some(t)).map_err(ClientError::Io)?;
        }

        let (write_tx, write_rx) = mpsc::sync_channel::<WriteMsg>(cfg.write_queue_depth.max(1));
        let write_stream = stream.try_clone().map_err(ClientError::Io)?;

        let inner = Arc::new(Inner {
            write_tx,
            slots: Mutex::new(Slots {
                state: Vec::new(),
                free: Vec::new(),
                used: 0,
            }),
            inflight: Mutex::new(HashMap::new()),
            max_inflight: cfg.max_inflight,
            late: AtomicUsize::new(0),
            closed: AtomicUsize::new(0),
        });

        // One owner of the sending half → frames never interleave on the wire.
        let writer = {
            let inner = Arc::clone(&inner);
            thread::spawn(move || run_writer(write_stream, write_rx, inner))
        };
        // Sole demuxer of inbound frames, keyed by request id.
        let reader = {
            let inner = Arc::clone(&inner);
            let max_payload = cfg.max_payload;
            thread::spawn(move || run_reader(stream, max_payload, inner))
        };

        Ok(Connection {
            inner,
            writer: Some(writer),
            reader: Some(reader),
        })
    }

    /// Responses received with no current owner (late or stale-generation).
    pub fn late_responses(&self) -> usize {
        self.inner.late.load(Ordering::Relaxed)
    }

    /// Currently outstanding requests.
    pub fn inflight_count(&self) -> usize {
        self.inner.slots.lock().unwrap().used
    }

    pub fn is_closed(&self) -> bool {
        self.inner.closed.load(Ordering::Relaxed) != 0
    }

    /// Begin a request. Applies backpressure at `max_inflight` and when the
    /// session is down.
    pub fn start_request(&self, payload: Vec<u8>) -> Result<Call, ClientError> {
        if self.inner.closed.load(Ordering::Relaxed) != 0 {
            return Err(ClientError::Closed);
        }
        let id = {
            let mut slots = self.inner.slots.lock().unwrap();
            if slots.used >= self.inner.max_inflight {
                return Err(ClientError::TooManyInFlight);
            }
            slots.alloc()
        };

        let (tx, rx) = mpsc::channel::<Result<Frame, ClientError>>();
        self.inner
            .inflight
            .lock()
            .unwrap()
            .insert(id, InflightEntry { tx });

        // Bounded outbound queue full (slow writer) → roll back and report
        // pressure rather than growing memory.
        if self
            .inner
            .write_tx
            .try_send(WriteMsg::Frame(Frame::new(FrameKind::Request, id, payload)))
            .is_err()
        {
            self.release_id(id);
            return Err(if self.inner.closed.load(Ordering::Relaxed) != 0 {
                ClientError::Closed
            } else {
                ClientError::TooManyInFlight
            });
        }

        Ok(Call {
            inner: Arc::clone(&self.inner),
            id,
            rx: Some(rx),
            done: false,
        })
    }

    fn release_id(&self, id: u64) {
        self.inner.inflight.lock().unwrap().remove(&id);
        self.inner.slots.lock().unwrap().release(id);
    }
}

impl Connection {
    /// A cheaply cloneable handle that shares this connection.
    pub fn client(&self) -> MuxClient {
        MuxClient {
            inner: Arc::clone(&self.inner),
        }
    }
}

impl Drop for Connection {
    fn drop(&mut self) {
        // Ask the writer to flush and shut the socket down; the reader sees
        // EOF (or its read timeout) and exits. Joining both bounds teardown.
        let _ = self.inner.write_tx.try_send(WriteMsg::Shutdown);
        if let Some(h) = self.writer.take() {
            let _ = h.join();
        }
        // Reader may be blocked up to its read timeout before noticing EOF.
        if let Some(h) = self.reader.take() {
            let _ = h.join();
        }
    }
}

/// A handle to one outstanding multiplexed request.
///
/// Dropping a `Call` releases its request slot with a fresh generation
/// (without sending CANCEL); a server response arriving afterwards is counted
/// as late. Use [`Call::cancel`] to actively ask the server to stop.
pub struct Call {
    inner: Arc<Inner>,
    id: u64,
    rx: Option<Receiver<Result<Frame, ClientError>>>,
    done: bool,
}

impl Call {
    /// The packed request id (slot + generation).
    pub fn id(&self) -> u64 {
        self.id
    }

    /// Block until the response frame arrives.
    pub fn wait(mut self) -> Result<Frame, ClientError> {
        let res = self
            .rx
            .as_ref()
            .unwrap()
            .recv()
            .map_err(|_| ClientError::Closed)?;
        self.finish();
        res
    }

    /// Wait up to `timeout`.
    ///
    /// A timeout does **not** cancel server work and does **not** release the
    /// id: the server may still be running, which is the precondition for a
    /// later late response. Poll again, [`cancel`](Self::cancel), or drop.
    pub fn wait_timeout(&self, timeout: Duration) -> Result<Frame, WaitError> {
        match self.rx.as_ref().unwrap().recv_timeout(timeout) {
            Ok(Ok(frame)) => Ok(frame),
            Ok(Err(e)) => Err(WaitError::Client(e)),
            Err(mpsc::RecvTimeoutError::Timeout) => Err(WaitError::Timeout),
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(WaitError::Closed),
        }
    }

    /// Send CANCEL and block for the terminating frame.
    ///
    /// Returns that frame: a well-behaved server sends `AppCode::Cancelled`
    /// when the cancel wins; if the response won the race it is returned
    /// unchanged, so the caller decides the outcome from the payload.
    pub fn cancel(mut self) -> Result<Frame, ClientError> {
        // Already here? Response beat the explicit cancel.
        if let Ok(ready) = self.rx.as_ref().unwrap().try_recv() {
            self.finish();
            return ready;
        }
        self.inner
            .write_tx
            .try_send(WriteMsg::Frame(Frame::new(
                FrameKind::Cancel,
                self.id,
                Vec::new(),
            )))
            .map_err(|_| ClientError::Closed)?;
        // Whichever terminating frame lands first resolves the race.
        let res = self
            .rx
            .as_ref()
            .unwrap()
            .recv()
            .map_err(|_| ClientError::Closed)?;
        self.finish();
        res
    }

    fn finish(&mut self) {
        if self.done {
            return;
        }
        self.done = true;
        self.rx.take();
        self.inner.inflight.lock().unwrap().remove(&self.id);
        self.inner.slots.lock().unwrap().release(self.id);
    }
}

impl Drop for Call {
    fn drop(&mut self) {
        self.finish();
    }
}

/// Error/result shape of [`Call::wait_timeout`].
#[derive(Debug)]
pub enum WaitError {
    Timeout,
    Closed,
    Client(ClientError),
}

// tasks --------------------------------------------------------------------------------

fn run_writer(mut stream: TcpStream, rx: Receiver<WriteMsg>, _inner: Arc<Inner>) {
    while let Ok(msg) = rx.recv() {
        match msg {
            WriteMsg::Frame(frame) => {
                if stream.write_all(&frame.encode()).is_err() {
                    return;
                }
                let _ = stream.flush();
            }
            WriteMsg::Shutdown => {
                let _ = stream.flush();
                // Shutdown affects the shared socket description, so the
                // reader observes EOF and exits too.
                let _ = stream.shutdown(Shutdown::Both);
                return;
            }
        }
    }
}

fn run_reader(mut stream: TcpStream, max_payload: usize, inner: Arc<Inner>) {
    let mut decoder = FrameDecoder::new(max_payload);
    let mut chunk = vec![0u8; 8192];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                if decoder.push(&chunk[..n]).is_err() {
                    // Framing error: stream is unparseable, tear down.
                    break;
                }
                loop {
                    match decoder.next_frame() {
                        Ok(Some(frame)) => route_frame(&inner, frame),
                        Ok(None) => break,
                        Err(_fatal) => {
                            inner.closed.store(1, Ordering::Relaxed);
                            fail_all(&inner);
                            return;
                        }
                    }
                }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(_) => break,
        }
    }
    inner.closed.store(1, Ordering::Relaxed);
    fail_all(&inner);
}

fn fail_all(inner: &Arc<Inner>) {
    let mut inflight = inner.inflight.lock().unwrap();
    for (_, entry) in inflight.drain() {
        let _ = entry.tx.send(Err(ClientError::Closed));
    }
}

fn route_frame(inner: &Arc<Inner>, frame: Frame) {
    if frame.kind != FrameKind::Response {
        // Only responses may travel server→client on v1.
        return;
    }
    let id = frame.request_id;
    let stale_generation = !inner.slots.lock().unwrap().is_current(id);
    let mut inflight = inner.inflight.lock().unwrap();
    if stale_generation {
        drop(inflight);
        inner.late.fetch_add(1, Ordering::Relaxed);
        return;
    }
    match inflight.remove(&id) {
        // Unbounded per-call channel: send never blocks, no allocation growth.
        Some(entry) => {
            let _ = entry.tx.send(Ok(frame));
        }
        // Id released after timeout/drop before the response got here.
        None => {
            inner.late.fetch_add(1, Ordering::Relaxed);
        }
    }
}
