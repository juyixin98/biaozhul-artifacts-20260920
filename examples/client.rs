//! 最小 TCP 客户端：发送文件中的原始 DNS 报文（可多帧），逐帧打印原始 hex 与解码结果。
//!
//! 用法：
//!     cargo run --example client -- <addr> <报文文件> [<报文文件> ...]
//! 每个文件内可包含一个或多个帧；两字节长度前缀由本示例自动添加。

use std::fs;
use std::net::TcpStream;

use dns_compress::frame::{recv_frame, send_frame};
use dns_compress::message::Message;
use dns_compress::parser::Limits;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 {
        eprintln!("用法: client <addr> <报文文件> [<报文文件> ...]");
        std::process::exit(2)
    }
    let addr = &args[1];
    let files = &args[2..];
    let limits = Limits::default();

    let mut stream = TcpStream::connect(addr).expect("连接服务器失败");
    stream.set_nodelay(true).unwrap();

    for path in files {
        let body = fs::read(path).unwrap_or_else(|e| panic!("读取 {path} 失败：{e}"));
        println!("=== 发送 {path}（{} 字节） ===", body.len());
        send_frame(&mut stream, &body).expect("发送失败");
        let resp = recv_frame(&mut stream, 65535).expect("接收响应失败");
        println!("响应原始字节（{} 字节）：", resp.len());
        println!("{}", hex_dump(&resp));
        match Message::parse(&resp, &limits) {
            Ok(msg) => print!("{msg}"),
            Err(e) => println!("响应解析失败：{e}"),
        }
    }
}

fn hex_dump(b: &[u8]) -> String {
    b.iter()
        .enumerate()
        .map(|(i, x)| {
            let sep = if (i + 1) % 16 == 0 { "\n" } else { " " };
            format!("{x:02x}{sep}")
        })
        .collect()
}
