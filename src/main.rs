//! Local TCP test server for the TLS record observer.
//!
//! It accepts raw TCP connections, reads **client-to-server** bytes only
//! (exactly the direction a real ClientHello travels), feeds them to the
//! incremental [`Observer`], and returns a single JSON observation when the
//! client half-closes (or after an idle timeout). It never replies to the
//! TLS peer and performs no handshake or decryption.
//!
//! Usage:
//!
//! ```text
//! tls-observer-server [--bind 127.0.0.1] [--port 8443]
//!                     [--idle-timeout-ms 2000] [--max-bytes 1048576]
//! ```

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::Duration;

use tls_record_observer::{Limits, Observation, Observer, ParseError};

fn main() {
    let mut bind = "127.0.0.1".to_string();
    let mut port: u16 = 8443;
    let mut idle_timeout_ms: u64 = 2000;
    let mut max_bytes: usize = 4 << 20; // 4 MiB hard per-connection cap

    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        let value = |name: &str, args: &mut dyn Iterator<Item = String>| -> String {
            args.next().unwrap_or_else(|| {
                eprintln!("error: option {name} requires a value");
                std::process::exit(2);
            })
        };
        match a.as_str() {
            "--bind" => bind = value("--bind", &mut args),
            "--port" => {
                port = value("--port", &mut args).parse().unwrap_or_else(|_| {
                    eprintln!("error: invalid --port");
                    std::process::exit(2);
                })
            }
            "--idle-timeout-ms" => {
                idle_timeout_ms = value("--idle-timeout-ms", &mut args)
                    .parse()
                    .unwrap_or_else(|_| {
                        eprintln!("error: invalid --idle-timeout-ms");
                        std::process::exit(2);
                    })
            }
            "--max-bytes" => {
                max_bytes = value("--max-bytes", &mut args).parse().unwrap_or_else(|_| {
                    eprintln!("error: invalid --max-bytes");
                    std::process::exit(2);
                })
            }
            "-h" | "--help" => {
                println!(
                    "tls-observer-server — local TLS record observation service\n\
                     \n\
                     USAGE:\n    tls-observer-server [--bind ADDR] [--port PORT]\n\
                     \x20       [--idle-timeout-ms MS] [--max-bytes N]\n\
                     \n\
                     Reads client-side TLS bytes from each TCP connection and\n\
                     replies with one JSON observation. Passive only: no handshake,\n\
                     no decryption."
                );
                return;
            }
            other => {
                eprintln!("error: unknown argument {other}");
                std::process::exit(2);
            }
        }
    }

    let addr = format!("{bind}:{port}");
    let listener = TcpListener::bind(&addr).unwrap_or_else(|e| {
        eprintln!("error: cannot bind {addr}: {e}");
        std::process::exit(1);
    });
    eprintln!("tls-record-observer listening on {addr} (passive, no decryption)");

    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let peer = s
                    .peer_addr()
                    .map(|a| a.to_string())
                    .unwrap_or_else(|_| "?".to_string());
                let json = handle(s, idle_timeout_ms, max_bytes);
                println!("--- observation from {peer} ---\n{json}");
            }
            Err(e) => eprintln!("accept failed: {e}"),
        }
    }
}

fn handle(mut stream: TcpStream, idle_timeout_ms: u64, max_bytes: usize) -> String {
    let _ = stream.set_read_timeout(Some(Duration::from_millis(idle_timeout_ms)));
    let _ = stream.set_nodelay(true);

    let limits = Limits::default();
    let mut observer = Observer::with_limits(limits);
    let mut buf = [0u8; 8192];
    let mut total = 0usize;
    let mut fatal: Option<ParseError> = None;

    // Read until the peer half-closes (WouldBlock timeout) or a hard cap /
    // fatal parse error is hit. On fatal error we stop *parsing* but keep
    // draining until EOF so that our JSON reply is written after the peer's
    // FIN — replying before the half-close would cause a connection reset.
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                total += n;
                if total > max_bytes {
                    eprintln!("connection exceeded max-bytes cap; stopping read");
                    break;
                }
                if fatal.is_none() {
                    if let Err(e) = observer.push(&buf[..n]) {
                        fatal = Some(e);
                    }
                }
            }
            Err(ref e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                break
            }
            Err(_) => break,
        }
    }

    // Consume the observer: finish() reports any half-record /
    // half-message left at stream end, and returns the partial
    // observation together with the error.
    let (finish_err, finished_obs) = match observer.finish() {
        Ok(obs) => (None, obs),
        Err((e, obs)) => (Some(e), obs),
    };
    let err = fatal.or(finish_err);

    let json = render_json(&finished_obs, err.as_ref());
    let _ = stream.write_all(json.as_bytes());
    let _ = stream.write_all(b"\n");
    let _ = stream.flush();
    json
}

// ---------------------------------------------------------------------------
// Minimal JSON serializer (dependency-free).
// ---------------------------------------------------------------------------

fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn q(s: &str) -> String {
    format!("\"{}\"", json_escape(s))
}

