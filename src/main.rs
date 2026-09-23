//! 命令行入口：
//!
//! ```text
//! dual-superblock serve    [--addr 127.0.0.1:8080] [--dir ./storedata]
//! dual-superblock format   [--dir ./storedata]
//! dual-superblock selftest
//! ```

use std::process::ExitCode;

use dual_superblock::http_server::{serve, HttpConfig};
use dual_superblock::io_layer::RealStorage;
use dual_superblock::selftest;
use dual_superblock::store::Repository;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (cmd, rest) = match args.split_first() {
        Some((c, r)) => (c.as_str(), r),
        None => {
            print_usage();
            return ExitCode::from(2);
        }
    };

    match cmd {
        "serve" => {
            let cfg = parse_http_args(rest);
            if let Err(e) = serve(&cfg) {
                eprintln!("服务退出: {e}");
                return ExitCode::FAILURE;
            }
            ExitCode::SUCCESS
        }
        "format" => {
            let dir = flag_value(rest, "--dir").unwrap_or_else(|| "./storedata".into());
            match RealStorage::create_files(std::path::Path::new(&dir)).and_then(|()| {
                let st = RealStorage::open(std::path::Path::new(&dir))?;
                Repository::format(st).map_err(real_err)?;
                Ok(())
            }) {
                Ok(()) => {
                    println!("已在 {dir} 完成格式化（代次0，空根，超级块槽位0）");
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("格式化失败: {e}");
                    ExitCode::FAILURE
                }
            }
        }
        "selftest" => {
            let report = selftest::run();
            print!("{}", report.render_text());
            if report.all_passed() {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            }
        }
        "-h" | "--help" | "help" => {
            print_usage();
            ExitCode::SUCCESS
        }
        other => {
            eprintln!("未知子命令: {other}");
            print_usage();
            ExitCode::from(2)
        }
    }
}

fn real_err<E: std::fmt::Display>(e: E) -> std::io::Error {
    std::io::Error::other(e.to_string())
}

fn parse_http_args(args: &[String]) -> HttpConfig {
    HttpConfig {
        addr: flag_value(args, "--addr").unwrap_or_else(|| "127.0.0.1:8080".into()),
        dir: flag_value(args, "--dir").unwrap_or_else(|| "./storedata".into()),
    }
}

fn flag_value(args: &[String], flag: &str) -> Option<String> {
    args.iter()
        .position(|a| a == flag)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn print_usage() {
    eprintln!(
        "dual-superblock —— 双页超级块恢复演示（纯后端）\n\
         \n\
         用法:\n  \
         dual-superblock serve    [--addr 127.0.0.1:8080] [--dir ./storedata]\n  \
         dual-superblock format   [--dir ./storedata]\n  \
         dual-superblock selftest\n  \
         dual-superblock help\n"
    );
}
