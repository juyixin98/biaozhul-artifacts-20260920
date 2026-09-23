//! HTTP API handlers and the repository-independent verification endpoint.

use std::path::Path;
use std::sync::{Arc, Mutex};

use crate::base64;
use crate::http::{Handler, Request, Response};
use crate::json::{self, Json};
use crate::merkle::{self, RangeProof};
use crate::store::{Store, StoreError};
use crate::vfs::{RealVfs, Vfs};

pub fn hex(h: &[u8]) -> String {
    h.iter().map(|b| format!("{b:02x}")).collect()
}

fn root_fields(root: &merkle::Hash32) -> Vec<(&'static str, Json)> {
    vec![
        ("root", Json::Str(base64::encode(root))),
        ("root_hex", Json::Str(hex(root))),
    ]
}

pub struct RepoHandler<V: Vfs> {
    store: Mutex<Store<V>>,
}

impl<V: Vfs> RepoHandler<V> {
    pub fn new(store: Store<V>) -> Self {
        RepoHandler {
            store: Mutex::new(store),
        }
    }

    pub fn shared(store: Store<V>) -> Arc<Self> {
        Arc::new(Self::new(store))
    }
}

impl RepoHandler<RealVfs> {
    pub fn open(dir: &Path) -> Result<Self, StoreError> {
        Ok(Self::new(Store::open(RealVfs::new(), dir)?))
    }
}

fn store_err_response(e: StoreError) -> Response {
    match e {
        StoreError::InvalidInput(msg) => Response::error_json(400, &msg),
        StoreError::Corrupt(msg) => Response::error_json(500, &msg),
        StoreError::Io(io) => Response::error_json(500, &format!("io error: {io}")),
    }
}

fn parse_u64(s: Option<String>, name: &str) -> Result<u64, Response> {
    s.and_then(|v| v.parse().ok())
        .ok_or_else(|| Response::error_json(400, &format!("missing/invalid '{name}'")))
}

impl<V: Vfs> Handler for RepoHandler<V> {
    fn handle(&self, req: &Request) -> Response {
        match (req.method.as_str(), req.path.as_str()) {
            ("GET", "/health") => Response::json(
                200,
                json::obj(vec![("status", Json::Str("ok".into()))]).pretty(),
            ),
            ("GET", "/") => Response::json(
                200,
                json::obj(vec![
                    ("service", Json::Str("merkle-store".into())),
                    (
                        "endpoints",
                        Json::Array(
                            [
                                "GET /health",
                                "GET /root",
                                "GET /range?start=0&end=1 (or which=first|last)",
                                "PUT /block?index=0 (raw bytes)",
                                "POST /build (raw bytes; full-file atomic build)",
                                "POST /verify (self-contained JSON, does not touch the store)",
                            ]
                            .iter()
                            .map(|s| Json::Str((*s).to_string()))
                            .collect(),
                        ),
                    ),
                ])
                .pretty(),
            ),
            ("GET", "/root") => self.handle_root(),
            ("GET", "/range") => self.handle_range(req),
            ("PUT", "/block") => self.handle_put_block(req),
            ("POST", "/build") => self.handle_build(req),
            ("POST", "/verify") => verify_request(req),
            (_, _) => Response::error_json(404, "no such endpoint"),
        }
    }
}

impl<V: Vfs> RepoHandler<V> {
    fn handle_root(&self) -> Response {
        let s = self.store.lock().unwrap();
        match s.root() {
            Ok(r) => {
                let mut fields = vec![
                    ("block_size", Json::Int(s.block_size() as i64)),
                    ("data_len", Json::Int(s.data_len() as i64)),
                    ("n", Json::Int(s.block_count() as i64)),
                ];
                fields.extend(root_fields(&r));
                Response::json(200, json::obj(fields).pretty())
            }
            Err(e) => store_err_response(e),
        }
    }

    fn handle_range(&self, req: &Request) -> Response {
        // Resolve the range first; "which" aliases need the current block count.
        let (start, end) = {
            let s = self.store.lock().unwrap();
            match resolve_range(req, s.block_count()) {
                Ok(v) => v,
                Err(r) => return r,
            }
        };
        let s = self.store.lock().unwrap();
        let proof = match s.prove_range(start, end) {
            Ok(p) => p,
            Err(e) => return store_err_response(e),
        };
        let mut blocks = Vec::new();
        for i in start..end {
            match s.read_block(i) {
                Ok(bytes) => blocks.push(json::obj(vec![
                    ("index", Json::Int(i as i64)),
                    ("length", Json::Int(bytes.len() as i64)),
                    ("data", Json::Str(base64::encode(&bytes))),
                ])),
                Err(e) => return store_err_response(e),
            }
        }
        match s.root() {
            Ok(root) => {
                let mut fields = vec![
                    ("block_size", Json::Int(s.block_size() as i64)),
                    ("data_len", Json::Int(s.data_len() as i64)),
                    ("n", Json::Int(s.block_count() as i64)),
                    ("start", Json::Int(start as i64)),
                    ("end", Json::Int(end as i64)),
                    ("blocks", Json::Array(blocks)),
                    ("proof", merkle::proof_to_json(&proof)),
                ];
                fields.extend(root_fields(&root));
                Response::json(200, json::obj(fields).pretty())
            }
            Err(e) => store_err_response(e),
        }
    }

