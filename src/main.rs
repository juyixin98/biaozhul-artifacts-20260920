use clap::{Parser, Subcommand};
use motion_arbiter::{config, crypto, db, server};
use std::sync::Arc;

/// 运动命令仲裁服务：合成速度命令（自主 / 遥控 / 急停）仲裁后端。
#[derive(Parser)]
#[command(name = "motion-arbiter", version, about)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// 启动 HTTP 服务。
    Serve {
        /// JSON 配置文件（含各来源 HMAC 密钥）。
        #[arg(long, default_value = "config.json")]
        config: String,
        /// SQLite 数据库文件。
        #[arg(long, default_value = "arbiter.db")]
        db: String,
        /// 监听地址。
        #[arg(long, default_value = "127.0.0.1:8080")]
        listen: String,
        /// 时钟模式：system（真实时钟）| manual（可控时钟，用 /admin/clock/advance 推进）。
        #[arg(long, default_value = "system")]
        clock: String,
        /// 手动时钟初始时刻（毫秒）。首次使用 manual 时有效；重启会沿用库中保存的时刻。
        #[arg(long)]
        clock_seed_ms: Option<i64>,
    },
    /// 生成三个 32 字节 HMAC 密钥（autonomous / rc / estop），输出 JSON 片段。
    Keygen,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Keygen => {
            let obj = serde_json::json!({
                "max_future_ms": 5000,
                "max_lease_ms": 5000,
                "limits": { "vx": 1.5, "vy": 1.0, "omega": 2.0 },
                "sources": {
                    "auto-1":  { "kind": "autonomous", "key_hex": crypto::generate_key_hex() },
                    "rc-1":    { "kind": "rc",         "key_hex": crypto::generate_key_hex() },
                    "estop-1": { "kind": "estop",      "key_hex": crypto::generate_key_hex() }
                }
            });
            println!(
                "{}",
                serde_json::to_string_pretty(&obj).expect("serialize config")
            );
        }
        Cmd::Serve {
            config,
            db,
            listen,
            clock,
            clock_seed_ms,
        } => {
            let cfg =
                config::Config::load(std::path::Path::new(&config)).unwrap_or_else(|e| fatal(&e));
            validate_kinds(&cfg);
            let database = db::Db::open(&db).unwrap_or_else(|e| fatal(&e));
            let cl = server::build_clock(&clock, &database, clock_seed_ms);
            server::start_server(cfg, database, Arc::clone(&cl), &listen).await;
        }
    }
}

fn validate_kinds(cfg: &config::Config) {
    use config::SourceKind::*;
    let mut kinds: Vec<_> = cfg.sources.values().map(|s| s.kind).collect();
    kinds.sort_by_key(|k| match k {
        Autonomous => 0,
        Rc => 1,
        Estop => 2,
    });
    kinds.dedup();
    for need in [Autonomous, Rc, Estop] {
        if !kinds.contains(&need) {
            fatal(&format!(
                "配置中必须至少各有一个 autonomous / rc / estop 来源（当前缺少 {}）",
                need.as_str()
            ));
        }
    }
}

fn fatal(msg: &str) -> ! {
    eprintln!("启动失败: {msg}");
    std::process::exit(1);
}
