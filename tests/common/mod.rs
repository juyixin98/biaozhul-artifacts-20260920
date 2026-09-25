//! Shared helpers for integration tests: spin a server on an ephemeral port.
#![allow(dead_code)]

use std::sync::Arc;

use brpc::server::{Server, ServerConfig};

pub fn test_server() -> Arc<Server> {
    let cfg = ServerConfig {
        max_payload: 4096,
        max_inflight: 8, // tight on purpose: backpressure test needs it
        ..ServerConfig::default()
    };
    Arc::new(Server::bind_with("127.0.0.1:0", cfg).expect("bind server"))
}

pub fn test_server_large_inflight() -> Arc<Server> {
    let cfg = ServerConfig {
        max_payload: 4096,
        max_inflight: 256,
        ..ServerConfig::default()
    };
    Arc::new(Server::bind_with("127.0.0.1:0", cfg).expect("bind server"))
}

pub fn delay(ms: u32, tail: &[u8]) -> Vec<u8> {
    let mut v = ms.to_be_bytes().to_vec();
    v.extend_from_slice(tail);
    v
}

/// Retry a closure until it returns Some or time runs out (avoids sleeps that
/// assume scheduler timing).
pub fn wait_until<F: Fn() -> bool>(timeout: std::time::Duration, f: F) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if f() {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
    false
}
