//! Binary entry point for the serial-frame replay service.

use std::net::SocketAddr;

use serial_frame_parser::{http, DEFAULT_MAX_PAYLOAD};

#[tokio::main]
async fn main() {
    // Bind to 127.0.0.1 by default; override with HOST/PORT.
    let host = std::env::var("HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8080);
    let max_payload: usize = std::env::var("MAX_PAYLOAD")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_MAX_PAYLOAD);

    let app = http::app(max_payload);
    let addr: SocketAddr = format!("{host}:{port}")
        .parse()
        .expect("invalid HOST:PORT");

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("failed to bind listener");
    println!(
        "serial-frame replay service on http://{addr} (max_payload={max_payload})"
    );
    axum::serve(listener, app).await.expect("server error");
}