    fn handle_put_block(&self, req: &Request) -> Response {
        let index = match parse_u64(req.query_param("index"), "index") {
            Ok(v) => v,
            Err(r) => return r,
        };
        let mut s = self.store.lock().unwrap();
        if let Err(e) = s.put_block(index, &req.body) {
            return store_err_response(e);
        }
        match s.root() {
            Ok(r) => {
                let mut fields = vec![
                    ("ok", Json::Bool(true)),
                    ("index", Json::Int(index as i64)),
                    ("data_len", Json::Int(s.data_len() as i64)),
                    ("n", Json::Int(s.block_count() as i64)),
                ];
                fields.extend(root_fields(&r));
                Response::json(200, json::obj(fields).pretty())
            }
            Err(e) => store_err_response(e),
        }
    }

    fn handle_build(&self, req: &Request) -> Response {
        let mut s = self.store.lock().unwrap();
        if let Err(e) = s.reset(&req.body) {
            return store_err_response(e);
        }
        match s.root() {
            Ok(r) => {
                let mut fields = vec![
                    ("ok", Json::Bool(true)),
                    ("data_len", Json::Int(s.data_len() as i64)),
                    ("n", Json::Int(s.block_count() as i64)),
                ];
                fields.extend(root_fields(&r));
                Response::json(200, json::obj(fields).pretty())
            }
            Err(e) => store_err_response(e),
        }
    }
}

fn resolve_range(req: &Request, n: u64) -> Result<(u64, u64), Response> {
    if let Some(which) = req.query_param("which") {
        // Empty file: the only valid range is the empty [0,0).
        if n == 0 {
            return Ok((0, 0));
        }
        return match which.as_str() {
            "first" => Ok((0, 1)),
            "last" => Ok((n - 1, n)),
            _ => Err(Response::error_json(400, "which must be first|last")),
        };
    }
    let start = parse_u64(req.query_param("start"), "start")?;
    let end = parse_u64(req.query_param("end"), "end")?;
    Ok((start, end))
}

/// Stateless verification: the request carries everything the verifier needs
/// (trusted root, declared sizes, block bytes, proof). The store is never read.
pub fn verify_request(req: &Request) -> Response {
    verify_body(&req.body)
}

pub fn verify_body(body: &[u8]) -> Response {
    let parsed = match json::parse(&String::from_utf8_lossy(body)) {
        Ok(v) => v,
        Err(e) => return Response::error_json(400, &format!("invalid JSON: {e}")),
    };
    match verify_parsed(&parsed) {
        Ok(()) => Response::json(200, json::obj(vec![("valid", Json::Bool(true))]).pretty()),
        Err(msg) => Response::json(
            200,
            json::obj(vec![
                ("valid", Json::Bool(false)),
                ("error", Json::Str(msg)),
            ])
            .pretty(),
        ),
    }
}

fn field_i64(v: &Json, name: &str) -> Result<i64, String> {
    v.get(name)
        .and_then(Json::as_i64)
        .ok_or_else(|| format!("missing/invalid '{name}'"))
}

fn verify_parsed(v: &Json) -> Result<(), String> {
    let block_size = field_i64(v, "block_size")?;
    let data_len = field_i64(v, "data_len")?;
    let n = field_i64(v, "n")?;
    let start = field_i64(v, "start")?;
    let end = field_i64(v, "end")?;
    if block_size <= 0 || data_len < 0 || n < 0 || start < 0 || end < 0 {
        return Err("numeric fields must be non-negative (block_size > 0)".into());
    }

    let root_s = v
        .get("root")
        .and_then(Json::as_str)
        .ok_or("missing 'root' (base64 of 32 bytes)")?;
    let root_bytes = base64::decode(root_s).map_err(|_| "root not valid base64".to_string())?;
    if root_bytes.len() != 32 {
        return Err("root must decode to 32 bytes".into());
    }
    let mut root = [0u8; 32];
    root.copy_from_slice(&root_bytes);

    let blocks_v = v
        .get("blocks")
        .and_then(Json::as_array)
        .ok_or("missing 'blocks' array")?;
    let mut blocks = Vec::with_capacity(blocks_v.len());
    for item in blocks_v {
        // Accept either a bare base64 string or a /range-style block object
        // {"index":..,"length":..,"data":"<base64>"}.
        let s = match item {
            Json::Str(s) => s.as_str(),
            Json::Object(_) => item
                .get("data")
                .and_then(Json::as_str)
                .ok_or("block object missing string 'data'")?,
            _ => return Err("blocks must be base64 strings or {data} objects".into()),
        };
        blocks.push(base64::decode(s).map_err(|_| "block not valid base64")?);
    }

    let proof: RangeProof = merkle::proof_from_json(
        n as usize,
        start as usize,
        end as usize,
        v.get("proof").ok_or("missing 'proof' array")?,
    )?;

    merkle::verify(&proof, block_size as u64, data_len as u64, &root, &blocks)
        .map_err(|e| e.to_string())
}
