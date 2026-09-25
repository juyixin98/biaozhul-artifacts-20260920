//! # 本地 TCP 测试服务：JSON 行协议
//!
//! 服务只监听 **127.0.0.1**，不需要原始套接字或任何特权。每条连接上按
//! “一行一个 JSON 请求 / 一行一个 JSON 响应”通信。服务端以系统时间作为
//! 重组时钟；协议额外提供 `now_ms` 可注入字段（缺省取系统时钟），
//! 便于自动化测试确定性地验证 TTL。
//!
//! ## 请求一览
//!
//! ```jsonc
//! {"op":"ping"}
//! {"op":"frag","src":"10.0.0.1","dst":"10.0.0.2","protocol":17,"id":1,
//!  "offset":0,"mf":true,"payload_hex":"0102...","now_ms":0}
//! {"op":"status"}                                   // 所有未完成组装
//! {"op":"status","src":"...","dst":"...","protocol":17,"id":1}
//! {"op":"purge","now_ms":5000}
//! {"op":"stats"}
//! {"op":"config"}
//! ```
//!
//! 完成重组时 `frag` 响应携带完整数据报的长度与 SHA-256 指纹
//! （以及可选的 `include_payload` 回显），客户端据此与本地原始载荷比对。

use crate::json::{self, arr, b, i, obj, s, JsonValue};
use crate::reassembly::{
    FlowKey, OverlapPolicy, ReassembleError, ReassembleEvent, ReassemblerConfig, ReassemblyEngine,
};
use crate::{parse_ipv4, sha256_hex};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

/// 服务端共享状态。计数器（完成数、拒绝数等）都保存在 engine.stats 中。
#[derive(Debug)]
pub struct ServerState {
    pub engine: ReassemblyEngine,
}

impl ServerState {
    pub fn new(config: ReassemblerConfig) -> Self {
        ServerState {
            engine: ReassemblyEngine::new(config),
        }
    }
}

/// 当前 UNIX 毫秒时间戳。
pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn err_json(code: &str, message: String) -> JsonValue {
    obj(vec![
        ("ok", b(false)),
        ("error", s(code)),
        ("message", s(message)),
    ])
}

fn flow_to_json(k: &FlowKey) -> JsonValue {
    obj(vec![
        ("src", s(k.src.to_string())),
        ("dst", s(k.dst.to_string())),
        ("protocol", i(k.protocol as i64)),
        ("id", i(k.identification as i64)),
    ])
}

fn parse_flow(v: &JsonValue) -> Result<FlowKey, String> {
    let src = v
        .str_field("src")?
        .parse()
        .map_err(|e: std::net::AddrParseError| e.to_string())?;
    let dst = v
        .str_field("dst")?
        .parse()
        .map_err(|e: std::net::AddrParseError| e.to_string())?;
    let protocol = v.int_field("protocol")?;
    if !(0..=255).contains(&protocol) {
        return Err("protocol must be in 0..=255".to_string());
    }
    let id = v.int_field("id")?;
    if !(0..=u16::MAX as i64).contains(&id) {
        return Err("id must be in 0..=65535".to_string());
    }
    Ok(FlowKey {
        src,
        dst,
        protocol: protocol as u8,
        identification: id as u16,
    })
}

/// 十六进制解码（忽略空白），奇数长度/非 hex 字符报错。
pub fn decode_hex(input: &str) -> Result<Vec<u8>, String> {
    let cleaned: String = input.chars().filter(|c| !c.is_whitespace()).collect();
    if !cleaned.len().is_multiple_of(2) {
        return Err("hex payload must have an even number of digits".to_string());
    }
    let mut out = Vec::with_capacity(cleaned.len() / 2);
    let bytes = cleaned.as_bytes();
    let mut pos = 0;
    while pos < bytes.len() {
        let hi = hex_nibble(bytes[pos])?;
        let lo = hex_nibble(bytes[pos + 1])?;
        out.push((hi << 4) | lo);
        pos += 2;
    }
    Ok(out)
}

fn hex_nibble(c: u8) -> Result<u8, String> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        _ => Err(format!("invalid hex digit `{}`", c as char)),
    }
}

