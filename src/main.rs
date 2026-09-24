//! Replay server binary.
//!
//! Configuration via environment variables:
//! * `SERIAL_BIND`        — listen address (default `127.0.0.1:8080`)
//! * `SERIAL_MAX_PAYLOAD` — max accepted/declared payload length in bytes
//!   (default from the parser, 4096; capped at 65535)

use std::net::SocketAddr;
use std::process::ExitCode;

use serialframe::parser::DEFAULT_MAX_PAYLOAD;
use serialframe::server::app;

#[tokio::main]
async fn main() -> ExitCode {
    let bind = std::env::var("SERIAL_BIND").unwrap_or_else(|_| "127.0.0.1:8080".to_string());
    let addr: SocketAddr = match bind.parse() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("invalid SERIAL_BIND={bind:?}: {e}");
            return ExitCode::FAILURE;
        }
    };

    let max_payload = match std::env::var("SERIAL_MAX_PAYLOAD") {
        Ok(v) => match v.parse::<usize>() {
            Ok(n) if n >= 1 && n <= u16::MAX as usize => n,
            Ok(_) => {
                eprintln!("SERIAL_MAX_PAYLOAD must be in 1..=65535");
                return ExitCode::FAILURE;
            }
            Err(e) => {
                eprintln!("invalid SERIAL_MAX_PAYLOAD={v:?}: {e}");
                return ExitCode::FAILURE;
            }
        },
        Err(_) => DEFAULT_MAX_PAYLOAD,
    };

    let hard_bound = serialframe::HEADER_LEN + max_payload + serialframe::TRAILER_LEN;
    let app = app(max_payload);

    let listener = match tokio::net::TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("failed to bind {addr}: {e}");
            return ExitCode::FAILURE;
        }
    };
    println!("serialframe replay server listening on http://{addr}");
    println!("  max_payload = {max_payload} bytes, hard frame bound = {hard_bound} bytes");
    println!("  POST raw bytes to /v1/sessions/<id>/feed , read /v1/sessions/<id>/events");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .map(|_| ExitCode::SUCCESS)
        .unwrap_or_else(|e| {
            eprintln!("server error: {e}");
            ExitCode::FAILURE
        })
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        let mut s =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("install SIGTERM handler");
        s.recv().await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    println!("\nshutdown signal received");
}
