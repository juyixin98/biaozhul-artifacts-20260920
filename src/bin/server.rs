//! tls-observer-server：本地 TCP 测试服务。
//!
//! 用法：
//!   tls-observer-server [127.0.0.1:9000] [--read-timeout-ms N]
//!
//! 每条连接输出一行 JSON 报告到 stdout；运行日志到 stderr。
//! 服务不做 TLS 握手，只接收并观察裸字节。

use std::time::Duration;

use tls_observer::config::Config;
use tls_observer::server::serve;

fn main() {
    let args: Vec<String> = std::env::args().collect();

    let mut addr = String::from("127.0.0.1:9000");
    let mut read_timeout_ms: u64 = 5_000;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--read-timeout-ms" => {
                i += 1;
                read_timeout_ms = args
                    .get(i)
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(read_timeout_ms);
            }
            "-h" | "--help" => {
                println!("usage: tls-observer-server [ADDR] [--read-timeout-ms N]");
                println!("  ADDR defaults to 127.0.0.1:9000");
                println!("  --read-timeout-ms N  per-connection idle timeout (default 5000)");
                return;
            }
            other if !other.starts_with('-') => addr = other.to_string(),
            other => {
                eprintln!("unknown argument: {other}");
                std::process::exit(2);
            }
        }
        i += 1;
    }

    if let Ok(v) = std::env::var("TLS_OBSERVER_READ_TIMEOUT_MS") {
        if let Ok(parsed) = v.parse() {
            read_timeout_ms = parsed;
        }
    }

    // 只写 socket 的是对端；保持默认即可，某些 Unix 上显式忽略 SIGPIPE 更稳。
    #[cfg(unix)]
    unsafe {
        libc_ignore_sigpipe();
    }

    let config = Config::default();
    if let Err(e) = serve(&addr, config, Duration::from_millis(read_timeout_ms)) {
        eprintln!("[tls-observer] failed to serve {addr}: {e}");
        std::process::exit(1);
    }
}

#[cfg(unix)]
unsafe fn libc_ignore_sigpipe() {
    // 不依赖 libc crate：直接用 libc::signal(SIGPIPE, SIG_IGN) 的常量。
    extern "C" {
        fn signal(signum: i32, handler: usize) -> usize;
    }
    const SIGPIPE: i32 = 13;
    const SIG_IGN: usize = 1;
    let _ = signal(SIGPIPE, SIG_IGN);
}
