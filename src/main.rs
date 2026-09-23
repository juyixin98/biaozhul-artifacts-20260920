//! Command-line entry point. All logic lives in the `swap_router` library.
//!
//! Subcommands:
//!   swap-router serve [--db PATH] [--addr HOST:PORT]
//!   swap-router seed  --db PATH --input snapshot.json

use std::sync::Arc;

use swap_router::db::Store;
use swap_router::model::{validate_snapshot_input, SnapshotInput};
use swap_router::app;

fn print_help() {
    println!(
        "swap-router — single-chain offline exact-quote router\n\n\
USAGE:\n  swap-router serve [--db PATH] [--addr HOST:PORT]\n  \
swap-router seed  --db PATH --input snapshot.json\n\n\
ENV:\n  SWAP_ROUTER_DB   sqlite file (default: swap_router.db)\n  \
SWAP_ROUTER_ADDR  bind address (default: 127.0.0.1:8080)\n"
    );
}

fn parse_flag<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    args.windows(2)
        .find(|w| w[0] == name)
        .map(|w| w[1].as_str())
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let args: Vec<String> = std::env::args().collect();
    let cmd = args.get(1).map(String::as_str).unwrap_or("serve");

    match cmd {
        "serve" => run_serve(&args[2..]).await,
        "seed" => run_seed(&args[2..]).await,
        "-h" | "--help" | "help" => print_help(),
        other => {
            eprintln!("unknown subcommand: {other}");
            print_help();
            std::process::exit(2);
        }
    }
}

async fn run_serve(args: &[String]) {
    let db = parse_flag(args, "--db")
        .map(|s| s.to_string())
        .or_else(|| std::env::var("SWAP_ROUTER_DB").ok())
        .unwrap_or_else(|| "swap_router.db".to_string());
    let addr = parse_flag(args, "--addr")
        .map(|s| s.to_string())
        .or_else(|| std::env::var("SWAP_ROUTER_ADDR").ok())
        .unwrap_or_else(|| "127.0.0.1:8080".to_string());

    let store = Arc::new(Store::open(&db).unwrap_or_else(|e| {
        eprintln!("failed to open database {db}: {e}");
        std::process::exit(1);
    }));

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap_or_else(|e| {
        eprintln!("failed to bind {addr}: {e}");
        std::process::exit(1);
    });
    tracing::info!("swap-router listening on http://{addr} (db={db})");
    axum::serve(listener, app(store))
        .with_graceful_shutdown(shutdown_signal())
        .await
        .unwrap();
}

async fn run_seed(args: &[String]) {
    let db = match parse_flag(args, "--db") {
        Some(v) => v.to_string(),
        None => std::env::var("SWAP_ROUTER_DB").unwrap_or_else(|_| "swap_router.db".into()),
    };
    let input = match parse_flag(args, "--input") {
        Some(v) => v,
        None => {
            eprintln!("seed requires --input snapshot.json");
            std::process::exit(2);
        }
    };
    let raw = tokio::fs::read_to_string(input).await.unwrap_or_else(|e| {
        eprintln!("cannot read {input}: {e}");
        std::process::exit(1);
    });
    let parsed: SnapshotInput = serde_json::from_str(&raw).unwrap_or_else(|e| {
        eprintln!("invalid snapshot json: {e}");
        std::process::exit(1);
    });
    let (assets, pools) = match validate_snapshot_input(&parsed) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("invalid snapshot: {e}");
            std::process::exit(1);
        }
    };
    let store = Store::open(&db).unwrap_or_else(|e| {
        eprintln!("failed to open database {db}: {e}");
        std::process::exit(1);
    });
    let hash = Store::content_hash(&assets, &pools);
    let created_at = swap_router::db::now_rfc3339();
    let id = store
        .insert_snapshot(&assets, &pools, &created_at, &hash)
        .await
        .unwrap_or_else(|e| {
            eprintln!("insert failed: {e}");
            std::process::exit(1);
        });
    println!(
        "seeded snapshot {id}: {} assets, {} pools, content_hash={hash}",
        assets.len(),
        pools.len()
    );
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!("shutdown signal received");
}
