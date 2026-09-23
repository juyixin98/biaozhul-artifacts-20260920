//! 本地 HTTP 验证入口（纯 `std::net`，无第三方运行时，线程/连接）。
//!
//! # 路由
//!
//! | 方法 | 路径 | 说明 |
//! |---|---|---|
//! | POST | `/repos/:name/open?chunk_size=4096` | 创建/打开仓库（幂等） |
//! | PUT  | `/repos/:name/file` | 写入/替换文件，body 为 hex 字符串 |
//! | GET  | `/repos/:name` | 查询状态（根、长度、块数、版本） |
//! | GET  | `/repos/:name/proof?start=&end=` | 生成范围证明（默认全范围） |
//! | POST | `/verify` | 独立校验请求体中的 [`RangeProof`]（不依赖任何仓库/文件） |
//!
//! 错误统一为 `{"error":"..."}` + 非 2xx 状态码。

use crate::merkle::RangeProof;
use crate::store::Repository;
use crate::vfs::{RealVfs, Vfs};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

type SharedRepo = Arc<Mutex<Repository>>;

/// 服务端注册表。
pub struct Server {
    data_dir: PathBuf,
    repos: Mutex<HashMap<String, SharedRepo>>,
}

impl Server {
    pub fn new(data_dir: impl Into<PathBuf>) -> Arc<Self> {
        Arc::new(Self {
            data_dir: data_dir.into(),
            repos: Mutex::new(HashMap::new()),
        })
    }

    /// 阻塞监听。
    pub fn serve(self: &Arc<Self>, addr: &str) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.data_dir)?;
        let listener = TcpListener::bind(addr)?;
        self.serve_listener(listener)
    }

    /// 在已绑定的 listener 上服务（测试用，避免端口竞争）。
    pub fn serve_listener(self: &Arc<Self>, listener: TcpListener) -> std::io::Result<()> {
        let local = listener.local_addr()?;
        eprintln!(
            "imerkle listening on http://{} (data dir: {:?})",
            local, self.data_dir
        );
        for stream in listener.incoming() {
            match stream {
                Ok(s) => {
                    let server = self.clone();
                    std::thread::spawn(move || {
                        let _ = handle_connection(server, s);
                    });
                }
                Err(e) => eprintln!("accept error: {e}"),
            }
        }
        Ok(())
    }

    fn get_repo(&self, name: &str) -> Option<SharedRepo> {
        self.repos.lock().unwrap().get(name).cloned()
    }

    fn open_repo(&self, name: &str, chunk_size: u64) -> Result<SharedRepo, String> {
        validate_name(name)?;
        let mut repos = self.repos.lock().unwrap();
        if let Some(r) = repos.get(name) {
            return Ok(r.clone());
        }
        let dir = self.data_dir.join(name);
        let vfs: Arc<dyn Vfs> = Arc::new(RealVfs::new(&dir));
        let repo = Repository::open(vfs, chunk_size).map_err(|e| e.to_string())?;
        let arc = Arc::new(Mutex::new(repo));
        repos.insert(name.to_string(), arc.clone());
        Ok(arc)
    }
}

fn validate_name(name: &str) -> Result<(), String> {
    if name.is_empty()
        || name.len() > 64
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err("invalid repo name (allowed: A-Z a-z 0-9 - _)".into());
    }
    Ok(())
}

struct Request {
    method: String,
    path: String,
    query: String,
    body: Vec<u8>,
}

fn read_request(stream: &mut TcpStream) -> std::io::Result<Request> {
    let mut buf = Vec::with_capacity(8192);
    let mut tmp = [0u8; 4096];
    // 读到 header 结束
    let header_end = loop {
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "empty",
            ));
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(p) = find_subsequence(&buf, b"\r\n\r\n") {
            break p;
        }
        if buf.len() > 64 * 1024 * 1024 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "header too large",
            ));
        }
    };

    let header = String::from_utf8_lossy(&buf[..header_end]).to_string();
    let mut lines = header.split("\r\n");
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let raw_target = parts.next().unwrap_or("");
    let (path, query) = match raw_target.split_once('?') {
        Some((p, q)) => (p.to_string(), q.to_string()),
        None => (raw_target.to_string(), String::new()),
    };

    let content_length: usize = lines
        .find_map(|l| {
            let (k, v) = l.split_once(':')?;
            if k.eq_ignore_ascii_case("content-length") {
                v.trim().parse().ok()
            } else {
                None
            }
        })
        .unwrap_or(0);

    let mut body = buf[header_end + 4..].to_vec();
    while body.len() < content_length {
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&tmp[..n]);
    }
    body.truncate(content_length);

    Ok(Request {
        method,
        path,
        query,
        body,
    })
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

fn query_params(q: &str) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for pair in q.split('&').filter(|s| !s.is_empty()) {
        if let Some((k, v)) = pair.split_once('=') {
            out.insert(url_decode(k), url_decode(v));
        }
    }
    out
}

