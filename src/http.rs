//! Local HTTP validation server.
//!
//! Thread-per-connection, JSON in/out. The binary in `src/main.rs` is only a
//! local verification entry point — there is intentionally no frontend.
//!
//! ## API
//!
//! | Method & path                | Body                          | Meaning |
//! |------------------------------|-------------------------------|---------|
//! | `POST /tx/read`              | `{}`                          | open a snapshot read txn |
//! | `GET  /tx/read/{id}/get?key=`| –                             | consistent read |
//! | `POST /tx/read/{id}/release` | `{}`                          | release the snapshot |
//! | `POST /tx/write`             | `{}`                          | begin a write txn |
//! | `PUT  /tx/write/{id}`        | `{"ops":[{"put":{"k":..,"v":..}},{"del":{"k":..}}]}` | buffer writes |
//! | `POST /tx/write/{id}/commit` | `{}`                          | commit (409 on conflict) |
//! | `POST /tx/write/{id}/abort`  | `{}`                          | discard |
//! | `POST /gc`                   | `{}`                          | run reclamation |
//! | `GET  /stats`                | –                             | versions, snapshots, bytes |
//! | `GET  /get?key=`             | –                             | one-shot latest read |

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;

use crate::json::{obj, s, Json};
use crate::mvcc::{CommitError, Engine, ReadTxn, WriteTxn};

struct ReadEntry(ReadTxn);
struct WriteEntry(WriteTxn);

struct ServerState {
    engine: Engine,
    reads: HashMap<u64, ReadEntry>,
    writes: HashMap<u64, WriteEntry>,
    next_read: u64,
    next_write: u64,
}

#[derive(Clone)]
pub struct Server {
    state: Arc<Mutex<ServerState>>,
}

type Resp = (u16, &'static str);
const OK: Resp = (200, "OK");
const CONFLICT: Resp = (409, "Conflict");
const BAD: Resp = (400, "Bad Request");
const NOTFOUND: Resp = (404, "Not Found");
const SERVERERR: Resp = (500, "Internal Server Error");

impl Server {
    pub fn new(dir: &Path) -> std::io::Result<Server> {
        let engine = Engine::open(dir)?;
        Ok(Self::with_engine(engine))
    }

    pub fn with_engine(engine: Engine) -> Server {
        Server {
            state: Arc::new(Mutex::new(ServerState {
                engine,
                reads: HashMap::new(),
                writes: HashMap::new(),
                next_read: 1,
                next_write: 1,
            })),
        }
    }

    /// Bind and serve connections on background threads. Returns the bound
    /// address (handy for ephemeral ports in tests).
    pub fn serve(&self, addr: &str) -> std::io::Result<std::net::SocketAddr> {
        let listener = TcpListener::bind(addr)?;
        let bound = listener.local_addr()?;
        let state = self.state.clone();
        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let state = state.clone();
                thread::spawn(move || {
                    let _ = handle(stream, state);
                });
            }
        });
        Ok(bound)
    }

    /// Engine handle for tests that want to inspect state directly.
    pub fn engine(&self) -> Engine {
        self.state.lock().unwrap().engine.clone()
    }
}

fn handle(mut stream: TcpStream, state: Arc<Mutex<ServerState>>) -> std::io::Result<()> {
    let mut head = [0u8; 4096];
    let n = stream.read(&mut head)?;
    if n == 0 {
        return Ok(());
    }
    let head = String::from_utf8_lossy(&head[..n]).to_string();
    let request_line = head.lines().next().unwrap_or("");
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("/").to_string();

    let (headers, mut body) = split_head_body(&head);
    let clen: usize = headers
        .iter()
        .find_map(|(k, v)| {
            if k.eq_ignore_ascii_case("content-length") {
                v.parse().ok()
            } else {
                None
            }
        })
        .unwrap_or(0);
    while body.len() < clen {
        let mut buf = vec![0u8; clen - body.len()];
        let r = stream.read(&mut buf)?;
        if r == 0 {
            break;
        }
        body.extend_from_slice(&buf[..r]);
    }

    let ((status, reason), json) = route(&method, &target, &body, &state);
    let text = json.to_string_compact();
    write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        text.len(),
        text
    )?;
    Ok(())
}

fn split_head_body(head: &str) -> (Vec<(String, String)>, Vec<u8>) {
    if let Some(idx) = head.find("\r\n\r\n") {
        let header_block = &head[..idx];
        let body = head.as_bytes()[idx + 4..].to_vec();
        let mut lines = header_block.split("\r\n");
        lines.next(); // request line
        let headers = lines
            .filter_map(|l| {
                let (k, v) = l.split_once(':')?;
                Some((k.trim().to_string(), v.trim().to_string()))
            })
            .collect();
        (headers, body)
    } else {
        (Vec::new(), Vec::new())
    }
}

