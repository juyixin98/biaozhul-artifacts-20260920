//! sse-server：启动本地 SSE 测试服务。
//!
//! 用法：
//!
//! ```text
//! sse-server [--port 18080] [--history 64] [--tick-ms 1000]
//! ```
//!
//! * `--history`  保留的最近事件数（有限历史窗口），默认 64；设小一点可演示
//!   过期游标收到的 reset 事件。
//! * `--tick-ms`  后台自动产生 `event: tick` 事件的间隔；0 表示不自动产生。
//!
//! Ctrl-C 退出。客户端用 `curl -N http://127.0.0.1:<port>/events` 即可观察。

use sse_resume::encode::OutEvent;
use sse_resume::server::Server;
use std::net::SocketAddr;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

struct Args {
    port: u16,
    history: usize,
    tick_ms: u64,
}

fn parse_args() -> Args {
    let mut a = Args {
        port: 18080,
        history: 64,
        tick_ms: 1000,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--port" | "-p" => {
                a.port = it
                    .next()
                    .unwrap_or_else(|| panic!("--port 需要一个值"))
                    .parse()
                    .unwrap_or_else(|e| panic!("--port 值非法: {e}"))
            }
            "--history" => {
                a.history = it
                    .next()
                    .unwrap_or_else(|| panic!("--history 需要一个值"))
                    .parse()
                    .unwrap_or_else(|e| panic!("--history 值非法: {e}"))
            }
            "--tick-ms" => {
                a.tick_ms = it
                    .next()
                    .unwrap_or_else(|| panic!("--tick-ms 需要一个值"))
                    .parse()
                    .unwrap_or_else(|e| panic!("--tick-ms 值非法: {e}"))
            }
            "--help" | "-h" => {
                println!("用法: sse-server [--port 18080] [--history 64] [--tick-ms 1000]");
                std::process::exit(0);
            }
            other => panic!("未知参数: {other}"),
        }
    }
    a
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn main() -> std::io::Result<()> {
    let args = parse_args();
    let addr: SocketAddr = ([127, 0, 0, 1], args.port).into();
    let running = Server::bind(addr, args.history)?.spawn()?;
    println!(
        "SSE 测试服务监听 http://{}/events （历史窗口 {}，tick {}ms）",
        running.addr(),
        args.history,
        args.tick_ms
    );

    println!("Ctrl-C 退出。");
    if args.tick_ms > 0 {
        loop {
            thread::sleep(Duration::from_millis(args.tick_ms));
            let ev = OutEvent {
                id: None,
                event: Some("tick".into()),
                retry_ms: None,
                data: vec![format!("{{\"ts\":{}}}", now_ms())],
            };
            match running.publish(ev) {
                Ok(id) => println!("publish tick id={id}"),
                Err(e) => eprintln!("publish 失败: {e}"),
            }
        }
    }
    loop {
        thread::sleep(Duration::from_secs(3600));
    }
}