/// 重组错误 -> (error code, message)。
fn reassemble_error_json(e: &ReassembleError) -> JsonValue {
    let code = match e {
        ReassembleError::Overlap { .. } => "overlap",
        ReassembleError::ConflictingData { .. } => "conflicting_data",
        ReassembleError::ConflictingLastFragment { .. } => "conflicting_last_fragment",
        ReassembleError::DatagramTooLarge { .. } => "datagram_too_large",
        ReassembleError::MemoryBudgetExceeded { .. } => "memory_budget_exceeded",
        ReassembleError::TooManyAssemblies { .. } => "too_many_assemblies",
    };
    err_json(code, e.to_string())
}

/// 单个请求的处理（纯函数式：操作共享引擎，返回一个响应 JSON）。
///
/// 抽成独立函数，集成测试可以不开 TCP 直接驱动协议语义。
pub fn handle_request(state: &mut ServerState, request: &JsonValue, sys_now: u64) -> JsonValue {
    let op = match request.str_field("op") {
        Ok(op) => op,
        Err(e) => return err_json("bad_request", e),
    };

    match op {
        "ping" => obj(vec![("ok", b(true)), ("pong", b(true))]),

        "config" => {
            let c = &state.engine.config();
            obj(vec![
                ("ok", b(true)),
                (
                    "config",
                    obj(vec![
                        ("assembly_ttl_ms", i(c.assembly_ttl_ms as i64)),
                        ("purge_interval_ms", i(c.purge_interval_ms as i64)),
                        ("mem_budget_bytes", i(c.mem_budget_bytes as i64)),
                        ("max_datagram_bytes", i(c.max_datagram_bytes as i64)),
                        ("max_assemblies", i(c.max_assemblies as i64)),
                        (
                            "overlap_policy",
                            s(match c.overlap_policy {
                                OverlapPolicy::RejectNewFragment => "reject_new_fragment",
                                OverlapPolicy::DropAssembly => "drop_assembly",
                            }),
                        ),
                    ]),
                ),
            ])
        }

        "stats" => {
            let st = &state.engine.stats;
            obj(vec![
                ("ok", b(true)),
                (
                    "stats",
                    obj(vec![
                        ("fragments_received", i(st.fragments_received as i64)),
                        ("duplicates", i(st.duplicates as i64)),
                        ("overlaps_rejected", i(st.overlaps_rejected as i64)),
                        ("datagrams_completed", i(st.datagrams_completed as i64)),
                        ("id_reuse_resets", i(st.id_reuse_resets as i64)),
                        ("assemblies_expired", i(st.assemblies_expired as i64)),
                        ("bytes_reassembled", i(st.bytes_reassembled as i64)),
                        ("bytes_used", i(state.engine.bytes_used() as i64)),
                        ("assembly_count", i(state.engine.assembly_count() as i64)),
                    ]),
                ),
            ])
        }

        "purge" => {
            let now = request
                .optional_i64("now_ms")
                .map(|v| v as u64)
                .unwrap_or(sys_now);
            let report = state.engine.purge_expired(now);
            obj(vec![
                ("ok", b(true)),
                ("removed_assemblies", i(report.removed_assemblies as i64)),
                ("reclaimed_bytes", i(report.reclaimed_bytes as i64)),
                (
                    "removed",
                    arr(report.keys.iter().map(flow_to_json).collect()),
                ),
            ])
        }

        "status" => {
            let now = request
                .optional_i64("now_ms")
                .map(|v| v as u64)
                .unwrap_or(sys_now);
            let list = if let Ok(key) = parse_flow(request) {
                state
                    .engine
                    .status(&key, now)
                    .map(|single| vec![single])
                    .unwrap_or_default()
            } else if request.get("src").is_some()
                || request.get("dst").is_some()
                || request.get("protocol").is_some()
                || request.get("id").is_some()
            {
                // 出现了部分四元组字段但解析失败 -> 参数错误
                return match parse_flow(request) {
                    Ok(_) => unreachable!(),
                    Err(e) => err_json("bad_flow", e),
                };
            } else {
                state.engine.list_status(now)
            };
            obj(vec![
                ("ok", b(true)),
                ("count", i(list.len() as i64)),
                (
                    "assemblies",
                    arr(list.into_iter().map(status_to_json).collect()),
                ),
            ])
        }

        "frag" => handle_frag(state, request, sys_now),

        other => err_json(
            "unknown_op",
            format!("unknown op `{other}`; expected one of ping/frag/status/purge/stats/config"),
        ),
    }
}

