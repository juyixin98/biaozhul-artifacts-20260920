//! observe-file：离线观察器。把一个报文文件按任意字节块喂给 Observer，
//! 打印 JSON 结论；可用第二个参数指定喂入块大小（模拟 TCP 分片）。
//!
//! 用法：observe-file FILE [CHUNK_SIZE，默认一次性整文件]

use tls_observer::config::Config;
use tls_observer::observer::Observer;
use tls_observer::server::client_hello_to_json;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let path = args.get(1).cloned().unwrap_or_else(|| {
        eprintln!("usage: observe-file FILE [CHUNK_SIZE]");
        std::process::exit(2);
    });
    let chunk: Option<usize> = args.get(2).and_then(|v| v.parse().ok());

    let data = std::fs::read(&path).unwrap_or_else(|e| {
        eprintln!("cannot read {path}: {e}");
        std::process::exit(1);
    });

    let mut observer = Observer::new(Config::default());
    let feed = |obs: &mut Observer, part: &[u8]| -> Result<(), String> {
        obs.feed(part).map_err(|e| e.to_string())
    };

    let feed_result = match chunk {
        Some(n) if n > 0 => {
            let mut err = None;
            for part in data.chunks(n) {
                if let Err(e) = feed(&mut observer, part) {
                    err = Some(e);
                    break;
                }
            }
            err.map(Err).unwrap_or(Ok(()))
        }
        _ => feed(&mut observer, &data),
    };

    // 输出结构化 JSON。
    match feed_result {
        Err(e) => {
            println!(
                "{{\"file\":{},\"result\":\"parse_error\",\"error\":{},\"record_count\":{}}}",
                tls_observer::json::json_string(&path),
                tls_observer::json::json_string(&e.to_string()),
                observer.records().len()
            );
            std::process::exit(1);
        }
        Ok(()) => match observer.finish() {
            Ok(tls_observer::Conclusion::ClientHello(ch)) => {
                println!(
                    "{{\"file\":{},\"result\":\"client_hello\",\"client_hello\":{}}}",
                    tls_observer::json::json_string(&path),
                    client_hello_to_json(&ch)
                );
            }
            Ok(tls_observer::Conclusion::NoClientHello(reason)) => {
                println!(
                    "{{\"file\":{},\"result\":\"no_client_hello\",\"reason\":\"{}\",\"record_count\":{}}}",
                    tls_observer::json::json_string(&path),
                    reason.as_code(),
                    observer.records().len()
                );
            }
            Err(e) => {
                println!(
                    "{{\"file\":{},\"result\":\"parse_error\",\"error\":{},\"record_count\":{}}}",
                    tls_observer::json::json_string(&path),
                    tls_observer::json::json_string(&e.to_string()),
                    observer.records().len()
                );
                std::process::exit(1);
            }
        },
    }
}
