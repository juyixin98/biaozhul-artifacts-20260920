//! Server entrypoint.
//!
//! Configuration via environment variables:
//! * `UTXO_DB`   — SQLite file path (default `./utxo.db`)
//! * `UTXO_ADDR` — listen address (default `127.0.0.1:8080`)

use std::sync::Arc;

use utxo_rollback::api;
use utxo_rollback::db::Db;

#[tokio::main]
async fn main() -> anyhow_lite::Result<()> {
    let db_path = std::env::var("UTXO_DB").unwrap_or_else(|_| "utxo.db".to_string());
    let addr = std::env::var("UTXO_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".to_string());

    // Opening is idempotent: schema is CREATE IF NOT EXISTS and the genesis
    // row is only inserted for a fresh database, so restart reuses state.
    let db = Arc::new(Db::open(&db_path)?);
    let (tip, hash) = db.tip()?;
    let root = db.state_root_now()?;
    println!("UTXO rollback validator");
    println!("  db:        {db_path}");
    println!("  tip:       height {tip} {}", hash);
    println!("  stateRoot: {}", root);

    let app = api::router(db);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    println!("  listening: http://{addr}");

    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            println!("\nshutting down");
        })
        .await?;
    Ok(())
}

/// Tiny local alias so the binary stays dependency-light.
mod anyhow_lite {
    pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
}
