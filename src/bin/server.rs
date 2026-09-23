//! Local TCP test server for the hand-written incremental RESP2 parser.
//!
//! It is intentionally tiny: an in-memory PING/ECHO/SET/GET/DEL/COMMAND/QUIT
//! subset, one OS thread per connection, no third-party crates. Its purpose is
//! to exercise the parser against real sockets with arbitrary TCP chunking —
//! not to be Redis.
//!
//! Usage:
//!
//! ```text
//! resp-server [--addr 127.0.0.1] [--port 6379] [--max-bulk-bytes N]
//!             [--max-depth N] [--max-total-bytes N]
//! ```
//!
//! `--port 0` binds an ephemeral port and prints `LISTENING <port>` on stdout.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use resp_incremental::{error_reply, simple, Config, ParseError, Parser, Poll, Value};

struct Args {
    addr: String,
    port: u16,
    config: Config,
}

fn parse_args() -> Args {
    let mut args = Args {
        addr: "127.0.0.1".to_string(),
        port: 6379,
        config: Config::default(),
    };
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        let value = it.next().unwrap_or_default();
        let parse_usize = |s: &str| -> usize {
            s.parse().unwrap_or_else(|_| {
                eprintln!("invalid number for {}: {}", flag, s);
                std::process::exit(2);
            })
        };
        match flag.as_str() {
            "--addr" => args.addr = value,
            "--port" => args.port = value.parse().unwrap_or(6379),
            "--max-bulk-bytes" => args.config.max_bulk_length = parse_usize(&value),
            "--max-array-len" => args.config.max_array_length = parse_usize(&value),
            "--max-depth" => args.config.max_depth = parse_usize(&value),
            "--max-line-len" => args.config.max_line_length = parse_usize(&value),
            "--max-total-bytes" => args.config.max_total_bytes = parse_usize(&value),
            "-h" | "--help" => {
                println!(
                    "resp-server [--addr 127.0.0.1] [--port 6379]\n\
                     \x20           [--max-bulk-bytes N] [--max-array-len N]\n\
                     \x20           [--max-depth N] [--max-line-len N]\n\
                     \x20           [--max-total-bytes N]\n\
                     Port 0 = ephemeral; prints 'LISTENING <port>' on stdout."
                );
                std::process::exit(0);
            }
            other => {
                eprintln!("unknown argument: {}", other);
                std::process::exit(2);
            }
        }
    }
    args
}

fn main() {
    let args = parse_args();
    let listener = TcpListener::bind((args.addr.as_str(), args.port))
        .unwrap_or_else(|e| panic!("bind {}:{} failed: {}", args.addr, args.port, e));
    let port = listener.local_addr().expect("local_addr").port();
    println!("LISTENING {}", port);
    println!(
        "# limits: max_bulk={} max_array={} max_depth={} max_line={} max_total={}",
        args.config.max_bulk_length,
        args.config.max_array_length,
        args.config.max_depth,
        args.config.max_line_length,
        args.config.max_total_bytes
    );
    std::io::stdout().flush().ok();

    let db: Arc<Mutex<HashMap<Vec<u8>, Vec<u8>>>> = Arc::new(Mutex::new(HashMap::new()));
    let config = Arc::new(args.config);

    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let db = Arc::clone(&db);
                let config = Arc::clone(&config);
                thread::spawn(move || {
                    if let Err(e) = handle_connection(stream, db, config) {
                        eprintln!("connection error: {}", e);
                    }
                });
            }
            Err(e) => eprintln!("accept failed: {}", e),
        }
    }
}

