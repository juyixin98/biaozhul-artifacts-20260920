//! gas-meter CLI。
//!
//! 用法：
//!   gas-meter serve [--addr 127.0.0.1:8080] [--data-dir ./data]
//!   gas-meter run-sample <name> [--version 1] [--input-b64 ...]
//!   gas-meter export-samples [--dir wasm-out]

use std::io::Write;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use versioned_gas_meter::{
    api::{self, AppState},
    build_engine, CommittedState, Engine, ExecRequest, Journal, VersionRegistry,
};

fn usage() -> ! {
    eprintln!(
        "版本化 Gas 计量器（离线 WASM 沙箱）\n\
\n\
用法:\n\
  gas-meter serve [--addr HOST:PORT] [--data-dir DIR] [--journal-cap N]\n\
  gas-meter run-sample <name> [--version V] [--fuel-limit N] [--input-b64 B64]\n\
  gas-meter export-samples [--dir DIR]\n\
\n\
样例: {}\n",
        versioned_gas_meter::samples::samples()
            .iter()
            .map(|s| s.name)
            .collect::<Vec<_>>()
            .join(", ")
    );
    std::process::exit(2);
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("serve") => serve(&args[1..]).await,
        Some("run-sample") => run_sample(&args[1..]),
        Some("export-samples") => export_samples(&args[1..]),
        Some("-h") | Some("--help") | None => usage(),
        Some(other) => {
            eprintln!("未知子命令：{other}");
            usage();
        }
    }
}

fn parse_opt<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
    args.windows(2)
        .find(|w| w[0] == flag)
        .map(|w| w[1].as_str())
}

async fn serve(args: &[String]) -> anyhow::Result<()> {
    let addr: SocketAddr = parse_opt(args, "--addr")
        .unwrap_or("127.0.0.1:8080")
        .parse()?;
    let data_dir = PathBuf::from(parse_opt(args, "--data-dir").unwrap_or("./data"));
    let journal_cap: usize = parse_opt(args, "--journal-cap")
        .map(|s| s.parse())
        .transpose()?
        .unwrap_or(1024);

    std::fs::create_dir_all(&data_dir)?;
    let state = Arc::new(CommittedState::load(&data_dir)?);
    let journal = Arc::new(Journal::new(journal_cap));
    let engine = build_engine(state.clone(), journal.clone())?;

    let app = api::router(AppState {
        engine,
        state,
        journal,
    });
    let listener = tokio::net::TcpListener::bind(addr).await?;
    eprintln!("版本化 Gas 计量器监听 http://{addr} ，数据目录 {data_dir:?}");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("install Ctrl-C handler");
    };
    #[cfg(unix)]
    let terminate = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            s.recv().await;
        } else {
            std::future::pending::<()>().await;
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {}
        _ = terminate => {}
    }
}

fn default_input_for(name: &str) -> Vec<u8> {
    match name {
        // n = 100_000，小端 8 字节。
        "finite_loop" => 100_000u64.to_le_bytes().to_vec(),
        // 每次增长 1 页（64KiB）。
        "memory_grow" => 1u64.to_le_bytes().to_vec(),
        _ => Vec::new(),
    }
}

fn run_sample(args: &[String]) -> anyhow::Result<()> {
    let name = match args.first() {
        Some(n) => n.clone(),
        None => usage(),
    };
    let sample = versioned_gas_meter::samples::by_name(&name)
        .ok_or_else(|| anyhow::anyhow!("未知样例 {name}"))?;
    let version: u32 = parse_opt(args, "--version")
        .map(|s| s.parse())
        .transpose()?
        .unwrap_or(1);
    let fuel_limit = parse_opt(args, "--fuel-limit")
        .map(|s| s.parse::<u64>())
        .transpose()?;
    let input = match parse_opt(args, "--input-b64") {
        Some(b64) => versioned_gas_meter::base64::decode(b64)
            .ok_or_else(|| anyhow::anyhow!("非法 base64 输入"))?,
        None => default_input_for(&name),
    };

    // 离线 CLI 不落盘，状态仅存内存。
    let state = Arc::new(CommittedState::memory());
    let journal = Arc::new(Journal::new(64));
    let engine = Engine::new(VersionRegistry::new(), state, journal)?;
    let resp = engine.execute(ExecRequest {
        module: sample.wasm.to_vec(),
        input,
        metering_version: version,
        fuel_limit_override: fuel_limit,
        idempotency_key: None,
    });
    println!("{}", serde_json::to_string_pretty(&resp)?);
    if resp.status != "committed" {
        // 失败如实报告：以退出码 1 供脚本验收。
        std::process::exit(1);
    }
    Ok(())
}

fn export_samples(args: &[String]) -> anyhow::Result<()> {
    let dir = PathBuf::from(parse_opt(args, "--dir").unwrap_or("wasm-out"));
    std::fs::create_dir_all(&dir)?;
    for s in versioned_gas_meter::samples::samples() {
        let path = dir.join(format!("{}.wasm", s.name));
        let mut f = std::fs::File::create(&path)?;
        f.write_all(s.wasm)?;
        println!("{} ({} 字节)", path.display(), s.wasm.len());
    }
    Ok(())
}
