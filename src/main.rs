//! sst 命令行与本地 HTTP 验证入口。
//!
//! 子命令：
//!   sst build  --out FILE [--block-size N] [--restart-interval N]   从 stdin 构建（行格式 hexkey=hexvalue）
//!   sst gen    --n N [--seed S] [--prefix HEX]                      生成随机 kv 行到 stdout（供 build 使用）
//!   sst verify --table FILE                                         严格校验
//!   sst get    --table FILE --key HEX
//!   sst scan   --table FILE [--start HEX] [--end HEX] [--limit N]
//!   sst serve  --table FILE [--addr 127.0.0.1:18080]                本地 HTTP 验证入口

use sst::hex::{from_hex, to_hex};
use sst::{
    Error, FileReader, FileWriter, Result, TableOptions, TableReader, TableWriter,
};
use std::collections::BTreeMap;
use std::io::{BufRead, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;

fn main() {
    let code = match run() {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("error: {e}");
            1
        }
    };
    std::process::exit(code);
}

fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first() else {
        usage();
        return Err(Error::bad_request("missing subcommand"));
    };
    let rest = &args[1..];
    match cmd.as_str() {
        "build" => cmd_build(rest),
        "gen" => cmd_gen(rest),
        "verify" => cmd_verify(rest),
        "get" => cmd_get(rest),
        "scan" => cmd_scan(rest),
        "serve" => cmd_serve(rest),
        _ => {
            usage();
            Err(Error::bad_request(format!("unknown subcommand: {cmd}")))
        }
    }
}

fn usage() {
    eprintln!(
        "usage:
  sst build  --out FILE [--block-size N] [--restart-interval N]   # stdin: 每行 hexkey=hexvalue
  sst gen    --n N [--seed S] [--prefix HEX]                      # 生成随机 kv 行
  sst verify --table FILE
  sst get    --table FILE --key HEX
  sst scan   --table FILE [--start HEX] [--end HEX] [--limit N]
  sst serve  --table FILE [--addr 127.0.0.1:18080]"
    );
}

// ---------------------------------------------------------------- 参数解析

fn opt<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    let mut i = 0;
    while i < args.len() {
        if args[i] == name {
            return args.get(i + 1).map(|s| s.as_str());
        }
        i += 1;
    }
    None
}

fn opt_req<'a>(args: &'a [String], name: &str) -> Result<&'a str> {
    opt(args, name).ok_or_else(|| Error::bad_request(format!("missing required option {name}")))
}

fn opt_usize(args: &[String], name: &str, default: usize) -> Result<usize> {
    match opt(args, name) {
        Some(s) => s
            .parse()
            .map_err(|_| Error::bad_request(format!("invalid integer for {name}: {s}"))),
        None => Ok(default),
    }
}

// ---------------------------------------------------------------- 子命令

fn cmd_build(args: &[String]) -> Result<()> {
    let out = opt_req(args, "--out")?;
    let block_size = opt_usize(args, "--block-size", 4096)?;
    let restart_interval = opt_usize(args, "--restart-interval", 16)?;

    // 读 stdin：每行 hexkey=hexvalue；空行忽略；重复键报错
    let stdin = std::io::stdin();
    let mut map: BTreeMap<Vec<u8>, Vec<u8>> = BTreeMap::new();
    for (lineno, line) in stdin.lock().lines().enumerate() {
        let line = line?;
        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            continue;
        }
        let (k, v) = line
            .split_once('=')
            .ok_or_else(|| Error::bad_request(format!("line {}: expected hexkey=hexvalue", lineno + 1)))?;
        let key = from_hex(k)?;
        let value = from_hex(v)?;
        if map.insert(key, value).is_some() {
            return Err(Error::bad_request(format!(
                "line {}: duplicate key {k}",
                lineno + 1
            )));
        }
    }

    let writer = FileWriter::create(out)?;
    let mut tw = TableWriter::new(
        writer,
        TableOptions {
            block_size,
            restart_interval,
        },
    );
    for (k, v) in &map {
        tw.add(k, v)?;
    }
    let stats = tw.finish()?;
    println!(
        "{{\"entries\":{},\"blocks\":{},\"file_size\":{}}}",
        stats.num_entries, stats.num_blocks, stats.file_size
    );
    Ok(())
}

