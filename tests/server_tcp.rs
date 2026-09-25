//! End-to-end tests for the local TCP reference service.
//!
//! The listener binds port 0 (ephemeral); the server is forced to a
//! one-byte `read()` so these tests exercise the incremental paths
//! over a real socket.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::thread;
use std::time::Duration;

use http_framing::server::{serve_listener, ServerConfig};
use http_framing::Limits;

struct TestServer {
    addr: std::net::SocketAddr,
    // Kept alive for the test's lifetime (the accept loop owns the
    // listener itself; this Arc just documents the ownership).
    _handle: thread::JoinHandle<()>,
}

fn spawn_server(read_size: usize, limits: Limits) -> TestServer {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let config = ServerConfig {
        addr: addr.to_string(),
        limits,
        read_size,
    };
    let handle = thread::spawn(move || {
        let _ = serve_listener(listener, config);
    });
    // Give the accept loop a moment (binding itself is synchronous;
    // the loop starts immediately).
    thread::sleep(Duration::from_millis(30));
    TestServer {
        addr,
        _handle: handle,
    }
}

fn connect(addr: std::net::SocketAddr) -> TcpStream {
    let s = TcpStream::connect(addr).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    s.set_nodelay(true).unwrap();
    s
}

fn read_all(s: &mut TcpStream) -> Vec<u8> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    // Server closes after errors; after success with keep-alive it
    // waits, so drive reads until a short timeout for the keep-alive
    // cases (we then shut down the write side ourselves).
    s.set_read_timeout(Some(Duration::from_millis(300))).unwrap();
    loop {
        match s.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if n < chunk.len() {
                    // Drain anything else available without blocking
                    // long; loop again to confirm.
                }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(ref e) if e.kind() == std::io::ErrorKind::TimedOut => break,
            Err(e) => panic!("unexpected read error: {e}"),
        }
    }
    buf
}

#[test]
fn get_returns_200_json_over_bytewise_reads() {
    let server = spawn_server(1, Limits::default());
    let mut s = connect(server.addr);
    s.write_all(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n").unwrap();
    s.flush().unwrap();
    let out = String::from_utf8(read_all(&mut s)).unwrap();
    assert!(out.starts_with("HTTP/1.1 200 OK\r\n"), "got: {out:?}");
    assert!(out.contains("\"method\":\"GET\""));
    assert!(out.contains("\"target\":\"/\""));
    assert!(out.contains("X-Framing: none"));
}

#[test]
fn pipelined_requests_each_get_a_response() {
    let server = spawn_server(3, Limits::default());
    let mut s = connect(server.addr);
    let wire = b"GET /1 HTTP/1.1\r\nHost: x\r\n\r\n\
                POST /2 HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\nbody\
                GET /3 HTTP/1.1\r\nHost: x\r\n\r\n";
    // Deliver one byte at a time from the client too.
    for &b in wire {
        s.write_all(&[b]).unwrap();
    }
    s.flush().unwrap();
    let out = String::from_utf8(read_all(&mut s)).unwrap();
    assert_eq!(out.matches("HTTP/1.1 200 OK").count(), 3);
    assert!(out.contains("\"target\":\"/1\""));
    assert!(out.contains("\"body_hex\":\"626f6479\""));
    assert!(out.contains("\"target\":\"/3\""));
}

#[test]
fn chunked_response_shows_decoded_hex_and_trailers() {
    let server = spawn_server(2, Limits::default());
    let mut s = connect(server.addr);
    s.write_all(
        b"POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
          3\r\nfoo\r\n0\r\nX-T: v\r\n\r\n",
    )
    .unwrap();
    let out = String::from_utf8(read_all(&mut s)).unwrap();
    assert!(out.starts_with("HTTP/1.1 200 OK"));
    assert!(out.contains("X-Framing: chunked"));
    assert!(out.contains("\"body_hex\":\"666f6f\""));
    assert!(out.contains("\"X-T\",\"v\""));
}

#[test]
fn smuggling_attempt_gets_400_and_closes() {
    let server = spawn_server(1, Limits::default());
    let mut s = connect(server.addr);
    s.write_all(
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 6\r\n\
          Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n\
          GET /admin HTTP/1.1\r\nHost: x\r\n\r\n",
    )
    .unwrap();
    s.shutdown(std::net::Shutdown::Write).unwrap();
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).unwrap(); // server closes the connection
    let out = String::from_utf8(buf).unwrap();
    assert!(out.starts_with("HTTP/1.1 400 Bad Request"));
    assert!(out.contains("X-Frame-Error: te-with-content-length"));
    assert_eq!(out.matches("HTTP/1.1 200").count(), 0);
    assert!(out.contains("Connection: close"));
}

#[test]
fn oversized_header_returns_431() {
    let limits = Limits {
        max_request_line_bytes: 16,
        ..Limits::default()
    };
    let server = spawn_server(1, limits);
    let mut s = connect(server.addr);
    s.write_all(b"GET /aaaaaaaaaaaaaaaaaaaa HTTP/1.1\r\n\r\n")
        .unwrap();
    s.shutdown(std::net::Shutdown::Write).unwrap();
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).unwrap();
    let out = String::from_utf8(buf).unwrap();
    assert!(out.starts_with("HTTP/1.1 431"));
    assert!(out.contains("X-Frame-Error: request-line-too-large"));
}

#[test]
fn partial_request_on_eof_returns_400() {
    let server = spawn_server(1, Limits::default());
    let mut s = connect(server.addr);
    s.write_all(b"POST / HTTP/1.1\r\nContent-Length: 10\r\n\r\nabc")
        .unwrap();
    s.flush().unwrap();
    // Half-close the write side so the server's reads hit EOF while
    // the read side stays open for its response.
    s.shutdown(std::net::Shutdown::Write).unwrap();
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).unwrap();
    let out = String::from_utf8(buf).unwrap();
    assert!(
        out.starts_with("HTTP/1.1 400"),
        "expected 400 on truncated request, got: {out:?}"
    );
}

#[test]
fn stdio_mode_via_handle_helper() {
    // Exercise the IO-agnostic handler directly with in-memory pipes:
    // same code path the TCP server and --stdio use.
    use http_framing::server::handle;
    let config = ServerConfig {
        read_size: 2,
        ..ServerConfig::default()
    };
    let input: &[u8] = b"GET /x HTTP/1.1\r\nHost: h\r\n\r\n";
    let mut output: Vec<u8> = Vec::new();
    handle(input, &mut output, &config).unwrap();
    let out = String::from_utf8(output).unwrap();
    assert!(out.contains("HTTP/1.1 200 OK"));
    assert!(out.contains("\"target\":\"/x\""));
}
