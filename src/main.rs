use anyhow::{Context, Result};
use lsm_kv::{app, Config, Db};
use std::sync::Arc;
use tokio::net::TcpListener;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let args: Vec<String> = std::env::args().collect();
    let mut dir = String::from("./lsm-data");
    let mut port = 3000u16;
    let mut memtable_entries = 1000usize;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--dir" => {
                dir = args.get(i + 1).cloned().context("--dir requires a value")?;
                i += 2;
            }
            "--port" => {
                port = args
                    .get(i + 1)
                    .context("--port requires a value")?
                    .parse()
                    .context("invalid --port")?;
                i += 2;
            }
            "--memtable-entries" => {
                memtable_entries = args
                    .get(i + 1)
                    .context("--memtable-entries requires a value")?
                    .parse()
                    .context("invalid --memtable-entries")?;
                i += 2;
            }
            "-h" | "--help" => {
                println!(
                    "lsm-kv — simplified LSM KV service\n\n\
                     USAGE:\n  lsm-kv [--dir DIR] [--port PORT] [--memtable-entries N]\n\n\
                     DEFAULTS:\n  --dir ./lsm-data\n  --port 3000\n  --memtable-entries 1000"
                );
                return Ok(());
            }
            other => {
                anyhow::bail!("unknown argument: {other} (try --help)");
            }
        }
    }

    let config = Config::new(&dir)
        .memtable_entries(memtable_entries)
        .background_flush(true);
    let db: Arc<Db> = Db::open(config)?;
    let listener = TcpListener::bind(("127.0.0.1", port)).await?;
    tracing::info!("lsm-kv listening on http://127.0.0.1:{port} (data dir: {dir})");

    let server = axum::serve(listener, app(db));
    let shutdown = async {
        let _ = tokio::signal::ctrl_c().await;
        tracing::info!("shutdown signal received");
    };
    server.with_graceful_shutdown(shutdown).await?;
    Ok(())
}
