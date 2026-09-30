//! HTTP service entry point: loads a local fixture, builds shared state and
//! serves the Axum router with graceful shutdown.

use std::net::SocketAddr;

use leapfrog_triejoin::api;
use leapfrog_triejoin::config::Config;
use leapfrog_triejoin::dataset::FixtureSet;
use leapfrog_triejoin::state::AppState;

#[tokio::main]
async fn main() -> std::io::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let config = Config::from_env().unwrap_or_else(|e| {
        eprintln!("invalid configuration: {e}");
        std::process::exit(2);
    });
    let fixture = FixtureSet::by_name(&config.fixture).unwrap_or_else(|e| {
        eprintln!("cannot load fixture `{}`: {e}", config.fixture);
        std::process::exit(2);
    });

    let state = AppState::from_fixture(config.clone(), fixture);
    let app = api::router(state);

    let addr = SocketAddr::new(
        config.host.parse().unwrap_or_else(|_| {
            eprintln!("invalid host `{}`", config.host);
            std::process::exit(2);
        }),
        config.port,
    );

    tracing::info!(%addr, fixture = %config.fixture, "leapfrog-triejoin listening");
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    tracing::info!("shutdown complete");
    Ok(())
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
}
