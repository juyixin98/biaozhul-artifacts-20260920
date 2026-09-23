//! 中继进程：持续从协调器领取租约并提交到目标桩。

use clap::Parser;
use relay_coord::worker::{run_loop, WorkerConfig};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Notify;

#[derive(Parser)]
#[command(name = "worker", about = "中继（relay）工作进程")]
struct Args {
    #[arg(long, default_value = "http://127.0.0.1:8080")]
    coordinator: String,
    #[arg(long, default_value = "http://127.0.0.1:9090")]
    target: String,
    #[arg(long, default_value_t = relay_id_default())]
    relay_id: String,
    /// 租约 TTL（秒）：超过它没回执，协调器允许别人重领
    #[arg(long, default_value_t = 10)]
    lease_ttl: i64,
    /// 提交目标的 HTTP 超时（秒）：超时即"未知"，先查询再重发
    #[arg(long, default_value_t = 3)]
    submit_timeout: u64,
    #[arg(long, default_value_t = 300)]
    poll_ms: u64,
}

fn relay_id_default() -> String {
    format!("relay-{}", &uuid::Uuid::new_v4().simple().to_string()[..8])
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
    let shutdown = Arc::new(Notify::new());
    let signal = shutdown.clone();
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            signal.notify_waiters();
        }
    });
    let cfg = WorkerConfig {
        coordinator_url: args.coordinator,
        target_url: args.target,
        relay_id: args.relay_id,
        lease_ttl_secs: args.lease_ttl,
        submit_timeout: Duration::from_secs(args.submit_timeout),
        poll_interval: Duration::from_millis(args.poll_ms),
        backoff_base: Duration::from_millis(300),
    };
    tracing::info!(relay = %cfg.relay_id, "worker starting");
    run_loop(cfg, shutdown).await;
    Ok(())
}
