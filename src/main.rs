//! 运动命令仲裁服务入口。
//!
//! 主要环境变量：
//! - ARBITER_CLOCK=sim|wall（默认 sim；sim 模式用 X-Sim-At 头控制时间）
//! - ARBITER_DB（默认 data/arbiter.sqlite；:memory: 可用于纯临时进程）
//! - ARBITER_BIND（默认 127.0.0.1:8080）
//! - ARBITER_SIM_START（sim 模式初始时刻，默认 1_000_000_000_000）
//! - ARBITER_KEY_AUTONOMOUS / ARBITER_KEY_REMOTE / ARBITER_KEY_ESTOP
//!   （缺省为开发用固定密钥，仅用于本地与测试；生产务必覆盖）
//! - ARBITER_ADMIN_TOKEN（设置后启用 /admin/reset）

mod arbiter;
mod crypto;
mod db;
mod http;
mod model;

use std::sync::Arc;

use arbiter::{Arbiter, Limits};
use db::Store;
use http::{Keys, AppState};
use model::ClockMode;

#[derive(Debug)]
struct Config {
    clock: ClockMode,
    db_path: String,
    bind: String,
    sim_start: i64,
    key_autonomous: String,
    key_remote: String,
    key_estop: String,
    admin_token: Option<String>,
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn load_config() -> Config {
    let clock = match env_or("ARBITER_CLOCK", "sim").as_str() {
        "sim" => ClockMode::Sim,
        "wall" => ClockMode::Wall,
        other => {
            eprintln!("ARBITER_CLOCK must be sim or wall, got {other:?}; using sim");
            ClockMode::Sim
        }
    };
    let sim_start = env_or("ARBITER_SIM_START", "1000000000000")
        .parse::<i64>()
        .expect("ARBITER_SIM_START must be integer unix-ms");
    Config {
        clock,
        db_path: env_or("ARBITER_DB", "data/arbiter.sqlite"),
        bind: env_or("ARBITER_BIND", "127.0.0.1:8080"),
        sim_start,
        key_autonomous: env_or("ARBITER_KEY_AUTONOMOUS", "dev-autonomous-secret"),
        key_remote: env_or("ARBITER_KEY_REMOTE", "dev-remote-secret"),
        key_estop: env_or("ARBITER_KEY_ESTOP", "dev-estop-secret"),
        admin_token: std::env::var("ARBITER_ADMIN_TOKEN").ok().filter(|s| !s.is_empty()),
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cfg = load_config();

    if cfg.db_path != ":memory:" {
        if let Some(parent) = std::path::Path::new(&cfg.db_path).parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
    }
    let store = Store::open(&cfg.db_path)?;
    let arbiter = Arc::new(Arbiter::new(store, cfg.clock, cfg.sim_start, Limits::default())?);

    let decode = |spec: &str| Arc::new(crypto::decode_key(spec).expect("key decode"));
    let state = AppState {
        arbiter,
        keys: Keys {
            autonomous: decode(&cfg.key_autonomous),
            remote: decode(&cfg.key_remote),
            estop: decode(&cfg.key_estop),
            admin_token: cfg.admin_token.map(Arc::new),
        },
    };

    let app = http::router(state);

    // 测试场景可通过 ARBITER_LISTEN_FD 直接继承已绑定的监听 socket（避免端口竞态）。
    let listener = match std::env::var("ARBITER_LISTEN_FD") {
        Ok(fd) => {
            let fd: i32 = fd.parse().expect("ARBITER_LISTEN_FD must be integer");
            let std_listener = unsafe {
                use std::os::fd::FromRawFd;
                std::net::TcpListener::from_raw_fd(fd)
            };
            std_listener.set_nonblocking(true)?;
            tokio::net::TcpListener::from_std(std_listener)?
        }
        Err(_) => tokio::net::TcpListener::bind(&cfg.bind).await?,
    };
    let local = listener.local_addr()?;
    eprintln!(
        "motion-arbiter listening on http://{local} (clock={clock}, db={db})",
        clock = cfg.clock.as_str(),
        db = cfg.db_path,
    );
    eprintln!("NOTE: backend only; decisions are logged, no motor is connected.");
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            eprintln!("\nshutdown signal received");
        })
        .await?;
    Ok(())
}
