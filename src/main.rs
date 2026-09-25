//! `aac`：自适应算术编码的 JSON 控制入口。
//!
//! 用法：
//!   aac [request.json]     从文件读取请求；缺省从 stdin 读取
//!
//! 请求（JSON）：
//!   {
//!     "op": "encode" | "decode",        // 必填
//!     "input_base64": "...",            // 与 input_file 二选一
//!     "input_file": "path",             // 与 input_base64 二选一
//!     "output_file": "path",            // 可选；缺省则结果内联为 output_base64
//!     "max_input_bytes": 1048576,       // 可选，默认 64 MiB
//!     "max_output_bytes": 1048576       // 可选，默认 64 MiB
//!   }
//!
//! 响应（JSON，写 stdout）：
//!   成功: {"ok":true,"op":"encode","input_bytes":N,"output_bytes":M,
//!          "rescale_count":K,"output_base64":"..."}
//!   失败: {"ok":false,"error":"...","message":"..."}
//!
//! 退出码：0 = 成功；1 = 请求合法但编解码失败（响应中 ok=false）；
//!         2 = 请求格式非法。

use std::fs::File;
use std::io::{self, BufReader, BufWriter, Cursor, Read, Write};
use std::process::ExitCode;

use adaptive_arith::{decode, encode, CodecStats, Error, Limits};
use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use serde_json::{json, Value};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    if args.len() > 2 || args.get(1).map(|s| s.as_str()) == Some("--help") {
        eprintln!("usage: aac [request.json]   (request defaults to stdin)");
        return ExitCode::from(2);
    }

    let request_text = match read_request(args.get(1)) {
        Ok(text) => text,
        Err(e) => {
            return fail_malformed(&format!("cannot read request: {e}"));
        }
    };
    let request: Value = match serde_json::from_str(&request_text) {
        Ok(v) => v,
        Err(e) => {
            return fail_malformed(&format!("invalid JSON: {e}"));
        }
    };

    match run(&request) {
        Ok(response) => {
            println!("{response}");
            ExitCode::SUCCESS
        }
        Err((code, response)) => {
            println!("{response}");
            ExitCode::from(code)
        }
    }
}

fn read_request(path: Option<&String>) -> io::Result<String> {
    let mut text = String::new();
    match path {
        Some(p) => {
            File::open(p)?.read_to_string(&mut text)?;
        }
        None => {
            io::stdin().read_to_string(&mut text)?;
        }
    }
    Ok(text)
}

fn fail_malformed(message: &str) -> ExitCode {
    let response = json!({"ok": false, "error": "malformed_request", "message": message});
    println!("{response}");
    ExitCode::from(2)
}

fn malformed(message: &str) -> (u8, Value) {
    (
        2,
        json!({"ok": false, "error": "malformed_request", "message": message}),
    )
}

fn run(request: &Value) -> Result<Value, (u8, Value)> {
    let op = request
        .get("op")
        .and_then(Value::as_str)
        .ok_or_else(|| malformed("missing or invalid \"op\" (expected \"encode\" or \"decode\")"))?;
    if op != "encode" && op != "decode" {
        return Err(malformed("\"op\" must be \"encode\" or \"decode\""));
    }

    let limits = parse_limits(request)?;

    // 输入来源：input_base64 或 input_file，二选一。
    let input_b64 = request.get("input_base64").and_then(Value::as_str);
    let input_file = request.get("input_file").and_then(Value::as_str);
    let input: Box<dyn Read> = match (input_b64, input_file) {
        (Some(b64), None) => {
            let bytes = B64
                .decode(b64)
                .map_err(|e| malformed(&format!("invalid input_base64: {e}")))?;
            Box::new(Cursor::new(bytes))
        }
        (None, Some(path)) => {
            let file = File::open(path)
                .map_err(|e| malformed(&format!("cannot open input_file: {e}")))?;
            Box::new(BufReader::new(file))
        }
        (Some(_), Some(_)) => {
            return Err(malformed(
                "provide only one of \"input_base64\" and \"input_file\"",
            ));
        }
        (None, None) => {
            return Err(malformed(
                "missing input: provide \"input_base64\" or \"input_file\"",
            ));
        }
    };

    // 输出去向：output_file 或内存缓冲（随后内联为 output_base64）。
    let output_file = request.get("output_file").and_then(Value::as_str);
    let mut inline_buf: Vec<u8> = Vec::new();
    let output: Box<dyn Write> = match output_file {
        Some(path) => {
            let file = File::create(path)
                .map_err(|e| malformed(&format!("cannot create output_file: {e}")))?;
            Box::new(BufWriter::new(file))
        }
        None => Box::new(&mut inline_buf),
    };

    let result = if op == "encode" {
        encode(input, output, &limits)
    } else {
        decode(input, output, &limits)
    };

    let stats: CodecStats = result.map_err(|e| op_error(&e))?;

    let mut response = json!({
        "ok": true,
        "op": op,
        "input_bytes": stats.input_bytes,
        "output_bytes": stats.output_bytes,
        "rescale_count": stats.rescale_count,
    });
    if output_file.is_none() {
        response["output_base64"] = Value::String(B64.encode(&inline_buf));
    } else {
        response["output_file"] = Value::String(output_file.unwrap().to_string());
    }
    Ok(response)
}

fn parse_limits(request: &Value) -> Result<Limits, (u8, Value)> {
    let mut limits = Limits::default();
    for (key, slot) in [
        ("max_input_bytes", &mut limits.max_input_bytes),
        ("max_output_bytes", &mut limits.max_output_bytes),
    ] {
        if let Some(v) = request.get(key) {
            *slot = v
                .as_u64()
                .ok_or_else(|| malformed(&format!("\"{key}\" must be a non-negative integer")))?;
        }
    }
    Ok(limits)
}

fn op_error(err: &Error) -> (u8, Value) {
    let code = match err {
        Error::InputLimitExceeded => "input_limit_exceeded",
        Error::OutputLimitExceeded => "output_limit_exceeded",
        Error::TruncatedStream => "truncated_stream",
        Error::Io(_) => "io_error",
    };
    (
        1,
        json!({"ok": false, "error": code, "message": err.to_string()}),
    )
}