fn render_json(o: &Observation, err: Option<&ParseError>) -> String {
    let mut s = String::new();
    s.push_str("{\n");
    s.push_str(&format!("  \"ok\": {},\n", err.is_none()));
    s.push_str(&format!("  \"bytes_in\": {},\n", o.bytes_in));
    s.push_str(&format!("  \"encrypted\": {},\n", o.encrypted));

    s.push_str("  \"counts\": {\n");
    s.push_str(&format!("    \"records\": {},\n", o.records.len()));
    s.push_str(&format!("    \"handshake\": {},\n", o.handshake_records));
    s.push_str(&format!(
        "    \"change_cipher_spec\": {},\n",
        o.change_cipher_spec_records
    ));
    s.push_str(&format!("    \"alert\": {},\n", o.alert_records));
    s.push_str(&format!(
        "    \"application_data\": {}\n",
        o.application_data_records
    ));
    s.push_str("  },\n");

    // records
    s.push_str("  \"records\": [");
    if o.records.is_empty() {
        s.push_str("],\n");
    } else {
        s.push('\n');
        let rendered: Vec<String> = o
            .records
            .iter()
            .map(|r| {
                format!(
                    "    {{\"index\": {}, \"content_type\": {}, \"content_type_name\": {}, \"legacy_version\": \"{}.{}\", \"fragment_length\": {}}}",
                    r.index,
                    r.content_type,
                    q(r.content_type_name),
                    r.legacy_version.0,
                    r.legacy_version.1,
                    r.fragment_length
                )
            })
            .collect();
        s.push_str(&rendered.join(",\n"));
        s.push_str("\n  ],\n");
    }

    // client_hello
    match &o.client_hello {
        None => s.push_str("  \"client_hello\": null,\n"),
        Some(ch) => {
            s.push_str("  \"client_hello\": {\n");
            s.push_str(&format!(
                "    \"client_version\": \"{}.{}\",\n",
                ch.client_version.0, ch.client_version.1
            ));
            s.push_str(&format!("    \"random_hex\": {},\n", q(&ch.random_hex)));
            s.push_str(&format!(
                "    \"session_id_hex\": {},\n",
                q(&ch.session_id_hex)
            ));
            s.push_str("    \"cipher_suites\": [");
            s.push_str(
                &ch.cipher_suites
                    .iter()
                    .map(|v| format!("\"{v:#06x}\""))
                    .collect::<Vec<_>>()
                    .join(", "),
            );
            s.push_str("],\n");
            s.push_str("    \"grease_cipher_suites\": [");
            s.push_str(
                &ch.grease_cipher_suites
                    .iter()
                    .map(|v| format!("\"{v:#06x}\""))
                    .collect::<Vec<_>>()
                    .join(", "),
            );
            s.push_str("],\n");
            s.push_str("    \"compression_methods\": [");
            s.push_str(
                &ch.compression_methods
                    .iter()
                    .map(|v| v.to_string())
                    .collect::<Vec<_>>()
                    .join(", "),
            );
            s.push_str("],\n");
            s.push_str(&format!(
                "    \"sni\": {},\n",
                ch.sni
                    .as_deref()
                    .map(q)
                    .unwrap_or_else(|| "null".to_string())
            ));
            s.push_str("    \"alpn\": [");
            s.push_str(&ch.alpn.iter().map(|a| q(a)).collect::<Vec<_>>().join(", "));
            s.push_str("],\n");
            s.push_str("    \"unknown_extensions\": [");
            if ch.unknown_extensions.is_empty() {
                s.push_str("]\n");
            } else {
                s.push('\n');
                let rendered: Vec<String> = ch
                    .unknown_extensions
                    .iter()
                    .map(|u| {
                        format!(
                            "      {{\"type\": \"{:#06x}\", \"data_len\": {}, \"data_hex\": {}}}",
                            u.type_,
                            u.data_len,
                            q(&u.data_hex)
                        )
                    })
                    .collect();
                s.push_str(&rendered.join(",\n"));
                s.push_str("\n    ]\n");
            }
            s.push_str("  },\n");
        }
    }

    // warnings
    s.push_str("  \"warnings\": [");
    if o.warnings.is_empty() {
        s.push_str("],\n");
    } else {
        s.push('\n');
        let rendered: Vec<String> = o
            .warnings
            .iter()
            .map(|w| format!("    {}", q(&w.to_string())))
            .collect();
        s.push_str(&rendered.join(",\n"));
        s.push_str("\n  ],\n");
    }

    // error
    match err {
        None => s.push_str("  \"error\": null\n"),
        Some(e) => {
            let (type_, layer) = error_meta(e);
            s.push_str("  \"error\": {\n");
            s.push_str(&format!("    \"type\": {},\n", q(type_)));
            s.push_str(&format!("    \"layer\": {},\n", q(layer)));
            s.push_str(&format!("    \"message\": {}\n", q(&e.to_string())));
            s.push_str("  }\n");
        }
    }

    s.push('}');
    s
}

fn error_meta(e: &ParseError) -> (&'static str, &'static str) {
    use tls_record_observer::ParseError::*;
    match e {
        Truncated { layer, .. } => ("truncated", layer.as_str()),
        BadContentType(_) => ("bad_content_type", "record_header"),
        InvalidRecordVersion { .. } => ("invalid_record_version", "record_header"),
        RecordTooLarge { .. } => ("record_too_large", "record"),
        HandshakeTooLarge { .. } => ("handshake_too_large", "handshake_header"),
        UnsupportedHandshakeType(_) => ("unsupported_handshake_type", "handshake_header"),
        InvalidClientHelloVersion { .. } => ("invalid_client_hello_version", "handshake_body"),
        LengthMismatch { layer, .. } => ("length_mismatch", layer.as_str()),
        MalformedVector { layer, .. } => ("malformed_vector", layer.as_str()),
    }
}