fn handle_connection(
    mut stream: TcpStream,
    db: Arc<Mutex<HashMap<Vec<u8>, Vec<u8>>>>,
    config: Arc<Config>,
) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(30)))?;
    let peer = stream.peer_addr().ok();
    let mut parser = Parser::new((*config).clone());
    let mut read_buf = [0u8; 4096];

    loop {
        // First, drain any frames already fully buffered (pipelines).
        loop {
            match parser.try_next() {
                Poll::Ready(value) => {
                    let reply = dispatch(&value, &db);
                    write_reply(&mut stream, reply)?;
                    if is_quit(&value) {
                        return Ok(());
                    }
                }
                Poll::Pending => break,
                Poll::Error(e) => {
                    return parser_error(&mut stream, e);
                }            }
        }

        match stream.read(&mut read_buf) {
            Ok(0) => return Ok(()), // client closed
            Ok(n) => {
                if let Err(e) = parser.feed(&read_buf[..n]) {
                    return parser_error(&mut stream, e);
                }
                if let Some(addr) = peer {
                    eprintln!(
                        "read {} bytes from {} (buffer now {} B, consumed {} B)",
                        n,
                        addr,
                        parser.buffered_len(),
                        parser.consumed_bytes()
                    );
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => return Ok(()),
            Err(e) => return Err(e),
        }
    }
}

fn parser_error(stream: &mut TcpStream, e: ParseError) -> std::io::Result<()> {
    eprintln!("fatal parse error, closing connection: {}", e);
    write_reply(
        stream,
        error_reply("ERR", &format!("protocol error: {}; connection closed", e)),
    )?;
    stream.flush()?;
    // Deliberately return Ok so the caller closes the socket: RESP has no
    // self-resynchronization marker after a frame-level violation.
    Ok(())
}

fn write_reply(stream: &mut TcpStream, reply: Value) -> std::io::Result<()> {
    let mut wire = Vec::new();
    // Any reply we build is always encodable; fall back to a bulk if not.
    if reply.encode(&mut wire).is_none() {
        wire.clear();
        if let Value::Error(s) = &reply {
            Value::Bulk(s.clone().into_bytes()).encode(&mut wire);
        }
    }
    stream.write_all(&wire)?;
    stream.flush()
}

/// Command dispatch over the tiny supported subset.
fn dispatch(value: &Value, db: &Mutex<HashMap<Vec<u8>, Vec<u8>>>) -> Value {
    // Clients normally send arrays; a top-level simple/inline value is rejected.
    let items = match value {
        Value::Array(items) => items,
        Value::Null => return error_reply("ERR", "protocol error: null command"),
        _ => return error_reply("ERR", "expected a RESP array of bulk strings"),
    };
    if items.is_empty() {
        return error_reply("ERR", "empty command array");
    }

    let cmd = match items[0].as_str() {
        Some(s) => s.to_ascii_uppercase(),
        None => return error_reply("ERR", "command name must be a bulk string"),
    };
    let args = &items[1..];

    fn need_bulk<'a>(v: &'a Value, what: &str) -> Result<&'a [u8], Value> {
        match v {
            Value::Bulk(b) => Ok(b),
            Value::Null => Err(error_reply("ERR", &format!("{} must not be null", what))),
            _ => Err(error_reply("ERR", &format!("{} must be a bulk string", what))),
        }
    }

    match cmd.as_str() {
        "PING" => {
            if args.is_empty() {
                simple("PONG")
            } else if args.len() == 1 {
                Value::Bulk(args[0].as_bytes().unwrap_or_default().to_vec())
            } else {
                wrong_args("ping")
            }
        }
        "ECHO" => {
            if args.len() == 1 {
                Value::Bulk(args[0].as_bytes().unwrap_or_default().to_vec())
            } else {
                wrong_args("echo")
            }
        }
        "SET" => {
            if args.len() < 2 {
                return wrong_args("set");
            }
            let key = match need_bulk(&args[0], "key") {
                Ok(k) => k,
                Err(v) => return v,
            };
            let val = match need_bulk(&args[1], "value") {
                Ok(v) => v,
                Err(v) => return v,
            };
            db.lock().unwrap().insert(key.to_vec(), val.to_vec());
            simple("OK")
        }
        "GET" => {
            if args.len() != 1 {
                return wrong_args("get");
            }
            let key = match need_bulk(&args[0], "key") {
                Ok(k) => k,
                Err(v) => return v,
            };
            match db.lock().unwrap().get(key) {
                Some(v) => Value::Bulk(v.clone()),
                None => Value::Null,
            }
        }
        "DEL" => {
            if args.is_empty() {
                return wrong_args("del");
            }
            let mut removed = 0i64;
            let mut store = db.lock().unwrap();
            for a in args {
                let key = match need_bulk(a, "key") {
                    Ok(k) => k,
                    Err(v) => return v,
                };
                if store.remove(key).is_some() {
                    removed += 1;
                }
            }
            Value::Integer(removed)
        }
        "COMMAND" => simple("OK"), // enough to satisfy redis-cli's startup handshake
        "QUIT" => simple("OK"),
        other => error_reply("ERR", &format!("unknown command '{}'", other)),
    }
}

fn wrong_args(name: &str) -> Value {
    error_reply("ERR", &format!("wrong number of arguments for '{}' command", name))
}

fn is_quit(v: &Value) -> bool {
    matches!(v, Value::Array(items) if matches!(items.first(), Some(Value::Bulk(b)) if b.eq_ignore_ascii_case(b"quit")))
}
