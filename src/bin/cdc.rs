//! `cdc` 命令行：JSON 控制入口。
//!
//! 从标准输入（或 `--request-file`）读取一个 JSON 请求，向标准输出写 JSON 响应。
//!
//! ```bash
//! cdc [--pretty] [--request-file PATH]
//! ```
//!
//! 支持的 op：`encode` / `decode` / `merge` / `inspect`，请求与响应格式见
//! `README.md` 与 `docs/API.md`。进程以退出码 0 表示得到响应（即使 `ok:false`），
//! 仅在无法读取输入等本地错误时返回非 0。

use std::io::Read;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut pretty = false;
    let mut request_file: Option<String> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--pretty" => pretty = true,
            "--request-file" => {
                i += 1;
                if i >= args.len() {
                    eprintln!("error: --request-file requires a path");
                    return ExitCode::from(2);
                }
                request_file = Some(args[i].clone());
            }
            "-h" | "--help" => {
                print_help();
                return ExitCode::SUCCESS;
            }
            other => {
                eprintln!("error: unknown argument '{other}'");
                print_help();
                return ExitCode::from(2);
            }
        }
        i += 1;
    }

    let input = match request_file {
        Some(path) => match std::fs::read_to_string(&path) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("error: cannot read {path}: {e}");
                return ExitCode::from(2);
            }
        },
        None => {
            let mut buf = String::new();
            if let Err(e) = std::io::stdin().read_to_string(&mut buf) {
                eprintln!("error: cannot read stdin: {e}");
                return ExitCode::from(2);
            }
            buf
        }
    };

    let response = if pretty {
        cdc::control::handle_pretty(&input)
    } else {
        cdc::control::handle(&input)
    };
    println!("{response}");
    ExitCode::SUCCESS
}

fn print_help() {
    eprintln!(
        "cdc — columnar dictionary coding, JSON control entry point\n\n\
         USAGE:\n    cdc [--pretty] [--request-file PATH]\n\n\
         Reads a JSON request from PATH (default: stdin) and writes the JSON\n\
         response to stdout. Ops: encode, decode, merge, inspect.\n\
         See README.md and docs/API.md for the request/response schema."
    );
}
