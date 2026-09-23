//! lsm-server 启动入口。
//!
//! 用法：lsm-server --addr 127.0.0.1:3000 --data-dir ./data --max-mem 1000

use lsm_server::app;
use lsm_server::engine::Engine;
use std::sync::Arc;

#[tokio::main]
async fn main() {
    let mut addr = "127.0.0.1:3000".to_string();
    let mut data_dir = "./data".to_string();
    let mut max_mem = 1000usize;

    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--addr" => addr = args.next().expect("--addr needs a value"),
            "--data-dir" => data_dir = args.next().expect("--data-dir needs a value"),
            "--max-mem" => {
                max_mem = args
                    .next()
                    .expect("--max-mem needs a value")
                    .parse()
                    .expect("--max-mem must be a positive integer")
            }
            "-h" | "--help" => {
                println!("用法: lsm-server [--addr 127.0.0.1:3000] [--data-dir ./data] [--max-mem 1000]");
                return;
            }
            other => {
                eprintln!("未知参数: {other}");
                std::process::exit(2);
            }
        }
    }

    let engine = Arc::new(
        Engine::open(&data_dir, max_mem).unwrap_or_else(|e| {
            eprintln!("打开数据目录 {data_dir:?} 失败: {e}");
            std::process::exit(1);
        }),
    );

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .unwrap_or_else(|e| {
            eprintln!("绑定 {addr} 失败: {e}");
            std::process::exit(1);
        });
    println!("lsm-server listening on http://{addr} (data_dir={data_dir}, max_mem={max_mem})");
    axum::serve(listener, app(engine))
        .await
        .unwrap_or_else(|e| {
            eprintln!("server error: {e}");
            std::process::exit(1);
        });
}
