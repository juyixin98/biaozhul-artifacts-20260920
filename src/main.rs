//! depres — a small backtracking package-dependency solver with an HTTP API.
//!
//! Modules:
//! - [`semver_range`]: the requirement language (`^ ~ > >= < <= = * x`, OR groups)
//! - [`platform`]: target predicates (os/arch/family, all/any/not)
//! - [`model`]: registry + wire types
//! - [`solver`]: MRV backtracking search with conflict chains
//! - [`oracle`]: brute-force cross-check for small instances
//! - [`lockfile`]: lock construction, fingerprinting, replay
//! - [`fixtures`]: acceptance scenarios
//! - [`http`]: Axum routes

mod fixtures;
mod http;
mod lockfile;
mod model;
mod oracle;
mod platform;
mod semver_range;
mod solver;

use std::net::SocketAddr;

#[tokio::main]
async fn main() {
    let addr: SocketAddr = std::env::var("DEPRES_ADDR")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(|| SocketAddr::from(([0, 0, 0, 0], 8080)));

    let app = http::router();
    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind listener");
    eprintln!("depres listening on http://{addr}");
    axum::serve(listener, app).await.expect("server error");
}
