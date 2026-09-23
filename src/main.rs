//! Server entry point.

use std::net::SocketAddr;
use std::path::PathBuf;

use merkle_proof_service::{api, Service};

#[derive(Debug, Clone)]
struct Config {
    db_path: PathBuf,
    addr: SocketAddr,
}

fn parse_args() -> Config {
    let mut db_path = PathBuf::from("db/merkle");
    let mut addr: SocketAddr = "127.0.0.1:8080".parse().unwrap();

    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--db" | "-d" => {
                db_path = PathBuf::from(args.next().expect("--db requires a path"))
            }
            "--addr" | "-a" | "--listen" => {
                let a = args.next().expect("--addr requires host:port");
                addr = a.parse().expect("invalid --addr host:port");
            }
            "--help" | "-h" => {
                println!(
                    "Merkle State Proof Service\n\nUSAGE:\n    merkle-proof-service [--db PATH] [--addr HOST:PORT]\n\nDEFAULTS:\n    --db   db/merkle\n    --addr 127.0.0.1:8080\n"
                );
                std::process::exit(0);
            }
            other => {
                eprintln!("unknown argument: {other}");
                std::process::exit(2);
            }
        }
    }
    Config { db_path, addr }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let cfg = parse_args();

    if let Some(parent) = cfg.db_path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }

    let service = Service::open(&cfg.db_path)?;
    let current = service.current_version()?;
    tracing::info!(
        db = %cfg.db_path.display(),
        current_version = current,
        "opened database"
    );

    let app = api::router(service);
    let listener = tokio::net::TcpListener::bind(cfg.addr).await?;
    tracing::info!(addr = %cfg.addr, "listening");
    axum::serve(listener, app).await?;
    Ok(())
}
