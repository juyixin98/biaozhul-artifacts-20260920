//! 目标链桩进程：幂等提交 / 结果查询 / 故障注入规则。

use clap::Parser;
use relay_coord::target;

#[derive(Parser)]
#[command(name = "target-stub", about = "本地模拟目标链桩")]
struct Args {
    #[arg(long, default_value = "127.0.0.1:9090")]
    listen: String,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let args = Args::parse();
    let listener = tokio::net::TcpListener::bind(&args.listen).await?;
    tracing::info!(%args.listen, "target stub listening");

    axum::serve(listener, target::router())
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
