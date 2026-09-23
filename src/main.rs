//! quote-router: single-chain offline swap router HTTP service.
//!
//! Run `quote-router --help` or see README.md.

use std::net::SocketAddr;
use std::sync::Arc;

use p016_quote_router::db::Store;
use p016_quote_router::api;

struct Args {
    bind: String,
    db: String,
}

fn print_usage() {
    eprintln!(
        "quote-router — exact integer multi-hop swap quote service\n\
\n\
USAGE:\n    quote-router [--bind <IP:PORT>] [--db <PATH>]\n\
\n\
ENVIRONMENT (used when the matching flag is absent):\n    \
ROUTER_BIND   listen address (default 127.0.0.1:8080)\n    \
ROUTER_DB     sqlite database path (default ./router.sqlite)\n"
    );
}

fn usage_error() -> ! {
    print_usage();
    std::process::exit(2);
}

fn parse_args() -> Args {
    let mut bind = std::env::var("ROUTER_BIND").unwrap_or_else(|_| "127.0.0.1:8080".into());
    let mut db = std::env::var("ROUTER_DB").unwrap_or_else(|_| "router.sqlite".into());

    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--bind" => {
                bind = it.next().unwrap_or_else(|| usage_error());
            }
            "--db" => {
                db = it.next().unwrap_or_else(|| usage_error());
            }
            "-h" | "--help" => {
                print_usage();
                std::process::exit(0);
            }
            other => {
                eprintln!("unknown argument: {other}");
                usage_error();
            }
        }
    }
    Args { bind, db }
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let args = parse_args();
    let addr: SocketAddr = args.bind.parse().unwrap_or_else(|e| {
        eprintln!("invalid --bind address {:?}: {e}", args.bind);
        std::process::exit(2);
    });

    let store = Arc::new(Store::open(&args.db).unwrap_or_else(|e| {
        eprintln!("failed to open sqlite database {:?}: {e}", args.db);
        std::process::exit(1);
    }));

    let app = api::router(store);
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap_or_else(|e| {
        eprintln!("failed to bind {addr}: {e}");
        std::process::exit(1);
    });
    tracing::info!("quote-router listening on http://{addr} (db={})", args.db);

    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            tracing::info!("shutdown signal received");
        })
        .await
        .expect("server error");
}
