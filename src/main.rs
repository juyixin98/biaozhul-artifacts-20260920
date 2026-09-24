use depsolver::build_router;

#[tokio::main]
async fn main() {
    let addr = std::env::var("BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:3000".to_string());
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .unwrap_or_else(|e| panic!("failed to bind {addr}: {e}"));
    eprintln!("depsolver listening on http://{addr}");
    eprintln!("endpoints: GET /health, POST /resolve, POST /replay");
    axum::serve(listener, build_router()).await.unwrap();
}
