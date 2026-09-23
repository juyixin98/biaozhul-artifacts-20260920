//! HTTP server binary.
//!
//! Usage:
//!   utxo-server --db utxo.db --addr 127.0.0.1:3000

use std::sync::Arc;

use utxo_rollback::api;
use utxo_rollback::Storage;

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut db_path = "utxo.db".to_string();
    let mut addr = "127.0.0.1:3000".to_string();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--db" => {
                db_path = args.get(i + 1).expect("--db requires a value").clone();
                i += 2;
            }
            "--addr" => {
                addr = args.get(i + 1).expect("--addr requires a value").clone();
                i += 2;
            }
            "-h" | "--help" => {
                println!("usage: utxo-server [--db PATH] [--addr HOST:PORT]");
                return;
            }
            other => {
                eprintln!("unknown argument: {other}");
                std::process::exit(2);
            }
        }
    }

    let storage = Arc::new(
        Storage::open(&db_path)
            .unwrap_or_else(|e| panic!("failed to open database {db_path}: {e}")),
    );
    let app = api::router(storage);

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .unwrap_or_else(|e| panic!("failed to bind {addr}: {e}"));
    eprintln!("UTXO rollback validator listening on http://{addr} (db: {db_path})");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("server error");
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    eprintln!("shutdown signal received, draining");
}