fn status_to_json(st: crate::reassembly::AssemblyStatus) -> JsonValue {
    let mut pairs = vec![
        ("flow", flow_to_json(&st.key)),
        ("buffered_bytes", i(st.buffered_bytes as i64)),
        ("fragment_count", i(st.fragment_count as i64)),
        ("has_first_fragment", b(st.has_first_fragment)),
        ("has_last_fragment", b(st.has_last_fragment)),
        ("contiguous_prefix", i(st.contiguous_prefix as i64)),
        ("age_ms", i(st.age_ms as i64)),
    ];
    if let Some(total) = st.expected_total {
        pairs.push(("expected_total_bytes", i(total as i64)));
    }
    obj(pairs)
}

fn handle_frag(state: &mut ServerState, request: &JsonValue, sys_now: u64) -> JsonValue {
    // 两种输入形式：
    //  1) 原始 IPv4 报文：{"packet_hex":"4500...."} —— 走手写解析器；
    //  2) 显式分片字段：flow 四元组 + offset + mf + payload_hex。
    let now = request
        .optional_i64("now_ms")
        .map(|v| v as u64)
        .unwrap_or(sys_now);

    let event = if let Some(packet_hex) = request.optional_str("packet_hex") {
        let raw = match decode_hex(packet_hex) {
            Ok(raw) => raw,
            Err(e) => return err_json("bad_hex", e),
        };
        let pkt = match parse_ipv4(&raw) {
            Ok(p) => p,
            Err(e) => return err_json("bad_packet", e.to_string()),
        };
        // RFC 791：MF=1 的片，其负载必须是 8 字节的整数倍。
        if pkt.is_fragment() && pkt.more_fragments && pkt.payload.len() % 8 != 0 {
            return err_json(
                "bad_packet",
                "non-final fragment (MF=1) payload must be a multiple of 8 bytes".to_string(),
            );
        }
        state.engine.insert_packet(&pkt, now)
    } else {
        let key = match parse_flow(request) {
            Ok(k) => k,
            Err(e) => return err_json("bad_flow", e),
        };
        let offset = match request.int_field("offset") {
            Ok(v) if v >= 0 => v as usize,
            Ok(_) => return err_json("bad_request", "offset must be non-negative".to_string()),
            Err(e) => return err_json("bad_request", e),
        };
        if offset % 8 != 0 {
            return err_json(
                "bad_request",
                "fragment offset must be a multiple of 8 bytes".to_string(),
            );
        }
        let mf = match request.get("mf").and_then(|v| v.as_bool()) {
            Some(mf) => mf,
            None => return err_json("bad_request", "missing boolean field `mf`".to_string()),
        };
        let payload_hex = request.optional_str("payload_hex").unwrap_or_default();
        let payload = match decode_hex(payload_hex) {
            Ok(p) => p,
            Err(e) => return err_json("bad_hex", e),
        };
        // RFC 791：除末片（MF=0）外，每个片的负载必须是 8 字节的整数倍，
        // 否则后续片无法落在合法的 8 字节偏移边界上。
        if mf && payload.len() % 8 != 0 {
            return err_json(
                "bad_request",
                "non-final fragment (mf=true) payload must be a multiple of 8 bytes".to_string(),
            );
        }
        state.engine.insert_fragment(
            crate::reassembly::FragmentInfo {
                key,
                offset,
                payload,
                is_last: !mf,
            },
            now,
        )
    };

    let include_payload = request
        .get("include_payload")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    match event {
        Ok(ev) => event_to_json(ev, include_payload),
        Err(e) => reassemble_error_json(&e),
    }
}

