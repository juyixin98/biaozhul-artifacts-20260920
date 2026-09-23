//! bptree-server 启动入口。
//!
//! 用法：
//! ```bash
//! bptree-server --file ./data/bptree.db --page-size 256 --addr 127.0.0.1:8080
//! ```
//! 文件不存在时按给定页大小新建；已存在时忽略 `--page-size`（以文件头为准）。

use bptree_index::bptree::BPTree;
use bptree_index::http;
use bptree_index::pager::FilePager;
use bptree_index::Pager;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

#[derive(Debug)]
struct Args {
    file: PathBuf,
    page_size: usize,
    addr: SocketAddr,
}

fn parse_args() -> Result<Args, String> {
    let mut file = PathBuf::from("bptree.db");
    let mut page_size: usize = 4096;
    let mut addr: SocketAddr = "127.0.0.1:8080".parse().unwrap();
    let mut iter = std::env::args().skip(1);
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--file" | "-f" => {
                file = PathBuf::from(
                    iter.next()
                        .ok_or_else(|| "--file 需要一个路径参数".to_string())?,
                );
            }
            "--page-size" => {
                let v = iter
                    .next()
                    .ok_or_else(|| "--page-size 需要一个整数".to_string())?;
                page_size = v.parse().map_err(|_| format!("非法页大小：{v}"))?;
            }
            "--addr" | "-a" => {
                let v = iter
                    .next()
                    .ok_or_else(|| "--addr 需要 host:port".to_string())?;
                addr = v.parse().map_err(|_| format!("非法监听地址：{v}"))?;
            }
            "--help" | "-h" => {
                println!(
                    "bptree-server — 磁盘 B+ 树整数键索引服务\n\
                     \n\
                     选项：\n\
                     \x20 --file,-f <PATH>      数据文件路径（默认 bptree.db）\n\
                     \x20 --page-size <BYTES>  新建文件时的页大小，最小 64（默认 4096）\n\
                     \x20 --addr,-a <HOST:PORT> 监听地址（默认 127.0.0.1:8080）\n\
                     \x20 --help,-h             显示本帮助"
                );
                std::process::exit(0);
            }
            other => return Err(format!("未知参数：{other}（用 --help 查看用法）")),
        }
    }
    Ok(Args {
        file,
        page_size,
        addr,
    })
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = parse_args().map_err(|e| {
        eprintln!("参数错误：{e}");
        e
    })?;

    // 不存在则新建；存在则直接打开（页大小以文件头记录为准）。
    let pager = if args.file.exists() {
        println!("打开已有数据文件 {:?}", args.file);
        FilePager::open(&args.file)?
    } else {
        if let Some(parent) = args.file.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        println!(
            "创建新数据文件 {:?}，页大小 {} 字节",
            args.file, args.page_size
        );
        FilePager::create(&args.file, args.page_size)?
    };

    let actual_page_size = pager.page_size();
    let tree = Arc::new(Mutex::new(BPTree::new(pager)));
    let app = http::router(tree);

    println!(
        "bptree-server 监听 http://{} （页大小 {} 字节）",
        args.addr, actual_page_size
    );
    let listener = tokio::net::TcpListener::bind(args.addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}
