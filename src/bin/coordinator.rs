//! 协调服务进程：连 PostgreSQL、跑迁移、启动 Axum。

use clap::Parser;
use relay_coord::coordinator;
use relay_coord::db::Store;
use std::sync::Arc;

#[derive(Parser)]
#[command(name = "coordinator", about = "中继租约与序号协调服务")]
struct Args {
    /// 监听地址
    #[arg(long, default_value = "127.0.0.1:8080")]
    listen: String,
    /// PostgreSQL 连接串
    #[arg(long, env = "DATABASE_URL", default_value = "postgres://admin:relay_coord_dev@localhost/relay_coord")]
    database_url: String,
}

#[tokio::main]
async fn main() -> anyhow_lite::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,sqlx=warn".into()),
        )
        .init();

    let args = Args::parse();
    let store = Store::connect(&args.database_url).await?;
    store.migrate().await?;
    tracing::info!(%args.database_url, "migrations applied");

    let app = coordinator::router(Arc::new(store));
    let listener = tokio::net::TcpListener::bind(&args.listen).await?;
    tracing::info!(%args.listen, "coordinator listening");

    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

/// 仅为 main 返回 Box<dyn Error> 的轻量别名，避免引入 anyhow。
mod anyhow_lite {
    pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
}
