//! HTTP 服务入口。用法：
//!   gas-meter --listen 127.0.0.1:8080 --seed-file examples/seed.json

use std::sync::Arc;

use anyhow::Context;
use gas_meter::{api, Engines, HostState};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,tower_http=warn".into()),
        )
        .init();

    let args: Vec<String> = std::env::args().collect();
    let mut listen = "127.0.0.1:8080".to_string();
    let mut seed_file: Option<String> = None;
    let mut iter = args.iter().skip(1);
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--listen" | "-l" => {
                listen = iter.next().context("--listen requires an address")?.clone();
            }
            "--seed-file" => {
                seed_file = Some(iter.next().context("--seed-file requires a path")?.clone());
            }
            "--help" | "-h" => {
                println!(
                    "Usage: gas-meter [--listen 127.0.0.1:8080] [--seed-file FILE]\n\
                     Env: RUST_LOG (default info,tower_http=warn)"
                );
                return Ok(());
            }
            other => anyhow::bail!("unknown argument: {other}"),
        }
    }

    let engines = Arc::new(Engines::new().context("failed to build wasmtime engines")?);
    let host = Arc::new(HostState::new());

    if let Some(path) = seed_file {
        load_seed(&host, &path).with_context(|| format!("loading seed file {path}"))?;
        tracing::info!("loaded seed KV from {path}");
    }

    let state = api::AppState {
        engines: Arc::clone(&engines),
        host: Arc::clone(&host),
    };
    let app = api::router(state);

    let listener = tokio::net::TcpListener::bind(&listen)
        .await
        .with_context(|| format!("binding {listen}"))?;
    tracing::info!(
        "versioned gas meter listening on http://{listen} (modules have NO network/filesystem access; fuel is not gas)"
    );
    axum::serve(listener, app).await?;
    Ok(())
}

/// 种子文件格式：{"key_hex_or_utf8": "value_hex_or_utf8", ...}
fn load_seed(host: &HostState, path: &str) -> anyhow::Result<()> {
    let raw = std::fs::read(path)?;
    let map: serde_json::Map<String, serde_json::Value> = serde_json::from_slice(&raw)?;
    for (k, v) in map {
        let val = match v {
            serde_json::Value::String(s) => s.into_bytes(),
            other => serde_json::to_vec(&other)?,
        };
        host.seed(k, val);
    }
    Ok(())
}
