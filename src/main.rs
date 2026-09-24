use std::env;
use std::process::ExitCode;

use incremental_build_planner::api;

#[tokio::main]
async fn main() -> ExitCode {
    let port = env::var("PORT").unwrap_or_else(|_| "8080".to_string());
    let addr = format!("127.0.0.1:{port}");

    let listener = match tokio::net::TcpListener::bind(&addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("fatal: failed to bind {addr}: {e}");
            return ExitCode::FAILURE;
        }
    };
    eprintln!("incremental-build-planner listening on http://{addr}");
    eprintln!("endpoints: GET /healthz , POST /plan");

    if let Err(e) = axum::serve(listener, api::router()).await {
        eprintln!("fatal: server error: {e}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}
