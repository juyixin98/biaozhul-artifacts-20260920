//! manifest-selector: offline OCI-style multi-architecture manifest selection.
//!
//! Pure backend HTTP service. It never contacts a registry or downloads
//! images; content comes from a local fixtures directory and/or the
//! `/admin/...` ingest API.

use std::net::SocketAddr;
use std::path::PathBuf;

use axum::Router;
use manifest_selector::api::{app, AppState};
use manifest_selector::loader;
use manifest_selector::store::Store;
use tower::limit::ConcurrencyLimitLayer;
use tower::ServiceBuilder;

#[derive(Debug)]
struct Args {
    addr: SocketAddr,
    fixtures_dir: Option<PathBuf>,
    max_concurrency: usize,
}

fn parse_args() -> Args {
    let mut addr: SocketAddr = "0.0.0.0:8080".parse().unwrap();
    let mut fixtures_dir: Option<PathBuf> = None;
    let mut max_concurrency = 256;

    let mut iter = std::env::args().skip(1);
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--listen" | "-l" => {
                let v = iter
                    .next()
                    .unwrap_or_else(|| usage("--listen needs a value"));
                addr = v.parse().unwrap_or_else(|e| {
                    eprintln!("invalid --listen address {v:?}: {e}");
                    std::process::exit(2);
                });
            }
            "--fixtures" | "-f" => {
                let v = iter
                    .next()
                    .unwrap_or_else(|| usage("--fixtures needs a value"));
                fixtures_dir = Some(PathBuf::from(v));
            }
            "--max-concurrency" => {
                let v = iter
                    .next()
                    .unwrap_or_else(|| usage("--max-concurrency needs a value"));
                max_concurrency = v.parse().unwrap_or_else(|e| {
                    eprintln!("invalid --max-concurrency {v:?}: {e}");
                    std::process::exit(2);
                });
            }
            "--help" | "-h" => {
                print_help();
                std::process::exit(0);
            }
            other => {
                eprintln!("unknown argument: {other}");
                print_help();
                std::process::exit(2);
            }
        }
    }

    Args {
        addr,
        fixtures_dir,
        max_concurrency,
    }
}

fn usage(msg: &str) -> ! {
    eprintln!("{msg}");
    print_help();
    std::process::exit(2)
}

fn print_help() {
    eprintln!(
        "manifest-selector — offline OCI multi-arch manifest selection\n\
        \n\
        USAGE:\n  \
            manifest-selector [OPTIONS]\n\
        \n\
        OPTIONS:\n  \
            -l, --listen <ADDR:PORT>     Bind address (default 0.0.0.0:8080)\n  \
            -f, --fixtures <DIR>         Load <DIR>/*/fixture.json at startup\n  \
                --max-concurrency <N>    In-flight request limit (default 256)\n  \
            -h, --help                   Show this help\n\
        \n\
        No network access is performed at any point."
    );
}

#[tokio::main]
async fn main() {
    let args = parse_args();

    let mut store = Store::new();
    if let Some(dir) = &args.fixtures_dir {
        match loader::load_dir(&mut store, dir) {
            Ok(repos) => {
                if repos.is_empty() {
                    eprintln!("note: no fixtures found under {}", dir.display());
                } else {
                    eprintln!(
                        "loaded {} fixture repository/repositories from {}",
                        repos.len(),
                        dir.display()
                    );
                }
            }
            Err(e) => {
                eprintln!("failed to load fixtures from {}: {e:?}", dir.display());
                std::process::exit(1);
            }
        }
    } else {
        eprintln!("note: no --fixtures directory given; starting with an empty store");
    }

    let state = AppState::new(store);
    let router: Router = app(state)
        .layer(ServiceBuilder::new().layer(ConcurrencyLimitLayer::new(args.max_concurrency)));

    let listener = tokio::net::TcpListener::bind(args.addr)
        .await
        .unwrap_or_else(|e| {
            eprintln!("failed to bind {}: {e}", args.addr);
            std::process::exit(1);
        });
    eprintln!("manifest-selector listening on http://{}", args.addr);
    axum::serve(listener, router).await.unwrap_or_else(|e| {
        eprintln!("server error: {e}");
        std::process::exit(1);
    });
}
