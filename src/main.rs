//! 中继租约与序号协调 —— 单二进制四个子命令：
//! `migrate`（建表）、`stub`（目标链桩）、`serve`（协调器 API）、`relay`（中继 worker）。

mod api;
mod crypto;
mod db;
mod stub;
mod worker;

use clap::{Parser, Subcommand};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

#[derive(Parser)]
#[command(name = "relay-coord", version, about)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// 对 DATABASE_URL 执行迁移
    Migrate {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
    /// 启动协调器 HTTP 服务
    Serve {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, env = "BIND_ADDR", default_value = "127.0.0.1:8080")]
        bind: String,
        #[arg(long, env = "LEASE_SECS", default_value_t = 10)]
        lease_secs: i32,
    },
    /// 启动目标链桩
    Stub {
        #[arg(long, env = "STUB_BIND", default_value = "127.0.0.1:9090")]
        bind: String,
        #[arg(long, env = "TARGET_SECRET", default_value = "dev-shared-secret")]
        secret: String,
        /// 桩全局故障模式：normal|drop_first:<ms>|fail_attempts:<n>|fatal|fatal_once|slow_ok:<ms>
        #[arg(long, env = "STUB_MODE", default_value = "normal")]
        mode: String,
    },
    /// 启动一个中继 worker
    Relay {
        #[arg(long, env = "COORDINATOR_URL", default_value = "http://127.0.0.1:8080")]
        coordinator: String,
        #[arg(long, env = "TARGET_URL", default_value = "http://127.0.0.1:9090")]
        target: String,
        #[arg(long, env = "TARGET_SECRET", default_value = "dev-shared-secret")]
        secret: String,
        #[arg(long, env = "LEASE_SECS", default_value_t = 10)]
        lease_secs: i32,
        #[arg(long, env = "RELAY_ID")]
        relay_id: Option<String>,
        /// 测试钩子：领取后挂死（不心跳、不完成），等待 SIGKILL
        #[arg(long, env = "STALL_AFTER_CLAIM", default_value_t = false)]
        stall_after_claim: bool,
        /// 连续空闲 N 秒后退出（测试用）
        #[arg(long, env = "EXIT_IF_IDLE_SECS")]
        exit_if_idle_secs: Option<u64>,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,sqlx=warn".into()),
        )
        .init();

    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Migrate { database_url } => {
            let pool = db::connect(&database_url).await?;
            db::run_migrations(&pool).await?;
            println!("migrations applied to {database_url}");
        }
        Cmd::Serve {
            database_url,
            bind,
            lease_secs,
        } => {
            let pool = db::connect(&database_url).await?;
            db::run_migrations(&pool).await?; // 幂等，启动时确保表存在
            let app = api::router(pool, lease_secs);
            let listener = tokio::net::TcpListener::bind(&bind).await?;
            let actual = listener.local_addr()?;
            println!("coordinator listening on http://{actual} (lease {lease_secs}s)");
            axum::serve(listener, app)
                .with_graceful_shutdown(shutdown_signal())
                .await?;
        }
        Cmd::Stub {
            bind,
            secret,
            mode,
        } => {
            let app = stub::router(secret, mode);
            let listener = tokio::net::TcpListener::bind(&bind).await?;
            let actual = listener.local_addr()?;
            println!("target stub listening on http://{actual}");
            axum::serve(listener, app)
                .with_graceful_shutdown(shutdown_signal())
                .await?;
        }
        Cmd::Relay {
            coordinator,
            target,
            secret,
            lease_secs,
            relay_id,
            stall_after_claim,
            exit_if_idle_secs,
        } => {
            let relay_id = relay_id.unwrap_or_else(|| {
                format!(
                    "relay-{}-{}",
                    std::process::id(),
                    hostname_token()
                )
            });
            let shutdown = Arc::new(AtomicBool::new(false));
            install_signal(shutdown.clone());
            let cfg = worker::Config {
                relay_id,
                coordinator,
                target,
                secret,
                lease_secs,
                stall_after_claim,
                exit_if_idle_secs,
            };
            worker::run(cfg, shutdown).await?;
        }
    }
    Ok(())
}

fn hostname_token() -> String {
    std::env::var("HOSTNAME")
        .ok()
        .unwrap_or_else(|| "local".to_string())
        .chars()
        .take(8)
        .collect()
}

#[cfg(unix)]
fn install_signal(flag: Arc<AtomicBool>) {
    tokio::spawn(async move {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = signal(SignalKind::terminate()).expect("SIGTERM handler");
        let mut int = signal(SignalKind::interrupt()).expect("SIGINT handler");
        tokio::select! {
            _ = term.recv() => {}
            _ = int.recv() => {}
        }
        flag.store(true, Ordering::Relaxed);
    });
}

async fn shutdown_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    let mut term = signal(SignalKind::terminate()).expect("SIGTERM handler");
    let mut int = signal(SignalKind::interrupt()).expect("SIGINT handler");
    tokio::select! {
        _ = term.recv() => {}
        _ = int.recv() => {}
    }
}