fn event_to_json(ev: ReassembleEvent, include_payload: bool) -> JsonValue {
    match ev {
        ReassembleEvent::Accepted {
            key,
            buffered_bytes,
            fragment_count,
            has_first,
            has_last,
        } => obj(vec![
            ("ok", b(true)),
            ("event", s("accepted")),
            ("flow", flow_to_json(&key)),
            ("buffered_bytes", i(buffered_bytes as i64)),
            ("fragment_count", i(fragment_count as i64)),
            ("has_first_fragment", b(has_first)),
            ("has_last_fragment", b(has_last)),
        ]),
        ReassembleEvent::Duplicate { key } => obj(vec![
            ("ok", b(true)),
            ("event", s("duplicate")),
            ("flow", flow_to_json(&key)),
        ]),
        ReassembleEvent::IdReuseReset {
            key,
            discarded_bytes,
        } => obj(vec![
            ("ok", b(true)),
            ("event", s("id_reuse_reset")),
            ("flow", flow_to_json(&key)),
            ("discarded_bytes", i(discarded_bytes as i64)),
        ]),
        ReassembleEvent::Completed {
            key,
            data,
            fragment_count,
        } => {
            let digest = sha256_hex(&data);
            let mut pairs = vec![
                ("ok", b(true)),
                ("event", s("completed")),
                ("flow", flow_to_json(&key)),
                ("total_bytes", i(data.len() as i64)),
                ("fragment_count", i(fragment_count as i64)),
                ("sha256", s(digest)),
            ];
            if include_payload {
                pairs.push(("payload_hex", s(hex_encode(&data))));
            }
            obj(pairs)
        }
    }
}

/// 字节 -> 小写十六进制。
pub fn hex_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len() * 2);
    for byte in data {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// 处理一条 TCP 连接上的全部请求行。
pub fn serve_connection(state: Arc<Mutex<ServerState>>, stream: TcpStream) -> std::io::Result<()> {
    let peer = stream.peer_addr().ok();
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut writer = stream;
    let mut line = String::new();

    loop {
        line.clear();
        let n = reader.read_line(&mut line)?;
        if n == 0 {
            break; // 对端关闭
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let response = match json::parse(trimmed) {
            Ok(req) => {
                let mut guard = state.lock().expect("engine mutex poisoned");
                handle_request(&mut guard, &req, now_ms())
            }
            Err(e) => err_json("bad_json", e),
        };
        let serialized = json::to_string(&response);
        writeln!(writer, "{serialized}")?;
        writer.flush()?;
    }
    if let Some(peer) = peer {
        eprintln!("connection from {peer} closed");
    }
    Ok(())
}

/// 绑定 127.0.0.1:`port` 并接受连接直到出错/进程退出。`port = 0` 由系统分配。
/// 返回实际绑定的端口。
pub fn run_tcp(
    state: Arc<Mutex<ServerState>>,
    port: u16,
    shutdown: Arc<Mutex<bool>>,
) -> std::io::Result<u16> {
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    let actual = listener.local_addr()?.port();
    eprintln!("ipfrag-server listening on 127.0.0.1:{actual}");

    // 非阻塞轮询 shutdown 标志，避免依赖外部 signal crate。
    listener.set_nonblocking(true)?;
    loop {
        if *shutdown.lock().unwrap() {
            eprintln!("shutdown requested, stopping accept loop");
            break;
        }
        match listener.accept() {
            Ok((stream, peer)) => {
                eprintln!("accepted connection from {peer}");
                let state = Arc::clone(&state);
                // 单连接一个线程，测试服务不需要高并发；线程模型刻意保持简单。
                std::thread::spawn(move || {
                    if let Err(e) = serve_connection(state, stream) {
                        eprintln!("connection error from {peer}: {e}");
                    }
                });
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
            Err(e) => return Err(e),
        }
    }
    Ok(actual)
}

/// 便捷方法：阻塞读取整个 `Read`（主要用于一次性客户端/测试）。
pub fn read_all_json_lines<R: Read>(reader: R) -> Vec<String> {
    let buf = BufReader::new(reader);
    buf.lines().map_while(Result::ok).collect()
}
