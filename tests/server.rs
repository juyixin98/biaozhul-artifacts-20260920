//! Integration test for the `resp-server` binary: spawns it on an ephemeral
//! port, sends fragmented and concatenated RESP2 frames over a real TCP
//! socket, and checks the round-trip replies.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

struct ServerGuard(Child);

impl Drop for ServerGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn spawn_server() -> (ServerGuard, String) {
    // Bind port 0 to get a free port, release it, then start the server on
    // it. (Small race window, acceptable for a local test.)
    let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = probe.local_addr().unwrap().port();
    drop(probe);

    let child = Command::new(env!("CARGO_BIN_EXE_resp-server"))
        .args(["--addr", &format!("127.0.0.1:{}", port)])
        .args(["--max-depth", "8"])
        .args(["--max-buffer-bytes", "65536"])
        .stderr(Stdio::null())
        .spawn()
        .expect("failed to spawn resp-server");

    // Wait until the port accepts connections.
    let addr = format!("127.0.0.1:{}", port);
    for _ in 0..100 {
        if TcpStream::connect(&addr).is_ok() {
            return (ServerGuard(child), addr);
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("server did not start listening");
}

/// Read exactly `n` bytes with a deadline.
fn read_exact(stream: &mut TcpStream, n: usize) -> Vec<u8> {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut buf = vec![0u8; n];
    stream.read_exact(&mut buf).expect("read_exact failed");
    buf
}

#[test]
fn server_round_trips_fragmented_and_pipelined_frames() {
    let (_guard, addr) = spawn_server();
    let mut s = TcpStream::connect(&addr).unwrap();

    // 1. A command fragmented mid-bulk, byte by byte.
    let cmd = b"*2\r\n$4\r\nECHO\r\n$5\r\nhe";
    for b in cmd {
        s.write_all(&[*b]).unwrap();
    }
    s.write_all(b"llo\r\n").unwrap();
    let reply = read_exact(&mut s, cmd.len() + 5); // cmd + "llo\r\n"
    assert_eq!(reply, b"*2\r\n$4\r\nECHO\r\n$5\r\nhello\r\n");

    // 2. Several messages in one write; server replies to each in order.
    s.write_all(b"+PING\r\n:42\r\n$-1\r\n").unwrap();
    let reply = read_exact(&mut s, b"+PING\r\n:42\r\n$-1\r\n".len());
    assert_eq!(reply, b"+PING\r\n:42\r\n$-1\r\n");

    // 3. Malformed input -> RESP error frame, then the connection keeps
    //    working (parser was reset).
    s.write_all(b"$-2\r\n").unwrap();
    let reply = read_exact(&mut s, b"-ERR invalid negative length -2\r\n".len());
    assert_eq!(reply, b"-ERR invalid negative length -2\r\n");

    s.write_all(b"+STILL-ALIVE\r\n").unwrap();
    let reply = read_exact(&mut s, b"+STILL-ALIVE\r\n".len());
    assert_eq!(reply, b"+STILL-ALIVE\r\n");
}
