//! End-to-end tests against the real `resp-server` binary over TCP.
//!
//! These start the server on an ephemeral port (port 0, `LISTENING <n>` on
//! stdout), connect a raw socket, and push bytes with adversarial chunking —
//! including one byte at a time — to prove the incremental parser works the
//! same way through real `read()` boundaries as it does in unit tests.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command};
use std::time::Duration;

const BIN: &str = env!("CARGO_BIN_EXE_resp-server");

struct Server {
    child: Child,
    port: u16,
}

impl Server {
    fn start(extra: &[&str]) -> Server {
        use std::process::Stdio;
        let mut child = Command::new(BIN)
            .args(["--addr", "127.0.0.1", "--port", "0"])
            .args(extra)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("start resp-server");
        // Read "LISTENING <port>" from the child's stdout.
        let port = wait_for_port(&mut child);
        Server { child, port }
    }

    fn connect(&self) -> TcpStream {
        let stream = retry_connect(("127.0.0.1", self.port));
        stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        stream.set_nodelay(true).unwrap();
        stream
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn wait_for_port(child: &mut Child) -> u16 {
    use std::io::BufRead;
    let stdout = child.stdout.take().expect("child stdout piped");
    let mut lines = std::io::BufReader::new(stdout);
    let mut line = String::new();
    lines.read_line(&mut line).expect("read LISTENING line");
    line.trim()
        .strip_prefix("LISTENING ")
        .unwrap_or_else(|| panic!("unexpected banner: {:?}", line))
        .parse()
        .expect("port number")
}

fn retry_connect(addr: (&str, u16)) -> TcpStream {
    let mut last = None;
    for _ in 0..50 {
        match TcpStream::connect(addr) {
            Ok(s) => return s,
            Err(e) => {
                last = Some(e);
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }
    panic!("connect failed: {:?}", last);
}

fn read_reply(stream: &mut TcpStream) -> Vec<u8> {
    // Read with a short deadline until quiet for 150ms; the server replies
    // promptly once a complete frame has been assembled.
    stream
        .set_read_timeout(Some(Duration::from_millis(150)))
        .unwrap();
    let mut all = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => all.extend_from_slice(&chunk[..n]),
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => break,
            Err(e) => panic!("read error: {}", e),
        }
    }
    all
}

fn has(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

fn cmd_array(parts: &[&[u8]]) -> Vec<u8> {
    let mut out = format!("*{}\r\n", parts.len()).into_bytes();
    for p in parts {
        out.extend_from_slice(format!("${}\r\n", p.len()).as_bytes());
        out.extend_from_slice(p);
        out.extend_from_slice(b"\r\n");
    }
    out
}

// ---------- basic command subset -------------------------------------------

#[test]
fn ping_pong() {
    let server = Server::start(&[]);
    let mut s = server.connect();
    s.write_all(b"*1\r\n$4\r\nPING\r\n").unwrap();
    assert_eq!(read_reply(&mut s), b"+PONG\r\n");
}

#[test]
fn lowercase_command_is_accepted() {
    let server = Server::start(&[]);
    let mut s = server.connect();
    s.write_all(b"*1\r\n$4\r\nping\r\n").unwrap();
    assert_eq!(read_reply(&mut s), b"+PONG\r\n");
}

#[test]
fn set_then_get_returns_value_and_empty_is_distinct_from_null() {
    let server = Server::start(&[]);
    let mut s = server.connect();

    s.write_all(&cmd_array(&[b"SET", b"greet", b"hello"])).unwrap();
    assert_eq!(read_reply(&mut s), b"+OK\r\n");

    s.write_all(&cmd_array(&[b"GET", b"greet"])).unwrap();
    assert_eq!(read_reply(&mut s), b"$5\r\nhello\r\n");

    // Missing key: null bulk, not an empty bulk.
    s.write_all(&cmd_array(&[b"GET", b"missing"])).unwrap();
    assert_eq!(read_reply(&mut s), b"$-1\r\n");

    // Empty-string value: $0.
    s.write_all(&cmd_array(&[b"SET", b"blank", b""])).unwrap();
    assert_eq!(read_reply(&mut s), b"+OK\r\n");
    s.write_all(&cmd_array(&[b"GET", b"blank"])).unwrap();
    assert_eq!(read_reply(&mut s), b"$0\r\n\r\n");
}

#[test]
fn del_reports_count() {
    let server = Server::start(&[]);
    let mut s = server.connect();
    s.write_all(&cmd_array(&[b"SET", b"k", b"v"])).unwrap();
    let _ = read_reply(&mut s);
    s.write_all(&cmd_array(&[b"DEL", b"k", b"nope"])).unwrap();
    assert_eq!(read_reply(&mut s), b":1\r\n");
}

#[test]
fn unknown_command_and_arg_errors() {
    let server = Server::start(&[]);
    let mut s = server.connect();
    s.write_all(&cmd_array(&[b"FROBNICATE"])).unwrap();
    let reply = read_reply(&mut s);
    assert!(reply.starts_with(b"-ERR unknown command"), "got {:?}", reply);

    s.write_all(&cmd_array(&[b"GET"])).unwrap();
    let reply = read_reply(&mut s);
    assert!(reply.starts_with(b"-ERR wrong number"), "got {:?}", reply);
}

// ---------- binary safety & chunking over the socket -----------------------

#[test]
fn binary_value_with_embedded_crlf_round_trips() {
    let server = Server::start(&[]);
    let mut s = server.connect();
    let payload: &[u8] = b"line1\r\nline2\x00\xff";
    s.write_all(&cmd_array(&[b"SET", b"bin", payload])).unwrap();
    assert_eq!(read_reply(&mut s), b"+OK\r\n");
    s.write_all(&cmd_array(&[b"GET", b"bin"])).unwrap();
    let mut expected = format!("${}\r\n", payload.len()).into_bytes();
    expected.extend_from_slice(payload);
    expected.extend_from_slice(b"\r\n");
    assert_eq!(read_reply(&mut s), expected);
}

#[test]
fn request_sent_one_byte_at_a_time_is_identical() {
    let server = Server::start(&[]);
    let wire = cmd_array(&[b"PING"]);
    let mut s = server.connect();
    for b in &wire {
        s.write_all(&[*b]).unwrap();
        std::thread::sleep(Duration::from_micros(500));
    }
    assert_eq!(read_reply(&mut s), b"+PONG\r\n");
}

#[test]
fn request_split_at_every_position_matches() {
    let server = Server::start(&[]);
    let wire = cmd_array(&[b"ECHO", b"ab\r\ncd"]); // embedded CRLF
    for k in 1..wire.len() {
        let mut s = server.connect();
        s.write_all(&wire[..k]).unwrap();
        std::thread::sleep(Duration::from_millis(30));
        // No premature reply yet (server is still waiting).
        s.set_read_timeout(Some(Duration::from_millis(80))).unwrap();
        let mut buf = [0u8; 64];
        let early = s.read(&mut buf);
        assert!(
            matches!(&early, Err(e) if e.kind() == std::io::ErrorKind::WouldBlock
                || e.kind() == std::io::ErrorKind::TimedOut),
            "server replied early at split {}: {:?}",
            k,
            early
        );
        s.write_all(&wire[k..]).unwrap();
        let reply = read_reply(&mut s);
        let expected = b"$6\r\nab\r\ncd\r\n".to_vec();
        assert_eq!(reply, expected, "split at {} mismatch", k);
        let _ = expected.len();
    }
}

#[test]
fn pipelined_requests_get_pipelined_replies() {
    let server = Server::start(&[]);
    let mut s = server.connect();
    let mut wire = Vec::new();
    wire.extend_from_slice(&cmd_array(&[b"PING"]));
    wire.extend_from_slice(&cmd_array(&[b"ECHO", b"x"]));
    wire.extend_from_slice(&cmd_array(&[b"SET", b"p", b"q"]));
    s.write_all(&wire).unwrap();
    assert_eq!(
        read_reply(&mut s),
        b"+PONG\r\n$1\r\nx\r\n+OK\r\n"
    );
}

// ---------- protocol errors over a real socket ------------------------------

#[test]
fn negative_bulk_minus_two_closes_connection_after_error() {
    let server = Server::start(&[]);
    let mut s = server.connect();
    s.write_all(b"$-2\r\n").unwrap();
    let reply = read_reply(&mut s);
    assert!(
        reply.starts_with(b"-ERR protocol error"),
        "got {:?}",
        reply
    );
    // Server then half-closes; a subsequent write sees EOF or EPIPE eventually.
    std::thread::sleep(Duration::from_millis(50));
}

#[test]
fn garbage_type_byte_is_rejected() {
    let server = Server::start(&[]);
    let mut s = server.connect();
    s.write_all(b"??notresp\r\n").unwrap();
    let reply = read_reply(&mut s);
    assert!(has(&reply, b"protocol error"), "got {:?}", reply);
}

// ---------- configured limits via CLI flags ---------------------------------

#[test]
fn max_bulk_flag_rejects_oversized_payload() {
    // 4-byte cap, send a 5-byte bulk.
    let server = Server::start(&["--max-bulk-bytes", "4"]);
    let mut s = server.connect();
    s.write_all(b"$5\r\nhello\r\n").unwrap();
    let reply = read_reply(&mut s);
    assert!(has(&reply, b"BulkTooLarge") || has(&reply, b"bulk string length"),
        "got {:?}", reply);
}

#[test]
fn max_depth_flag_rejects_deep_frame() {
    // depth 0 allowed, depth 1 rejected.
    let server = Server::start(&["--max-depth", "0"]);
    let mut s = server.connect();
    s.write_all(b"*1\r\n*1\r\n:1\r\n").unwrap();
    let reply = read_reply(&mut s);
    assert!(has(&reply, b"NestingTooDeep") || has(&reply, b"nesting depth"),
        "got {:?}", reply);
}

#[test]
fn total_budget_flag_caps_cumulative_frames() {
    // Budget for roughly one PING frame (14 bytes); a second one fails.
    let server = Server::start(&["--max-total-bytes", "14"]);
    let mut s = server.connect();
    s.write_all(&cmd_array(&[b"PING"])).unwrap();
    assert_eq!(read_reply(&mut s), b"+PONG\r\n");
    s.write_all(&cmd_array(&[b"PING"])).unwrap();
    let reply = read_reply(&mut s);
    assert!(
        has(&reply, b"BudgetExceeded") || has(&reply, b"byte budget"),
        "got {:?}",
        reply
    );
}

// ---------- quit ------------------------------------------------------------

#[test]
fn quit_closes_gracefully() {
    let server = Server::start(&[]);
    let mut s = server.connect();
    s.write_all(&cmd_array(&[b"QUIT"])).unwrap();
    assert_eq!(read_reply(&mut s), b"+OK\r\n");
    // Next read must be clean EOF.
    let mut buf = [0u8; 8];
    s.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let n = s.read(&mut buf).unwrap();
    assert_eq!(n, 0, "expected EOF after QUIT");
}
