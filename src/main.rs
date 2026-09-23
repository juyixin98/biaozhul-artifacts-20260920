//! `wsecho`：本地 WebSocket 帧回声测试服务。
//!
//! 用法：
//! ```text
//! wsecho [--addr 127.0.0.1:9001] [--raw]
//!        [--max-frame-bytes 1048576] [--max-message-bytes 65536] [--quiet]
//! ```
//! - 默认先做 RFC 6455 握手；`--raw` 跳过握手直接处理已掩码的客户端帧；
//! - 长度上限默认单帧 1 MiB、整消息 64 KiB（便于演示 1009）。

use std::process::ExitCode;
use std::str::FromStr;

use wsframe::frame::Limits;
use wsframe::server::{run, ServerConfig};

struct Args {
    config: ServerConfig,
}

fn print_usage() {
    eprintln!(
        "wsecho - local RFC 6455 frame echo server for testing\n\
\n\
USAGE:\n    wsecho [OPTIONS]\n\
\n\
OPTIONS:\n\
    --addr <IP:PORT>             listen address (default 127.0.0.1:9001)\n\
    --raw                        skip HTTP upgrade handshake, accept raw masked frames\n\
    --max-frame-bytes <N>        max payload of one frame (default {frame})\n\
    --max-message-bytes <N>      max reassembled message size (default {msg})\n\
    --quiet                      do not log per-frame events to stderr\n\
    -h, --help                   show this help\n",
        frame = Limits::DEFAULT.max_frame_payload,
        msg = Limits::DEFAULT.max_message_size
    );
}

fn parse_args() -> Result<Args, String> {
    let mut config = ServerConfig::default();
    let mut max_frame = Limits::DEFAULT.max_frame_payload;
    let mut max_message = Limits::DEFAULT.max_message_size;

    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                print_usage();
                std::process::exit(0);
            }
            "--addr" => {
                config.addr = it
                    .next()
                    .ok_or_else(|| "--addr needs a value, e.g. 127.0.0.1:9001".to_string())?;
            }
            "--raw" => config.raw = true,
            "--quiet" => config.verbose = false,
            "--max-frame-bytes" => {
                let v = it
                    .next()
                    .ok_or_else(|| "--max-frame-bytes needs a value".to_string())?;
                max_frame = parse_usize(&v)? as u64;
            }
            "--max-message-bytes" => {
                let v = it
                    .next()
                    .ok_or_else(|| "--max-message-bytes needs a value".to_string())?;
                max_message = parse_usize(&v)?;
            }
            other => return Err(format!("unknown argument: {other} (see --help)")),
        }
    }

    config.limits = Limits {
        max_frame_payload: max_frame,
        max_message_size: max_message,
    };
    Ok(Args { config })
}

fn parse_usize(s: &str) -> Result<usize, String> {
    // 允许 16k / 1m 这类后缀，方便命令行调参。
    let (num, mul) = match s.chars().last() {
        Some('k') | Some('K') => (&s[..s.len() - 1], 1024usize),
        Some('m') | Some('M') => (&s[..s.len() - 1], 1024 * 1024),
        Some('g') | Some('G') => (&s[..s.len() - 1], 1024 * 1024 * 1024),
        _ => (s, 1usize),
    };
    usize::from_str(num.trim())
        .map(|n| n.saturating_mul(mul))
        .map_err(|_| format!("not a size: {s}"))
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("error: {e}");
            print_usage();
            return ExitCode::FAILURE;
        }
    };

    match run(args.config) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("server error: {e}");
            ExitCode::FAILURE
        }
    }
}
