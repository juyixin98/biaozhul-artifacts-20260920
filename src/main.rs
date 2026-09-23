//! ehindex 服务入口。
//!
//! 参数（命令行或环境变量；命令行优先）：
//!
//! | 参数 | 环境变量 | 默认值 | 说明 |
//! |---|---|---|---|
//! | `--addr` | `EHINDEX_ADDR` | `127.0.0.1:8080` | 监听地址 |
//! | `--file` | `EHINDEX_FILE` | `./ehindex.db` | 索引文件路径 |
//! | `--cap` | `EHINDEX_CAP` | `4` | 桶容量（记录数，仅创建时生效，1..=6） |
//! | `--hash` | `EHINDEX_HASH` | `fx` | 哈希：`fx` / `const[:值]` / `mod:n`（仅创建时生效） |
//!
//! 已存在的文件会忽略 `--cap/--hash`（以文件头为准）；`--hash` 与文件中
//! 记录不同则拒绝打开，避免用错散列破坏定位。

use std::process::ExitCode;

use ehindex::hash::HashKind;
use ehindex::index::Index;

struct Args {
    addr: String,
    file: String,
    cap: u32,
    hash: Option<String>,
}

fn parse_args() -> Args {
    let mut args = Args {
        addr: std::env::var("EHINDEX_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".into()),
        file: std::env::var("EHINDEX_FILE").unwrap_or_else(|_| "ehindex.db".into()),
        cap: std::env::var("EHINDEX_CAP")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(4),
        hash: std::env::var("EHINDEX_HASH").ok(),
    };
    let mut iter = std::env::args().skip(1);
    while let Some(a) = iter.next() {
        let take_val = |name: &str, iter: &mut std::iter::Skip<std::env::Args>| -> Option<String> {
            if let Some(v) = iter.next() {
                Some(v)
            } else {
                eprintln!("参数 {name} 需要一个值");
                std::process::exit(2);
            }
        };
        match a.as_str() {
            "--addr" => args.addr = take_val("--addr", &mut iter).unwrap(),
            "--file" => args.file = take_val("--file", &mut iter).unwrap(),
            "--cap" => {
                args.cap = take_val("--cap", &mut iter)
                    .unwrap()
                    .parse()
                    .unwrap_or_else(|_| {
                        eprintln!("--cap 必须是正整数");
                        std::process::exit(2);
                    });
            }
            "--hash" => args.hash = Some(take_val("--hash", &mut iter).unwrap()),
            "-h" | "--help" => {
                print_help();
                std::process::exit(0);
            }
            other => {
                eprintln!("未知参数：{other}");
                print_help();
                std::process::exit(2);
            }
        }
    }
    args
}

fn print_help() {
    println!(
        "ehindex — 磁盘可扩展哈希页索引服务\n\
\n\
用法：ehindex [--addr 127.0.0.1:8080] [--file ehindex.db] [--cap 4] [--hash fx]\n\
\n\
HTTP 接口：\n\
  GET  /healthz\n\
  POST /put    {{\"key\": \"k\", \"value\": \"v\"}}\n\
  POST /get    {{\"key\": \"k\"}}\n\
  POST /delete {{\"key\": \"k\"}}\n\
  GET  /stats\n\
  POST /raw/put?key=k   （请求体即值字节，二进制安全）\n\
\n\
哈希模式：fx（默认）| const[:值]（全碰撞演示）| mod:n（受控碰撞）"
    );
}

fn main() -> ExitCode {
    let args = parse_args();
    let hash_spec = args.hash.as_deref().unwrap_or("fx");
    let hash_kind = match HashKind::parse(hash_spec) {
        Ok(k) => k,
        Err(e) => {
            eprintln!("哈希参数错误：{e}");
            return ExitCode::FAILURE;
        }
    };

    let index = match Index::open_with(&args.file, Some(hash_kind)) {
        Ok(idx) => {
            eprintln!(
                "已打开索引 {}（global_depth={}，哈希={}，桶容量={}）",
                args.file,
                idx.global_depth(),
                idx.hash_kind().describe(),
                idx.bucket_capacity()
            );
            idx
        }
        Err(ehindex::IndexError::Io(ref io)) if io.kind() == std::io::ErrorKind::NotFound => {
            match Index::create(&args.file, args.cap, hash_kind) {
                Ok(idx) => {
                    eprintln!(
                        "已创建索引 {}（桶容量={}，哈希={}）",
                        args.file,
                        args.cap,
                        hash_kind.describe()
                    );
                    idx
                }
                Err(e) => {
                    eprintln!("创建索引失败：{e}");
                    return ExitCode::FAILURE;
                }
            }
        }
        Err(e) => {
            eprintln!("打开索引失败：{e}");
            return ExitCode::FAILURE;
        }
    };

    let rt = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("启动 Tokio 运行时失败：{e}");
            return ExitCode::FAILURE;
        }
    };
    rt.block_on(async move {
        if let Err(e) = ehindex::server::serve(index, &args.addr).await {
            eprintln!("服务退出：{e}");
            ExitCode::FAILURE
        } else {
            ExitCode::SUCCESS
        }
    })
}
