//! B+ 树 HTTP 服务入口。
//!
//! 启动示例：
//! ```bash
//! cargo run --release -- --db ./data/bptree.db --page-size 64 --addr 127.0.0.1:3000
//! ```
//! 文件已存在时忽略 --page-size（以文件内元数据为准）。

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use disk_bptree::{build_router, AppState, BpTree};

#[derive(Debug)]
struct Args {
    db: PathBuf,
    page_size: u16,
    addr: SocketAddr,
    no_sync: bool,
}

fn print_usage() {
    eprintln!(
        "用法: bptree-server --db <PATH> [--page-size 64] [--addr 127.0.0.1:3000] [--no-sync]\n\
         也可用环境变量 BPTREE_DB / BPTREE_PAGE_SIZE / BPTREE_ADDR / BPTREE_NO_SYNC"
    );
}

fn parse_args() -> Result<Args, String> {
    let mut args = Args {
        db: PathBuf::from("bptree.db"),
        page_size: 256,
        addr: "127.0.0.1:3000".parse().unwrap(),
        no_sync: false,
    };
    let env_or = |key: &str| std::env::var(key).ok();
    if let Some(v) = env_or("BPTREE_DB") {
        args.db = PathBuf::from(v);
    }
    if let Some(v) = env_or("BPTREE_PAGE_SIZE") {
        args.page_size = v.parse().map_err(|_| format!("无效页大小 {v}"))?;
    }
    if let Some(v) = env_or("BPTREE_ADDR") {
        args.addr = v.parse().map_err(|_| format!("无效监听地址 {v}"))?;
    }
    if let Some(v) = env_or("BPTREE_NO_SYNC") {
        args.no_sync = matches!(v.as_str(), "1" | "true" | "TRUE");
    }

    let mut iter = std::env::args().skip(1);
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--db" => {
                args.db = PathBuf::from(
                    iter.next().ok_or_else(|| "--db 缺少参数".to_string())?,
                );
            }
            "--page-size" => {
                let v = iter.next().ok_or_else(|| "--page-size 缺少参数".to_string())?;
                args.page_size = v.parse().map_err(|_| format!("无效页大小 {v}"))?;
            }
            "--addr" => {
                let v = iter.next().ok_or_else(|| "--addr 缺少参数".to_string())?;
                args.addr = v.parse().map_err(|_| format!("无效监听地址 {v}"))?;
            }
            "--no-sync" => args.no_sync = true,
            "-h" | "--help" => {
                print_usage();
                std::process::exit(0);
            }
            other => return Err(format!("未知参数 {other}")),
        }
    }
    Ok(args)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            print_usage();
            eprintln!("参数错误: {e}");
            std::process::exit(2);
        }
    };

    // 已存在则打开（页大小以文件元数据为准），否则用指定页大小创建。
    let tree = if args.db.exists() {
        println!("打开已有数据库 {}（忽略 --page-size）", args.db.display());
        BpTree::open_opts(&args.db, !args.no_sync)?
    } else {
        if let Some(parent) = args.db.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        println!(
            "创建新数据库 {}，page_size = {} 字节",
            args.db.display(),
            args.page_size
        );
        BpTree::create(&args.db, args.page_size)?
    };

    let limits = tree.limits();
    println!(
        "叶容量 {} 项（半满 {}），内部分隔键容量 {}（半满 {}），fsync = {}",
        limits.leaf_max,
        limits.leaf_min(),
        limits.internal_max,
        limits.internal_min_seps(),
        !args.no_sync
    );

    let state = Arc::new(AppState::new(tree));
    let app = build_router(state);
    let listener = tokio::net::TcpListener::bind(args.addr).await?;
    println!("B+ 树索引服务监听 http://{}", args.addr);
    axum::serve(listener, app).await?;
    Ok(())
}