/// 确定性 xorshift64* 伪随机数（避免外部依赖）。
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
}

fn cmd_gen(args: &[String]) -> Result<()> {
    let n = opt_usize(args, "--n", 1000)?;
    let seed = opt_usize(args, "--seed", 1)? as u64;
    let prefix = from_hex(opt(args, "--prefix").unwrap_or(""))?;
    let mut rng = Rng(seed.max(1));
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    for _ in 0..n {
        let mut key = prefix.clone();
        key.extend_from_slice(rng.next().to_be_bytes().as_slice());
        let mut val = Vec::new();
        for _ in 0..(rng.next() % 3 + 1) {
            val.extend_from_slice(rng.next().to_be_bytes().as_slice());
        }
        writeln!(out, "{}={}", to_hex(&key), to_hex(&val))?;
    }
    Ok(())
}

fn open_table(args: &[String]) -> Result<TableReader<FileReader>> {
    let path = opt_req(args, "--table")?;
    TableReader::open(FileReader::open(path)?)
}

fn cmd_verify(args: &[String]) -> Result<()> {
    let t = open_table(args)?;
    let r = t.verify()?;
    println!(
        "{{\"ok\":true,\"blocks\":{},\"entries\":{},\"first_key\":\"{}\",\"last_key\":\"{}\",\"file_size\":{}}}",
        r.num_blocks,
        r.num_entries,
        r.first_key.as_deref().map(to_hex).unwrap_or_default(),
        r.last_key.as_deref().map(to_hex).unwrap_or_default(),
        r.file_size
    );
    Ok(())
}

fn cmd_get(args: &[String]) -> Result<()> {
    let t = open_table(args)?;
    let key = from_hex(opt_req(args, "--key")?)?;
    match t.get(&key)? {
        Some(v) => println!("{{\"found\":true,\"value\":\"{}\"}}", to_hex(&v)),
        None => println!("{{\"found\":false}}"),
    }
    Ok(())
}

fn cmd_scan(args: &[String]) -> Result<()> {
    let t = open_table(args)?;
    let start = from_hex(opt(args, "--start").unwrap_or(""))?;
    let end = match opt(args, "--end") {
        Some(s) => Some(from_hex(s)?),
        None => None,
    };
    let limit = opt_usize(args, "--limit", 1000)?;
    let mut out = std::io::stdout().lock();
    writeln!(out, "[")?;
    for (count, item) in t.scan(&start, end.as_deref()).enumerate() {
        let (k, v) = item?;
        if count >= limit {
            break;
        }
        if count > 0 {
            writeln!(out, ",")?;
        }
        write!(out, "  [\"{}\",\"{}\"]", to_hex(&k), to_hex(&v))?;
    }
    writeln!(out, "\n]")?;
    Ok(())
}

// ---------------------------------------------------------------- HTTP 服务

fn cmd_serve(args: &[String]) -> Result<()> {
    let path = opt_req(args, "--table")?;
    let addr = opt(args, "--addr").unwrap_or("127.0.0.1:18080");
    let table = Arc::new(TableReader::open(FileReader::open(path)?)?);
    let listener = TcpListener::bind(addr).map_err(|e| {
        Error::bad_request(format!("cannot bind {addr}: {e}"))
    })?;
    eprintln!("serving {path} on http://{addr}  (GET /get /scan /verify /stats /health)");
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let table = Arc::clone(&table);
                std::thread::spawn(move || {
                    let _ = handle_conn(s, &table);
                });
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
    Ok(())
}

