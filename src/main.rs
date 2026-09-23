//! Merkle state proof service — binary entrypoint.

use merkle_proof_service::{api, store};
use std::net::SocketAddr;
use std::sync::Arc;

#[derive(Debug)]
struct Cli {
    db_path: String,
    listen: SocketAddr,
}

fn parse_cli() -> Result<Cli, String> {
    let mut db_path = "data/merkle-rocksdb".to_string();
    let mut listen: SocketAddr = "127.0.0.1:8080".parse().unwrap();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--db" => {
                db_path = args
                    .next()
                    .ok_or_else(|| "--db requires a path".to_string())?
            }
            "--listen" => {
                let v = args
                    .next()
                    .ok_or_else(|| "--listen requires ADDR:PORT".to_string())?;
                listen = v
                    .parse()
                    .map_err(|e| format!("bad --listen address {v:?}: {e}"))?;
            }
            "-h" | "--help" => {
                println!(
                    "merkle-proof-service\n\nUSAGE:\n    merkle-proof-service [--db PATH] [--listen ADDR:PORT]\n\nENV:\n    MERKLE_DB_PATH, MERKLE_LISTEN override defaults\n    RUST_LOG (default: info)"
                );
                std::process::exit(0);
            }
            other => return Err(format!("unknown argument {other}")),
        }
    }
    if let Ok(p) = std::env::var("MERKLE_DB_PATH") {
        db_path = p;
    }
    if let Ok(l) = std::env::var("MERKLE_LISTEN") {
        listen = l.parse().map_err(|e| format!("bad MERKLE_LISTEN: {e}"))?;
    }
    Ok(Cli { db_path, listen })
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let cli = match parse_cli() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("argument error: {e}");
            std::process::exit(2);
        }
    };

    let store = Arc::new(store::Store::open(&cli.db_path)?);
    tracing::info!(
        db = %cli.db_path,
        version = store.current_version(),
        "opened state (current version restored from manifest on restart)"
    );

    let listener = tokio::net::TcpListener::bind(cli.listen).await?;
    tracing::info!(addr = %cli.listen, "Merkle state proof service listening");
    let app = api::router(store);
    axum::serve(listener, app).await?;
    Ok(())
}