fn bad(msg: impl Into<String>) -> (Resp, Json) {
    (BAD, obj(&[("error", s(msg))]))
}
fn notfound(msg: impl Into<String>) -> (Resp, Json) {
    (NOTFOUND, obj(&[("error", s(msg))]))
}

fn route(
    method: &str,
    target: &str,
    body: &[u8],
    state: &Arc<Mutex<ServerState>>,
) -> (Resp, Json) {
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let segments: Vec<&str> = path.trim_start_matches('/').split('/').collect();

    match (method, segments.as_slice()) {
        ("GET", ["get"]) => match parse_query(query, "key") {
            Some(k) => {
                let st = state.lock().unwrap();
                match st.engine.get_latest(k.as_bytes()) {
                    Some(v) => (
                        OK,
                        obj(&[
                            ("found", Json::Bool(true)),
                            ("key", s(k)),
                            ("value", s(String::from_utf8_lossy(&v))),
                        ]),
                    ),
                    None => (
                        OK,
                        obj(&[("found", Json::Bool(false)), ("key", s(k))]),
                    ),
                }
            }
            None => bad("missing key"),
        },

        ("POST", ["tx", "read"]) => {
            let mut st = state.lock().unwrap();
            let tx = st.engine.begin_read();
            let id = st.next_read;
            st.next_read += 1;
            let version = tx.version();
            st.reads.insert(id, ReadEntry(tx));
            (
                OK,
                obj(&[
                    ("read_txn_id", Json::Int(id as i64)),
                    ("version", Json::Int(version as i64)),
                ]),
            )
        }

        ("GET", ["tx", "read", id, "get"]) => {
            let id: u64 = match id.parse() {
                Ok(v) => v,
                Err(_) => return bad("bad read txn id"),
            };
            let key = match parse_query(query, "key") {
                Some(k) => k,
                None => return bad("missing key"),
            };
            let st = state.lock().unwrap();
            match st.reads.get(&id) {
                Some(tx) => match tx.0.get(key.as_bytes()) {
                    Some(v) => (
                        OK,
                        obj(&[
                            ("found", Json::Bool(true)),
                            ("key", s(key)),
                            ("value", s(String::from_utf8_lossy(&v))),
                        ]),
                    ),
                    None => (OK, obj(&[("found", Json::Bool(false)), ("key", s(key))])),
                },
                None => notfound("unknown read txn"),
            }
        }

        ("POST", ["tx", "read", id, "release"]) => {
            let id: u64 = match id.parse() {
                Ok(v) => v,
                Err(_) => return bad("bad read txn id"),
            };
            let mut st = state.lock().unwrap();
            match st.reads.remove(&id) {
                Some(tx) => {
                    drop(tx); // ReadTxn::Drop releases the snapshot
                    (
                        OK,
                        obj(&[
                            ("released", Json::Bool(true)),
                            ("read_txn_id", Json::Int(id as i64)),
                        ]),
                    )
                }
                None => notfound("unknown read txn"),
            }
        }

        ("POST", ["tx", "write"]) => {
            let mut st = state.lock().unwrap();
            let tx = st.engine.begin_write();
            let id = st.next_write;
            st.next_write += 1;
            let version = tx.snapshot_version();
            st.writes.insert(id, WriteEntry(tx));
            (
                OK,
                obj(&[
                    ("write_txn_id", Json::Int(id as i64)),
                    ("snapshot_version", Json::Int(version as i64)),
                ]),
            )
        }

        ("PUT", ["tx", "write", id]) => {
            let id: u64 = match id.parse() {
                Ok(v) => v,
                Err(_) => return bad("bad write txn id"),
            };
            let parsed = match Json::from_bytes(body) {
                Ok(j) => j,
                Err(e) => return bad(format!("bad json: {e}")),
            };
            let ops: &Vec<Json> = match parsed.get("ops") {
                Some(Json::Arr(a)) => a,
                _ => return bad("expected {\"ops\":[...]}"),
            };
            let mut st = state.lock().unwrap();
            let tx = match st.writes.get_mut(&id) {
                Some(tx) => tx,
                None => return notfound("unknown write txn"),
            };
            for op in ops {
                if let Some(put) = op.get("put") {
                    let k = match put.get("k").and_then(|x| x.as_str()) {
                        Some(k) => k,
                        None => return bad("put needs k"),
                    };
                    let v = match put.get("v").and_then(|x| x.as_str()) {
                        Some(v) => v,
                        None => return bad("put needs v"),
                    };
                    tx.0.put(k.as_bytes().to_vec(), v.as_bytes().to_vec());
                } else if let Some(del) = op.get("del") {
                    let k = match del.get("k").and_then(|x| x.as_str()) {
                        Some(k) => k,
                        None => return bad("del needs k"),
                    };
                    tx.0.del(k.as_bytes().to_vec());
                } else {
                    return bad("unknown op (need put/del)");
                }
            }
            (
                OK,
                obj(&[
                    ("buffered", Json::Int(ops.len() as i64)),
                    ("write_txn_id", Json::Int(id as i64)),
                ]),
            )
        }

        ("POST", ["tx", "write", id, "commit"]) => {
            let id: u64 = match id.parse() {
                Ok(v) => v,
                Err(_) => return bad("bad write txn id"),
            };
            let mut st = state.lock().unwrap();
            let tx = match st.writes.remove(&id) {
                Some(tx) => tx,
                None => return notfound("unknown write txn"),
            };
            match tx.0.commit() {
                Ok(version) => (
                    OK,
                    obj(&[
                        ("committed", Json::Bool(true)),
                        ("version", Json::Int(version as i64)),
                    ]),
                ),
                Err(CommitError::Conflict { keys }) => {
                    let keys: Vec<Json> = keys
                        .iter()
                        .map(|k| s(String::from_utf8_lossy(k)))
                        .collect();
                    (
                        CONFLICT,
                        obj(&[
                            ("committed", Json::Bool(false)),
                            ("error", s("write-write conflict")),
                            ("keys", Json::Arr(keys)),
                        ]),
                    )
                }
                Err(CommitError::Io(e)) => (
                    SERVERERR,
                    obj(&[("error", s(format!("io: {e}")))]),
                ),
            }
        }

        ("POST", ["tx", "write", id, "abort"]) => {
            let id: u64 = match id.parse() {
                Ok(v) => v,
                Err(_) => return bad("bad write txn id"),
            };
            let mut st = state.lock().unwrap();
            match st.writes.remove(&id) {
                Some(_) => (OK, obj(&[("aborted", Json::Bool(true))])),
                None => notfound("unknown write txn"),
            }
        }

        ("POST", ["gc"]) => {
            let st = state.lock().unwrap();
            match st.engine.gc() {
                Ok(Some(r)) => (
                    OK,
                    obj(&[
                        ("ran", Json::Bool(true)),
                        ("watermark", Json::Int(r.watermark as i64)),
                        ("bytes_before", Json::Int(r.bytes_before as i64)),
                        ("bytes_after", Json::Int(r.bytes_after as i64)),
                        ("reclaimed", Json::Int(r.reclaimed as i64)),
                    ]),
                ),
                Ok(None) => (
                    OK,
                    obj(&[
                        ("ran", Json::Bool(false)),
                        ("reason", s("nothing to reclaim at current watermark")),
                    ]),
                ),
                Err(e) => (
                    SERVERERR,
                    obj(&[("error", s(format!("gc failed: {e}")))]),
                ),
            }
        }

        ("GET", ["stats"]) => {
            let st = state.lock().unwrap();
            (OK, stats_json(&st.engine.stats()))
        }

        _ => notfound("no such route"),
    }
}

