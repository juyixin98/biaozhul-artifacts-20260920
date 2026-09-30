//! Server entry point.
//!
//! Loads local configuration, builds the catalog from synthetic fixtures and
//! serves the Axum app. Run with `--config config/default.json` (default).

use decorrelate::api::http::router;
use decorrelate::catalog::Catalog;
use decorrelate::ServerConfig;
use std::process::ExitCode;
use tracing_subscriber::{fmt, EnvFilter};

#[tokio::main]
async fn main() -> ExitCode {
    let config_path = std::env::args().nth(1).map(|a| {
        // Accept either a bare path or `--config <path>`.
        if a == "--config" {
            std::env::args().nth(2).unwrap_or_else(|| "config/default.json".to_string())
        } else {
            a
        }
    });
    let config_path = config_path.as_deref();

    let cfg = match ServerConfig::load(config_path) {
        Ok(cfg) => cfg,
        Err(e) => {
            eprintln!("fatal: {e}");
            return ExitCode::FAILURE;
        }
    };

    init_tracing(&cfg);

    tracing::info!(
        bind = %cfg.bind,
        fixtures_dir = %cfg.fixtures_dir.display(),
        version = env!("CARGO_PKG_VERSION"),
        "starting decorrelate service"
    );

    let catalog = match Catalog::load_from_dir(&cfg.fixtures_dir) {
        Ok(catalog) => catalog,
        Err(e) => {
            tracing::error!(error = %e, "failed to load fixture catalog");
            return ExitCode::FAILURE;
        }
    };
    tracing::info!(relations = ?catalog.names(), "catalog loaded");

    let app = router(catalog);
    let listener = match tokio::net::TcpListener::bind(&cfg.bind).await {
        Ok(l) => l,
        Err(e) => {
            tracing::error!(bind = %cfg.bind, error = %e, "failed to bind");
            return ExitCode::FAILURE;
        }
    };
    tracing::info!("listening on http://{}", cfg.bind);

    if let Err(e) = axum::serve(listener, app).await {
        tracing::error!(error = %e, "server error");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

fn init_tracing(cfg: &ServerConfig) {
    let filter = EnvFilter::try_new(&cfg.log_filter).unwrap_or_else(|_| EnvFilter::new("info"));
    let subscriber = fmt().with_env_filter(filter);
    if cfg.log_json {
        subscriber.json().with_target(false).init();
    } else {
        subscriber.with_target(false).init();
    }
}
