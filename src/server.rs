//! 本地 TCP 测试服务：接受一条连接上的裸字节，喂给 [`Observer`]，
//! 输出每条连接的观察报告（单行 JSON 到 stdout，日志到 stderr）。
//!
//! 服务本身**绝不参与 TLS 握手**：它只是一个透明的字节接收器，
//! 因此可以观察到任何客户端发来的 ClientHello 明文，也能接收合成报文。

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::{Duration, Instant};

use crate::client_hello::ClientHelloInfo;
use crate::config::Config;
use crate::json::{hex, json_string};
use crate::observer::{Conclusion, NoHelloReason, Observer};

/// 读循环单次最多读取的字节数。
const READ_CHUNK: usize = 4096;

/// 连接终止方式。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Termination {
    /// 对端正常关闭写端（EOF）。
    Eof,
    /// 读超时（静默对端超过设定时长）。
    Timeout,
    /// 读错误（给出错误描述）。
    ReadError(String),
    /// 解析阶段出现硬错误（错误详情在 [`Report::error`]）。
    ParseError,
}

impl Termination {
    fn code(&self) -> &'static str {
        match self {
            Termination::Eof => "eof",
            Termination::Timeout => "timeout",
            Termination::ReadError(_) => "read_error",
            Termination::ParseError => "parse_error",
        }
    }
}

/// 一条连接的观察报告。
#[derive(Debug, Clone)]
pub struct Report {
    pub peer_addr: String,
    pub duration_ms: u128,
    pub bytes_received: usize,
    pub termination: Termination,
    /// 成功 finish 时的结论；失败时为 None（见 error）。
    pub conclusion: Option<Conclusion>,
    pub error: Option<String>,
    pub record_count: usize,
}

impl Report {
    /// 序列化为单行 JSON。
    pub fn to_json(&self) -> String {
        let mut s = String::new();
        s.push('{');
        s.push_str(&format!("\"peer_addr\":{},", json_string(&self.peer_addr)));
        s.push_str(&format!("\"duration_ms\":{},", self.duration_ms));
        s.push_str(&format!("\"bytes_received\":{},", self.bytes_received));
        s.push_str(&format!(
            "\"termination\":{},",
            json_string(self.termination.code())
        ));

        match &self.conclusion {
            Some(Conclusion::ClientHello(ch)) => {
                s.push_str("\"result\":\"client_hello\",");
                s.push_str("\"client_hello\":");
                s.push_str(&ch.to_json());
                s.push(',');
            }
            Some(Conclusion::NoClientHello(reason)) => {
                s.push_str("\"result\":\"no_client_hello\",");
                s.push_str(&format!(
                    "\"no_client_hello_reason\":{},",
                    json_string(reason.as_code())
                ));
                if let NoHelloReason::FirstHandshakeNotClientHello { handshake_type } = reason {
                    s.push_str(&format!("\"first_handshake_type\":{handshake_type},"));
                }
                if let NoHelloReason::RecordInterleaved { content_type } = reason {
                    s.push_str(&format!("\"interleaving_content_type\":{content_type},"));
                }
            }
            None => {
                s.push_str("\"result\":\"parse_error\",");
            }
        }

        if let Some(e) = &self.error {
            s.push_str(&format!("\"error\":{},", json_string(e)));
        }

        s.push_str(&format!("\"record_count\":{}", self.record_count));
        s.push('}');
        s
    }
}

/// 把 ClientHello 观察结果序列化为 JSON 对象。
pub fn client_hello_to_json(ch: &ClientHelloInfo) -> String {
    ch.to_json()
}