fn stats_json(st: &crate::mvcc::Stats) -> Json {
    let snaps: Vec<Json> = st
        .snapshots
        .iter()
        .map(|si| {
            obj(&[
                ("id", Json::Int(si.id as i64)),
                ("version", Json::Int(si.version as i64)),
            ])
        })
        .collect();
    obj(&[
        ("latest_version", Json::Int(st.latest_version as i64)),
        ("committed_versions", Json::Int(st.committed_versions as i64)),
        ("live_keys", Json::Int(st.live_keys as i64)),
        ("watermark", Json::Int(st.watermark as i64)),
        ("file_bytes", Json::Int(st.file_bytes as i64)),
        ("retained_bytes", Json::Int(st.retained_bytes as i64)),
        ("reclaimable_bytes", Json::Int(st.reclaimable_bytes as i64)),
        ("snapshots", Json::Arr(snaps)),
    ])
}

fn parse_query(query: &str, key: &str) -> Option<String> {
    for pair in query.split('&') {
        let (k, v) = pair.split_once('=')?;
        if k == key {
            return Some(percent_decode(v));
        }
    }
    None
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let h = |b: u8| match b {
                b'0'..=b'9' => Some(b - b'0'),
                b'a'..=b'f' => Some(b - b'a' + 10),
                b'A'..=b'F' => Some(b - b'A' + 10),
                _ => None,
            };
            if let (Some(a), Some(b)) = (h(bytes[i + 1]), h(bytes[i + 2])) {
                out.push(a * 16 + b);
                i += 3;
                continue;
            }
        } else if bytes[i] == b'+' {
            out.push(b' ');
            i += 1;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}
