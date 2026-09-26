//! JSON 控制入口。
//!
//! 用法：`lzsw [request.json]`（缺省从 stdin 读取请求）。
//! 请求：
//! ```json
//! {"op":"compress",   "input_base64":"...", "window":4096, "chunk":1024}
//! {"op":"decompress", "input_base64":"...", "max_output":1048576, "chunk":1024}
//! ```
//! `window` / `max_output` / `chunk` 均可选；`chunk` 用于以指定块大小
//! 分块喂入编解码器（演练流式路径），缺省整块处理。
//! 响应（stdout）：
//! ```json
//! {"ok":true,  "op":"compress", "input_bytes":N, "output_bytes":M, "tokens":T, "output_base64":"..."}
//! {"ok":false, "op":"decompress", "error_kind":"...", "error":"..."}
//! ```
//! 退出码：成功 0，失败 1。

use std::io::Read;

use lzsw::decoder::Decoder;
use lzsw::encoder::{Encoder, EncoderConfig};
use lzsw::error::{Error, Result};
use lzsw::json::{self, Json};
use lzsw::{base64, format};

fn main() {
    std::process::exit(run());
}

fn run() -> i32 {
    match handle() {
        Ok(resp) => {
            println!("{resp}");
            0
        }
        Err((op, e)) => {
            println!(
                "{{\"ok\":false,\"op\":{},\"error_kind\":{},\"error\":{}}}",
                json::escape_str(&op),
                json::escape_str(e.kind()),
                json::escape_str(&e.to_string())
            );
            1
        }
    }
}

fn handle() -> std::result::Result<String, (String, Error)> {
    let input = read_request().map_err(|e| (String::new(), e))?;
    let req = json::parse(&input).map_err(|e| (String::new(), e))?;
    let op = req
        .get("op")
        .and_then(Json::as_str)
        .ok_or(Error::MissingField("op"))
        .map_err(|e| (String::new(), e))?
        .to_string();
    match op.as_str() {
        "compress" => compress(&req).map_err(|e| (op.clone(), e)),
        "decompress" => decompress(&req).map_err(|e| (op.clone(), e)),
        other => Err((op.clone(), Error::UnknownOp(other.to_string()))),
    }
}

fn read_request() -> Result<String> {
    let arg = std::env::args().nth(1);
    let mut s = String::new();
    match arg {
        Some(path) => {
            s = std::fs::read_to_string(&path)?;
        }
        None => {
            std::io::stdin().read_to_string(&mut s)?;
        }
    }
    Ok(s)
}

fn input_bytes(req: &Json) -> Result<Vec<u8>> {
    let b64 = req
        .get("input_base64")
        .and_then(Json::as_str)
        .ok_or(Error::MissingField("input_base64"))?;
    base64::decode(b64)
}

fn chunk_size(req: &Json) -> Result<usize> {
    match req.get("chunk") {
        None | Some(Json::Null) => Ok(usize::MAX),
        Some(v) => {
            let n = v.as_u64().ok_or(Error::InvalidField("chunk"))?;
            if n == 0 {
                return Err(Error::InvalidField("chunk"));
            }
            Ok(n as usize)
        }
    }
}

fn ok_response(op: &str, in_len: usize, out_len: usize, tokens: u64, out: &[u8]) -> String {
    format!(
        "{{\"ok\":true,\"op\":{},\"input_bytes\":{},\"output_bytes\":{},\"tokens\":{},\"output_base64\":{}}}",
        json::escape_str(op),
        in_len,
        out_len,
        tokens,
        json::escape_str(&base64::encode(out))
    )
}

fn compress(req: &Json) -> Result<String> {
    let data = input_bytes(req)?;
    let window = match req.get("window") {
        None | Some(Json::Null) => format::DEFAULT_WINDOW,
        Some(v) => v.as_u64().ok_or(Error::InvalidField("window"))? as usize,
    };
    let chunk = chunk_size(req)?;
    let mut enc = Encoder::new(EncoderConfig {
        window,
        ..EncoderConfig::default()
    })?;
    let mut out = Vec::new();
    for piece in data.chunks(chunk) {
        enc.feed(piece, &mut out)?;
    }
    let tokens = enc.finish(&mut out)?;
    Ok(ok_response("compress", data.len(), out.len(), tokens, &out))
}

fn decompress(req: &Json) -> Result<String> {
    let data = input_bytes(req)?;
    let max_output = match req.get("max_output") {
        None | Some(Json::Null) => format::DEFAULT_MAX_OUTPUT,
        Some(v) => v.as_u64().ok_or(Error::InvalidField("max_output"))?,
    };
    let chunk = chunk_size(req)?;
    let mut dec = Decoder::new(max_output);
    let mut out = Vec::new();
    for piece in data.chunks(chunk) {
        dec.feed(piece, &mut out)?;
    }
    let tokens = dec.finish()?;
    Ok(ok_response(
        "decompress",
        data.len(),
        out.len(),
        tokens,
        &out,
    ))
}