impl ClientHelloInfo {
    pub fn to_json(&self) -> String {
        let mut s = String::new();
        s.push('{');
        s.push_str(&format!(
            "\"legacy_version\":\"0x{:04x}\",",
            self.legacy_version
        ));
        s.push_str(&format!("\"random\":\"{}\",", hex(&self.random)));
        s.push_str(&format!("\"session_id\":\"{}\",", hex(&self.session_id)));

        // 注意：JSON 数字不支持 0x 十六进制字面量，协议值一律输出为带引号字符串。
        let suites: Vec<String> = self
            .cipher_suites
            .iter()
            .map(|v| json_string(&format!("0x{v:04x}")))
            .collect();
        s.push_str(&format!("\"cipher_suites\":[{}],", suites.join(",")));
        let grease: Vec<String> = self
            .grease_cipher_suites
            .iter()
            .map(|v| json_string(&format!("0x{v:04x}")))
            .collect();
        s.push_str(&format!("\"grease_cipher_suites\":[{}],", grease.join(",")));

        let comp: Vec<String> = self
            .compression_methods
            .iter()
            .map(|b| json_string(&format!("0x{b:02x}")))
            .collect();
        s.push_str(&format!("\"compression_methods\":[{}],", comp.join(",")));

        match &self.sni {
            Some(host) => s.push_str(&format!("\"sni\":{},", json_string(host))),
            None => s.push_str("\"sni\":null,"),
        }

        let alpn: Vec<String> = self.alpn.iter().map(|p| json_string(p)).collect();
        s.push_str(&format!("\"alpn\":[{}],", alpn.join(",")));

        let versions: Vec<String> = self
            .supported_versions
            .iter()
            .map(|v| json_string(&format!("0x{v:04x}")))
            .collect();
        s.push_str(&format!("\"supported_versions\":[{}],", versions.join(",")));
        let gv: Vec<String> = self
            .grease_versions
            .iter()
            .map(|v| json_string(&format!("0x{v:04x}")))
            .collect();
        s.push_str(&format!("\"grease_versions\":[{}],", gv.join(",")));
        let ge: Vec<String> = self
            .grease_extensions
            .iter()
            .map(|v| json_string(&format!("0x{v:04x}")))
            .collect();
        s.push_str(&format!("\"grease_extensions\":[{}],", ge.join(",")));

        let unknown: Vec<String> = self
            .unknown_extensions
            .iter()
            .map(|u| {
                format!(
                    "{{\"ext_type\":\"0x{:04x}\",\"data_len\":{},\"data_preview\":\"{}\"}}",
                    u.ext_type,
                    u.data_len,
                    hex(&u.data_preview)
                )
            })
            .collect();
        s.push_str(&format!("\"unknown_extensions\":[{}]", unknown.join(",")));

        s.push('}');
        s
    }
}

/// 处理单条连接：读到 EOF/超时/错误，持续喂入观察器，返回报告。
pub fn observe_stream(stream: &mut TcpStream, config: &Config) -> Report {
    let peer_addr = stream
        .peer_addr()
        .map(|a| a.to_string())
        .unwrap_or_else(|_| "<unknown>".to_string());

    let mut observer = Observer::new(config.clone());
    let mut buf = [0u8; READ_CHUNK];
    let mut bytes_received = 0usize;
    let start = Instant::now();

    let termination = loop {
        match stream.read(&mut buf) {
            Ok(0) => break Termination::Eof,
            Ok(n) => {
                bytes_received += n;
                if observer.feed(&buf[..n]).is_err() {
                    // 硬解析错误：停止读取；详情在 observer.error() 中。
                    break Termination::ParseError;
                }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                break Termination::Timeout;
            }
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => {
                break Termination::Timeout;
            }
            Err(e) => break Termination::ReadError(e.to_string()),
        }
    };

    let (conclusion, error) = match observer.finish() {
        Ok(c) => (Some(c), observer.error().map(|e| e.to_string())),
        Err(e) => (None, Some(e.to_string())),
    };

    Report {
        peer_addr,
        duration_ms: start.elapsed().as_millis(),
        bytes_received,
        termination,
        conclusion,
        error,
        record_count: observer.records().len(),
    }
}

/// 在 `addr` 上启动监听服务（阻塞，当前线程 accept 循环；每条连接一线程）。
///
/// `read_timeout` 为每条连接的静默超时；超时即结束观察并输出报告。
pub fn serve(
    addr: &str,
    config: Config,
    read_timeout: Duration,
) -> std::io::Result<std::net::SocketAddr> {
    let listener = TcpListener::bind(addr)?;
    let local = listener.local_addr()?;

    eprintln!("[tls-observer] listening on {local}");
    eprintln!(
        "[tls-observer] max_record_fragment={} max_handshake_message={} max_client_hello={}",
        config.max_record_fragment, config.max_handshake_message, config.max_client_hello
    );
    eprintln!("[tls-observer] this server does NOT perform any TLS handshake; send raw bytes");

    for stream in listener.incoming() {
        match stream {
            Ok(mut stream) => {
                let config = config.clone();
                std::thread::spawn(move || {
                    let _ = stream.set_read_timeout(Some(read_timeout));
                    let _ = stream.set_nodelay(true);
                    let report = observe_stream(&mut stream, &config);
                    let mut stdout = std::io::stdout().lock();
                    let _ = writeln!(stdout, "{}", report.to_json());
                    let _ = stdout.flush();
                });
            }
            Err(e) => {
                eprintln!("[tls-observer] accept failed: {e}");
            }
        }
    }
    Ok(local)
}