fn url_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hi = hex_val(bytes[i + 1]);
                let lo = hex_val(bytes[i + 2]);
                if let (Some(h), Some(l)) = (hi, lo) {
                    out.push((h << 4) | l);
                    i += 3;
                    continue;
                }
                out.push(bytes[i]);
                i += 1;
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_val(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

fn handle_connection(server: Arc<Server>, mut stream: TcpStream) -> std::io::Result<()> {
    let req = match read_request(&mut stream) {
        Ok(r) => r,
        Err(_) => return Ok(()),
    };
    let resp = route(&server, &req);
    write_response(&mut stream, resp.0, &resp.1, resp.2.as_deref())
}

fn route(server: &Server, req: &Request) -> (u16, Vec<u8>, Option<&'static str>) {
    let json = Some("application/json");
    let segments: Vec<&str> = req.path.split('/').filter(|s| !s.is_empty()).collect();

    // POST /verify —— 无状态
    if req.method == "POST" && segments == ["verify"] {
        return match serde_json::from_slice::<RangeProof>(&req.body) {
            Ok(proof) => match proof.verify() {
                Ok(()) => (
                    200,
                    serde_json::json!({"valid": true}).to_string().into_bytes(),
                    json,
                ),
                Err(e) => (
                    200,
                    serde_json::json!({"valid": false, "error": e.to_string()})
                        .to_string()
                        .into_bytes(),
                    json,
                ),
            },
            Err(e) => (
                400,
                serde_json::json!({"error": format!("malformed proof JSON: {e}")})
                    .to_string()
                    .into_bytes(),
                json,
            ),
        };
    }

    // /repos/:name/...
    if segments.first() == Some(&"repos") {
        let name = segments.get(1).copied().unwrap_or("");
        if name.is_empty() {
            return err(404, "not found");
        }
        let sub = segments.get(2..).unwrap_or(&[]);

        // POST /repos/:name/open?chunk_size=
        if req.method == "POST" && sub == ["open"] {
            let params = query_params(&req.query);
            let chunk_size = params
                .get("chunk_size")
                .map(|v| v.parse::<u64>())
                .transpose();
            let chunk_size = match chunk_size {
                Ok(Some(n)) if n > 0 => n,
                Ok(None) => crate::merkle::DEFAULT_CHUNK_SIZE,
                _ => return err(400, "chunk_size must be a positive integer"),
            };
            return match server.open_repo(name, chunk_size) {
                Ok(repo) => {
                    let r = repo.lock().unwrap();
                    (
                        200,
                        serde_json::json!({
                            "repo": name,
                            "chunk_size": r.chunk_size(),
                            "file_length": r.file_length(),
                            "chunk_count": r.chunk_count(),
                            "version": r.version(),
                            "root": r.root_hex(),
                        })
                        .to_string()
                        .into_bytes(),
                        json,
                    )
                }
                Err(e) => err(500, &e),
            };
        }

        // 以下路由需要已打开的仓库
        let repo = match server.get_repo(name) {
            Some(r) => r,
            None => {
                return err(
                    404,
                    &format!("repo '{name}' is not open; POST /repos/{name}/open"),
                )
            }
        };

        // GET /repos/:name
        if req.method == "GET" && sub.is_empty() {
            let r = repo.lock().unwrap();
            return (
                200,
                serde_json::json!({
                    "repo": name,
                    "chunk_size": r.chunk_size(),
                    "file_length": r.file_length(),
                    "chunk_count": r.chunk_count(),
                    "version": r.version(),
                    "root": r.root_hex(),
                })
                .to_string()
                .into_bytes(),
                json,
            );
        }

        // PUT /repos/:name/file  body: JSON {"data":"<hex>"} 或裸 hex 字符串
        if req.method == "PUT" && sub == ["file"] {
            let data_hex: String =
                if let Ok(v) = serde_json::from_slice::<serde_json::Value>(&req.body) {
                    v.get("data")
                        .and_then(|x| x.as_str())
                        .unwrap_or("")
                        .to_string()
                } else {
                    String::from_utf8_lossy(&req.body).trim().to_string()
                };
            let data = match crate::hex_codec::from_hex(data_hex.trim()) {
                Some(d) => d,
                None => return err(400, "body must be hex string or {\"data\":\"<hex>\"}"),
            };
            let mut r = repo.lock().unwrap();
            return match r.put_file(&data) {
                Ok((written, deleted)) => (
                    200,
                    serde_json::json!({
                        "file_length": r.file_length(),
                        "chunk_count": r.chunk_count(),
                        "version": r.version(),
                        "root": r.root_hex(),
                        "chunks_written": written,
                        "chunks_deleted": deleted,
                    })
                    .to_string()
                    .into_bytes(),
                    json,
                ),
                Err(e) => err(500, &format!("commit failed: {e}")),
            };
        }

        // GET /repos/:name/proof?start=&end=
        if req.method == "GET" && sub == ["proof"] {
            let params = query_params(&req.query);
            let r = repo.lock().unwrap();
            let total = r.chunk_count();
            let start = match params.get("start").map(|v| v.parse::<u64>()).transpose() {
                Ok(Some(n)) => n,
                Ok(None) => 0,
                Err(_) => return err(400, "start must be an integer"),
            };
            let end = match params.get("end").map(|v| v.parse::<u64>()).transpose() {
                Ok(Some(n)) => n,
                Ok(None) => total,
                Err(_) => return err(400, "end must be an integer"),
            };
            if total == 0 {
                return err(
                    400,
                    "file is empty: no range can be proved; empty root is the commitment",
                );
            }
            return match r.range_proof(start, end) {
                Ok(proof) => (
                    200,
                    serde_json::to_vec_pretty(&proof).unwrap_or_default(),
                    json,
                ),
                Err(e) => err(400, &e.to_string()),
            };
        }
    }

    err(404, "not found")
}

fn err(code: u16, msg: &str) -> (u16, Vec<u8>, Option<&'static str>) {
    (
        code,
        serde_json::json!({"error": msg}).to_string().into_bytes(),
        Some("application/json"),
    )
}

fn write_response(
    stream: &mut TcpStream,
    code: u16,
    body: &[u8],
    content_type: Option<&str>,
) -> std::io::Result<()> {
    let reason = match code {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        500 => "Internal Server Error",
        _ => "OK",
    };
    let head = format!(
        "HTTP/1.1 {code} {reason}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        content_type.unwrap_or("application/octet-stream"),
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body)?;
    stream.flush()
}

/// 测试辅助：在随机端口上起服务器，返回完整地址。
#[cfg(test)]
pub fn spawn_test_server() -> (Arc<Server>, String) {
    let dir = std::env::temp_dir().join(format!(
        "imerkle-http-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let server = Server::new(dir);
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    let s = server.clone();
    std::thread::spawn(move || {
        let _ = s.serve_listener(listener);
    });
    (server, addr)
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpStream;

    fn request(addr: &str, method: &str, path: &str, body: &str) -> (u16, String) {
        let mut s = TcpStream::connect(addr).unwrap();
        let req = format!(
            "{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        s.write_all(req.as_bytes()).unwrap();
        let mut raw = Vec::new();
        s.read_to_end(&mut raw).unwrap();
        let sep = super::find_subsequence(&raw, b"\r\n\r\n").unwrap();
        let status: u16 = String::from_utf8_lossy(&raw[..sep])
            .split_whitespace()
            .nth(1)
            .unwrap()
            .parse()
            .unwrap();
        (status, String::from_utf8_lossy(&raw[sep + 4..]).to_string())
    }

    #[test]
    fn end_to_end_http_flow() {
        let (_srv, addr) = super::spawn_test_server();

        // 打开
        let (code, body) = request(&addr, "POST", "/repos/demo/open?chunk_size=16", "");
        assert_eq!(code, 200, "{body}");
        let st: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(st["chunk_count"], 0);
        let empty_root = st["root"].as_str().unwrap().to_string();

        // 写 4 块数据（64 字节）
        let data: Vec<u8> = (0u8..64).collect();
        let payload = serde_json::json!({"data": crate::hex_codec::to_hex(&data)}).to_string();
        let (code, body) = request(&addr, "PUT", "/repos/demo/file", &payload);
        assert_eq!(code, 200, "{body}");
        let st: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(st["chunk_count"], 4);
        let root1 = st["root"].as_str().unwrap().to_string();
        assert_ne!(root1, empty_root);

        // 首块证明
        let (code, body) = request(&addr, "GET", "/repos/demo/proof?start=0&end=1", "");
        assert_eq!(code, 200, "{body}");
        let proof: crate::merkle::RangeProof = serde_json::from_str(&body).unwrap();
        assert_eq!(proof.chunks.len(), 1);

        // 独立验证通过
        let pj = serde_json::to_string(&proof).unwrap();
        let (code, body) = request(&addr, "POST", "/verify", &pj);
        assert_eq!(code, 200);
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["valid"], true);

        // 篡改后验证失败
        let mut bad = proof.clone();
        bad.chunks[0][0] ^= 0xff;
        let badj = serde_json::to_string(&bad).unwrap();
        let (_c, body) = request(&addr, "POST", "/verify", &badj);
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["valid"], false);

        // 伪造长度被拒
        let mut forged = proof;
        forged.file_length = 100;
        let fj = serde_json::to_string(&forged).unwrap();
        let (_c, body) = request(&addr, "POST", "/verify", &fj);
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["valid"], false);
    }

    #[test]
    fn empty_repo_proof_is_rejected() {
        let (_srv, addr) = super::spawn_test_server();
        let (code, _) = request(&addr, "POST", "/repos/blank/open?chunk_size=8", "");
        assert_eq!(code, 200);
        let (code, body) = request(&addr, "GET", "/repos/blank/proof", "");
        assert_eq!(code, 400, "{body}");
    }
}