fn json_escape(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

fn respond(stream: &mut TcpStream, status: &str, body: &str) -> std::io::Result<()> {
    let resp = format!(
        "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(resp.as_bytes())
}

fn handle_conn(mut stream: TcpStream, table: &TableReader<FileReader>) -> std::io::Result<()> {
    let mut buf = Vec::with_capacity(1024);
    let mut chunk = [0u8; 4096];
    // 读到请求头结束（本服务只处理无请求体的 GET）
    loop {
        let n = stream.read(&mut chunk)?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..n]);
        if buf.windows(4).any(|w| w == b"\r\n\r\n") || buf.len() > 65536 {
            break;
        }
    }
    let req = String::from_utf8_lossy(&buf);
    let line = req.lines().next().unwrap_or("");
    let mut parts = line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let target = parts.next().unwrap_or("");
    if method != "GET" {
        return respond(&mut stream, "405 Method Not Allowed", "{\"error\":\"only GET is supported\"}");
    }
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let params: std::collections::HashMap<&str, &str> = query
        .split('&')
        .filter(|p| !p.is_empty())
        .filter_map(|p| p.split_once('=').or(Some((p, ""))))
        .collect();

    let hex_param = |name: &str| -> Result<Option<Vec<u8>>> {
        match params.get(name) {
            Some(v) => Ok(Some(from_hex(v)?)),
            None => Ok(None),
        }
    };

    let result: Result<(String, String)> = (|| {
        match path {
            "/health" => Ok(("200 OK".into(), "{\"ok\":true}".into())),
            "/stats" => Ok((
                "200 OK".into(),
                format!(
                    "{{\"entries\":{},\"blocks\":{},\"file_size\":{}}}",
                    table.num_entries(),
                    table.num_blocks(),
                    table.file_size()
                ),
            )),
            "/get" => {
                let key = hex_param("key")?
                    .ok_or_else(|| Error::bad_request("missing query param: key"))?;
                match table.get(&key)? {
                    Some(v) => Ok((
                        "200 OK".into(),
                        format!("{{\"found\":true,\"key\":\"{}\",\"value\":\"{}\"}}", to_hex(&key), to_hex(&v)),
                    )),
                    None => Ok(("200 OK".into(), "{\"found\":false}".into())),
                }
            }
            "/scan" => {
                let start = hex_param("start")?.unwrap_or_default();
                let end = hex_param("end")?;
                let limit: usize = match params.get("limit") {
                    Some(s) => s.parse().map_err(|_| Error::bad_request("bad limit"))?,
                    None => 1000,
                };
                let mut entries = String::from("[");
                let mut count = 0usize;
                let mut truncated = false;
                for item in table.scan(&start, end.as_deref()) {
                    let (k, v) = item?;
                    if count >= limit {
                        truncated = true;
                        break;
                    }
                    if count > 0 {
                        entries.push(',');
                    }
                    entries.push_str(&format!("[\"{}\",\"{}\"]", to_hex(&k), to_hex(&v)));
                    count += 1;
                }
                entries.push(']');
                Ok((
                    "200 OK".into(),
                    format!("{{\"entries\":{entries},\"count\":{count},\"truncated\":{truncated}}}"),
                ))
            }
            "/verify" => match table.verify() {
                Ok(r) => Ok((
                    "200 OK".into(),
                    format!(
                        "{{\"ok\":true,\"blocks\":{},\"entries\":{},\"first_key\":\"{}\",\"last_key\":\"{}\",\"file_size\":{}}}",
                        r.num_blocks,
                        r.num_entries,
                        r.first_key.as_deref().map(to_hex).unwrap_or_default(),
                        r.last_key.as_deref().map(to_hex).unwrap_or_default(),
                        r.file_size
                    ),
                )),
                Err(e) => Ok((
                    "500 Internal Server Error".into(),
                    format!("{{\"ok\":false,\"error\":\"{}\"}}", json_escape(&e.to_string())),
                )),
            },
            _ => Ok(("404 Not Found".into(), "{\"error\":\"not found\"}".into())),
        }
    })();

    match result {
        Ok((status, body)) => respond(&mut stream, &status, &body),
        Err(e @ Error::BadRequest(_)) => respond(
            &mut stream,
            "400 Bad Request",
            &format!("{{\"error\":\"{}\"}}", json_escape(&e.to_string())),
        ),
        Err(e) => respond(
            &mut stream,
            "500 Internal Server Error",
            &format!("{{\"error\":\"{}\"}}", json_escape(&e.to_string())),
        ),
    }
}
