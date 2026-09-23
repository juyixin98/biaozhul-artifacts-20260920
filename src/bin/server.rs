//! dns-tcp-server：本地 DNS 压缩名字解码测试服务（TCP，RFC 1035 §4.2.2 成帧）。
//!
//! 用法：
//!     dns-tcp-server [绑定地址] [最大报文长度]
//! 默认：127.0.0.1:10053，最大报文 4096 字节。

use std::net::{TcpListener, TcpStream};
use std::thread;

use dns_compress::parser::Limits;
use dns_compress::server::serve_connection;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "-h" || a == "--help") {
        println!("用法: dns-tcp-server [绑定地址] [最大报文长度]");
        println!("默认: 127.0.0.1:10053，最大报文 4096 字节");
        return;
    }
    let addr = args.get(1).map(String::as_str).unwrap_or("127.0.0.1:10053");
    let max_msg: usize = args
        .get(2)
        .map(|s| s.parse().expect("最大报文长度必须是数字"))
        .unwrap_or(4096);
    let limits = Limits::default();

    let listener = TcpListener::bind(addr).unwrap_or_else(|e| {
        eprintln!("无法绑定 {addr}：{e}");
        std::process::exit(1);
    });
    eprintln!(
        "DNS TCP 测试服务已启动：{addr}（max_msg={max_msg}, 指针跳转上限={}）",
        limits.max_pointer_jumps
    );

    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                thread::spawn(move || handle(stream, limits, max_msg));
            }
            Err(e) => eprintln!("接受连接失败：{e}"),
        }
    }
}

fn handle(mut stream: TcpStream, limits: Limits, max_msg: usize) {
    let _ = stream.set_nodelay(true);
    serve_connection(&mut stream, &limits, max_msg);
}
