//! Binary entry point: load config + local fixtures, start Axum server.

use std::net::SocketAddr;

use decorr_service::config::Config;
use decorr_service::http;
use decorr_service::state::Catalog;
use tracing_subscriber::{fmt, EnvFilter};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let config = Config::load()?;

    let filter = EnvFilter::try_new(&config.log_level)
        .unwrap_or_else(|_| EnvFilter::new("info"));
    fmt().with_env_filter(filter).init();

    tracing::info!(
        fixtures_dir = %config.fixtures_dir.display(),
        host = %config.host,
        port = config.port,
        engine_version = decorr_service::decorrel::ENGINE_VERSION,
        "starting decorr-service"
    );

    let catalog = Catalog::from_fixtures(&config.fixtures_dir, config.max_rows_per_table)?;
    for t in catalog.table_names() {
        let table = catalog.get(&t)?;
        tracing::info!(table = %t, rows = table.batch.n_rows, "loaded synthetic table");
    }

    let app = http::router(catalog, config.clone());
    let addr: SocketAddr = config.bind_addr().parse()?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!("decorr-service listening on http://{addr}");

    axum::serve(listener, app).await?;
    Ok(())
}
