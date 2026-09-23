//! `merkle-store` CLI: local server, raw HTTP client, and offline verifier.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::ExitCode;

use merkle_store::api::{self, RepoHandler};
use merkle_store::base64;
use merkle_store::http;
use merkle_store::json::{self, Json};
use merkle_store::merkle;
use merkle_store::store::Store;
use merkle_store::vfs::RealVfs;

fn usage() -> String {
    r#"merkle-store — incremental Merkle-verified block store

USAGE:
  merkle-store init  <dir> --block-size <N>
  merkle-store build <dir> --file <path> [--block-size <N>]   (block-size only used for init)
  merkle-store put   <dir> --index <I> --file <path>
  merkle-store root  <dir>
  merkle-store range <dir> (--start <S> --end <E>) | --which first|last
  merkle-store verify <request.json>          offline verify of a /range response + trusted root
  merkle-store serve <dir> --addr 127.0.0.1:8080
  merkle-store req   <addr> <METHOD> <path> [--data <file>]

The `verify` subcommand reads the JSON body of a GET /range response plus a
"root" field (the trusted root, base64) and performs verification locally.
"#
    .to_string()
}

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn read_file(path: &str) -> std::io::Result<Vec<u8>> {
    std::fs::read(path)
}

fn print_json(body: &[u8]) -> i32 {
    match std::str::from_utf8(body) {
        Ok(text) => match json::parse(text) {
            Ok(v) => print!("{}", v.pretty()),
            Err(_) => print!("{text}"),
        },
        Err(_) => print!("{}", String::from_utf8_lossy(body)),
    }
    0
}

