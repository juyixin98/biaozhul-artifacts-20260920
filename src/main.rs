//! Binary entry point: loads fixtures, builds the Axum router, serves HTTP.
//!
//! No outbound network calls are ever made — blobs come only from the
//! built-in fixtures and client uploads.

use std::net::SocketAddr;
use std::sync::Arc;

use manifest_selector::api::build_app;
use manifest_selector::fixtures;
use manifest_selector::store::Registry;

mod args {
    pub struct Args {
        pub host: String,
        pub port: u16,
        pub no_fixtures: bool,
    }

    pub fn parse() -> Args {
        let mut a = Args {
            host: "127.0.0.1".to_string(),
            port: 8080,
            no_fixtures: false,
        };
        let mut iter = std::env::args().skip(1);
        while let Some(arg) = iter.next() {
            match arg.as_str() {
                "--host" => a.host = iter.next().unwrap_or_else(|| usage()),
                "--port" => {
                    a.port = iter
                        .next()
                        .unwrap_or_else(|| usage())
                        .parse()
                        .unwrap_or_else(|_| usage())
                }
                "--no-fixtures" => a.no_fixtures = true,
                "-h" | "--help" => {
                    println!(
                        "Usage: manifest-selector [--host 127.0.0.1] [--port 8080] [--no-fixtures]"
                    );
                    std::process::exit(0);
                }
                other => {
                    eprintln!("unknown argument: {other}");
                    usage();
                }
            }
        }
        a
    }

    fn usage() -> ! {
        eprintln!("Usage: manifest-selector [--host 127.0.0.1] [--port 8080] [--no-fixtures]");
        std::process::exit(2);
    }
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,tower_http=off".into()),
        )
        .init();

    let args = args::parse();
    let registry = Arc::new(Registry::new());

    if !args.no_fixtures {
        let summary = fixtures::build_all(&registry);
        tracing::info!("loaded {} fixture tags", summary.len());
        for (repo, tag, digest) in &summary {
            tracing::info!("  {repo}:{tag} -> {digest}");
        }
    }

    let app = build_app(registry);
    let addr: SocketAddr = format!("{}:{}", args.host, args.port)
        .parse()
        .expect("valid socket address");

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .unwrap_or_else(|e| panic!("failed to bind {addr}: {e}"));
    tracing::info!("listening on http://{addr} (no outbound network access is used)");

    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
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
}
