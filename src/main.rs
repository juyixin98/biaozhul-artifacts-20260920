mod clap_lite;

use chunk_upload::app::{router, AppState};
use chunk_upload::store::Store;
use clap_lite::CliArgs;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let args = CliArgs::parse();
    let store = Store::new(&args.data_dir).await?;
    let state = AppState {
        store,
        max_body: args.max_body,
    };
    let app = router(state);

    let listener = tokio::net::TcpListener::bind(args.addr).await?;
    tracing::info!(
        "listening on http://{} (data dir {:?})",
        args.addr,
        args.data_dir
    );
    axum::serve(listener, app).await?;
    Ok(())
}