fn cmd_init(args: &[String]) -> ExitCode {
    let dir = match args.first() {
        Some(d) => d,
        None => {
            eprintln!("{}", usage());
            return ExitCode::from(2);
        }
    };
    let bs = match arg(args, "--block-size").and_then(|s| s.parse::<u64>().ok()) {
        Some(n) if n > 0 => n,
        _ => {
            eprintln!("--block-size <positive integer> required");
            return ExitCode::from(2);
        }
    };
    match Store::create(RealVfs::new(), &PathBuf::from(dir), bs) {
        Ok(_) => {
            println!("initialized empty repository at {dir} (block_size={bs})");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_build(args: &[String]) -> ExitCode {
    let (dir, file) = match (args.first(), arg(args, "--file")) {
        (Some(d), Some(f)) => (d, f),
        _ => {
            eprintln!("build <dir> --file <path>");
            return ExitCode::from(2);
        }
    };
    let bytes = match read_file(&file) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("cannot read {file}: {e}");
            return ExitCode::FAILURE;
        }
    };
    let result = Store::open(RealVfs::new(), &PathBuf::from(dir)).and_then(|mut s| s.reset(&bytes));
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_put(args: &[String]) -> ExitCode {
    let dir = args.first();
    let index = arg(args, "--index").and_then(|s| s.parse::<u64>().ok());
    let file = arg(args, "--file");
    let (dir, index, file) = match (dir, index, file) {
        (Some(d), Some(i), Some(f)) => (d, i, f),
        _ => {
            eprintln!("put <dir> --index <I> --file <path>");
            return ExitCode::from(2);
        }
    };
    let bytes = match read_file(&file) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("cannot read {file}: {e}");
            return ExitCode::FAILURE;
        }
    };
    match Store::open(RealVfs::new(), &PathBuf::from(dir)) {
        Ok(mut s) => match s.put_block(index, &bytes) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("error: {e}");
                ExitCode::FAILURE
            }
        },
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_root(args: &[String]) -> ExitCode {
    let dir = match args.first() {
        Some(d) => d,
        None => return ExitCode::from(2),
    };
    match Store::open(RealVfs::new(), &PathBuf::from(dir)) {
        Ok(s) => match s.root() {
            Ok(r) => {
                println!(
                    "{}",
                    json::obj(vec![
                        ("block_size", Json::Int(s.block_size() as i64)),
                        ("data_len", Json::Int(s.data_len() as i64)),
                        ("n", Json::Int(s.block_count() as i64)),
                        ("root", Json::Str(base64::encode(&r))),
                        ("root_hex", Json::Str(api::hex(&r))),
                    ])
                    .pretty()
                );
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("error: {e}");
                ExitCode::FAILURE
            }
        },
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_range(args: &[String]) -> ExitCode {
    let dir = match args.first() {
        Some(d) => d,
        None => return ExitCode::from(2),
    };
    let which = arg(args, "--which");
    let start = arg(args, "--start").and_then(|s| s.parse::<u64>().ok());
    let end = arg(args, "--end").and_then(|s| s.parse::<u64>().ok());
    match Store::open(RealVfs::new(), &PathBuf::from(dir)) {
        Ok(s) => {
            let (st, en) = if let Some(w) = which {
                match w.as_str() {
                    "first" if s.block_count() > 0 => (0, 1),
                    "last" if s.block_count() > 0 => (s.block_count() - 1, s.block_count()),
                    _ if s.block_count() == 0 => (0, 0),
                    _ => {
                        eprintln!("--which must be first|last");
                        return ExitCode::from(2);
                    }
                }
            } else {
                match (start, end) {
                    (Some(a), Some(b)) => (a, b),
                    _ => {
                        eprintln!("range requires --start/--end or --which first|last");
                        return ExitCode::from(2);
                    }
                }
            };
            match s.prove_range(st, en) {
                Ok(proof) => {
                    let root = s.root().unwrap_or_else(|_| merkle::empty_root());
                    let blocks: Vec<Json> = (st..en)
                        .map(|i| {
                            let b = s.read_block(i).unwrap();
                            json::obj(vec![
                                ("index", Json::Int(i as i64)),
                                ("length", Json::Int(b.len() as i64)),
                                ("data", Json::Str(base64::encode(&b))),
                            ])
                        })
                        .collect();
                    println!(
                        "{}",
                        json::obj(vec![
                            ("block_size", Json::Int(s.block_size() as i64)),
                            ("data_len", Json::Int(s.data_len() as i64)),
                            ("n", Json::Int(s.block_count() as i64)),
                            ("start", Json::Int(st as i64)),
                            ("end", Json::Int(en as i64)),
                            ("root", Json::Str(base64::encode(&root))),
                            ("blocks", Json::Array(blocks)),
                            ("proof", merkle::proof_to_json(&proof)),
                        ])
                        .pretty()
                    );
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("error: {e}");
                    ExitCode::FAILURE
                }
            }
        }
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_verify(args: &[String]) -> ExitCode {
    let path = match args.first() {
        Some(p) => p,
        None => {
            eprintln!("verify <request.json>");
            return ExitCode::from(2);
        }
    };
    let body = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("cannot read {path}: {e}");
            return ExitCode::FAILURE;
        }
    };
    let resp = api::verify_body(&body);
    let _ = print_json(&resp.body);
    let parsed = json::parse(&String::from_utf8_lossy(&resp.body)).unwrap();
    if parsed.get("valid").and_then(Json::as_bool) == Some(true) {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

fn cmd_serve(args: &[String]) -> ExitCode {
    let dir = match args.first() {
        Some(d) => d,
        None => return ExitCode::from(2),
    };
    let addr = arg(args, "--addr").unwrap_or_else(|| "127.0.0.1:8080".to_string());
    let store = match Store::open(RealVfs::new(), &PathBuf::from(dir)) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot open {dir}: {e}");
            return ExitCode::FAILURE;
        }
    };
    let listener = match std::net::TcpListener::bind(&addr) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("cannot bind {addr}: {e}");
            return ExitCode::FAILURE;
        }
    };
    eprintln!("merkle-store serving {dir} on http://{addr} (one request per connection)");
    if let Err(e) = http::serve(listener, RepoHandler::shared(store)) {
        eprintln!("server error: {e}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

fn cmd_req(args: &[String]) -> ExitCode {
    let (addr, method, path) = match (args.first(), args.get(1), args.get(2)) {
        (Some(a), Some(m), Some(p)) => (a, m, p),
        _ => {
            eprintln!("req <addr> <METHOD> <path> [--data <file>]");
            return ExitCode::from(2);
        }
    };
    let body = match arg(args, "--data") {
        Some(file) => match read_file(&file) {
            Ok(b) => b,
            Err(e) => {
                eprintln!("cannot read {file}: {e}");
                return ExitCode::FAILURE;
            }
        },
        None => Vec::new(),
    };
    let mut stream = match TcpStream::connect(addr) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("connect: {e}");
            return ExitCode::FAILURE;
        }
    };
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    if stream.write_all(req.as_bytes()).is_err() || stream.write_all(&body).is_err() {
        eprintln!("write failed");
        return ExitCode::FAILURE;
    }
    let mut raw = Vec::new();
    match stream.read_to_end(&mut raw) {
        Ok(_) => {}
        Err(e) => {
            eprintln!("read failed: {e}");
            return ExitCode::FAILURE;
        }
    }
    // Strip status/headers; print body.
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .map(|p| p + 4)
        .unwrap_or(0);
    let status_ok = raw
        .windows(12)
        .next()
        .map(|s| s.starts_with(b"HTTP/1.1 200"))
        .unwrap_or(false);
    let _ = print_json(&raw[split..]);
    if status_ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let (cmd, rest) = match argv.split_first() {
        Some((c, r)) => (c.as_str(), r.to_vec()),
        None => {
            eprintln!("{}", usage());
            return ExitCode::from(2);
        }
    };
    match cmd {
        "init" => cmd_init(&rest),
        "build" => cmd_build(&rest),
        "put" => cmd_put(&rest),
        "root" => cmd_root(&rest),
        "range" => cmd_range(&rest),
        "verify" => cmd_verify(&rest),
        "serve" => cmd_serve(&rest),
        "req" => cmd_req(&rest),
        "-h" | "--help" | "help" => {
            println!("{}", usage());
            ExitCode::SUCCESS
        }
        other => {
            eprintln!("unknown command: {other}\n\n{}", usage());
            ExitCode::from(2)
        }
    }
}
