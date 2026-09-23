//! 本地 TCP 测试服务：MQTT 3.1.1 子集 broker。
//!
//! 纯标准库实现，线程模型极简：
//! - accept 线程接受连接（非阻塞轮询以便干净停机）；
//! - 每条连接一个处理线程，自带增量 [`Decoder`]；
//! - 一个重发定时器线程周期性扫描所有会话，重发超时的在途 QoS1 消息（DUP=1）。
//!
//! 语义边界（务必与 README 一致）：
//! - **仅保证至少一次**：QoS1 消息在 PUBACK 前持续重发；消费者可能收到重复消息。
//! - QoS2 PUBLISH 不属于子集：连接后收到即按协议错误断开。
//! - 遗嘱（Will）不属于子集：CONNECT 带 Will Flag 时返回 CONNACK 0x03。

use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::codec::{
    encode_connack, encode_pingresp, encode_puback, encode_publish, encode_suback, Decoder,
};
use crate::error::ConAckReason;
use crate::packet::Packet;
use crate::session::{InFlight, QueuedMessage, Session, MAX_OFFLINE_PER_SESSION};
use crate::topic;

/// Broker 可调参数。
#[derive(Debug, Clone)]
pub struct BrokerConfig {
    pub bind_addr: String,
    pub max_packet_size: usize,
    /// 在途 QoS1 消息多久未收到 PUBACK 就重发。
    pub retry_interval: Duration,
    /// Keep Alive 宽限倍数（§3.1.2.10：服务端可在 1.5 倍后断开）。
    pub keepalive_multiplier: f64,
    /// 静默运行（测试时关闭 eprintln 日志）。
    pub quiet: bool,
}

impl Default for BrokerConfig {
    fn default() -> Self {
        Self {
            bind_addr: "127.0.0.1:1883".to_string(),
            max_packet_size: crate::codec::DEFAULT_MAX_PACKET_SIZE,
            retry_interval: Duration::from_secs(2),
            keepalive_multiplier: 1.5,
            quiet: false,
        }
    }
}

/// 保留消息存储项（§3.3.1.3）：载荷 + 发布时 QoS。
#[derive(Debug, Clone)]
struct Retained {
    payload: Vec<u8>,
    qos: u8,
}

/// 可观测计数（测试直接断言这些值）。
#[derive(Debug, Default)]
pub struct BrokerStats {
    pub connect_accepted: AtomicU64,
    pub connect_rejected: AtomicU64,
    pub publish_received: AtomicU64,
    pub duplicate_publishes_suppressed: AtomicU64,
    pub publish_delivered_qos0: AtomicU64,
    pub publish_delivered_qos1: AtomicU64,
    pub puback_received: AtomicU64,
    pub puback_unknown: AtomicU64,
    pub subscribe_received: AtomicU64,
    /// 因会话离线队列满 / 在途ID池满 / 非持久会话离线 而丢弃的转发次数。
    pub publishes_dropped: AtomicU64,
    /// 收到子集外的 QoS2 PUBLISH 而断开的连接数。
    pub qos2_rejected: AtomicU64,
}

struct Inner {
    sessions: HashMap<String, Arc<Mutex<Session>>>,
    retained: HashMap<String, Retained>,
    anonymous_seq: u64,
}

/// Broker 句柄；drop 不会自动停机，请显式调用 [`Broker::shutdown`]。
pub struct Broker {
    listener: TcpListener,
    inner: Arc<Mutex<Inner>>,
    stats: Arc<BrokerStats>,
    shutdown_flag: Arc<AtomicBool>,
    threads: Vec<JoinHandle<()>>,
}

impl Broker {
    /// 绑定端口并启动 accept / 重发线程。
    pub fn start(config: BrokerConfig) -> io::Result<Broker> {
        let listener = TcpListener::bind(&config.bind_addr)?;
        listener.set_nonblocking(true)?;

        let inner = Arc::new(Mutex::new(Inner {
            sessions: HashMap::new(),
            retained: HashMap::new(),
            anonymous_seq: 0,
        }));
        let stats = Arc::new(BrokerStats::default());
        let shutdown_flag = Arc::new(AtomicBool::new(false));

        let mut threads = Vec::new();

        // accept 线程。
        {
            let inner = Arc::clone(&inner);
            let stats = Arc::clone(&stats);
            let cfg = config.clone();
            let flag = Arc::clone(&shutdown_flag);
            let listener = listener.try_clone()?;
            threads.push(thread::spawn(move || {
                accept_loop(listener, inner, stats, cfg, flag)
            }));
        }

        // 重发定时器线程（200ms 扫描粒度）。
        {
            let inner = Arc::clone(&inner);
            let cfg = config.clone();
            let flag = Arc::clone(&shutdown_flag);
            threads.push(thread::spawn(move || retry_loop(inner, cfg, flag)));
        }

        Ok(Broker {
            listener,
            inner,
            stats,
            shutdown_flag,
            threads,
        })
    }

    pub fn local_addr(&self) -> io::Result<std::net::SocketAddr> {
        self.listener.local_addr()
    }

    pub fn stats(&self) -> Arc<BrokerStats> {
        Arc::clone(&self.stats)
    }

    /// 测试辅助：当前会话数。
    pub fn session_count(&self) -> usize {
        self.inner.lock().unwrap().sessions.len()
    }

    /// 测试辅助：某持久会话的离线队列长度。
    pub fn offline_len(&self, client_id: &str) -> Option<usize> {
        self.inner
            .lock()
            .unwrap()
            .sessions
            .get(client_id)
            .map(|s| s.lock().unwrap().offline.len())
    }

    /// 停止接受新连接、结束后台线程（已建立的连接随处理线程读错退出）。
    pub fn shutdown(mut self) {
        self.shutdown_flag.store(true, Ordering::SeqCst);
        // 主动关闭监听套接字并解除 accept 阻塞。
        // （TcpListener 没有 take_shutdown，依赖标志位 + 非阻塞轮询。）
        for t in self.threads.drain(..) {
            let _ = t.join();
        }
    }
}

fn log(cfg: &BrokerConfig, msg: impl Into<String>) {
    if !cfg.quiet {
        eprintln!("[mqtt-subset] {}", msg.into());
    }
}

fn accept_loop(
    listener: TcpListener,
    inner: Arc<Mutex<Inner>>,
    stats: Arc<BrokerStats>,
    cfg: BrokerConfig,
    shutdown_flag: Arc<AtomicBool>,
) {
    while !shutdown_flag.load(Ordering::SeqCst) {
        match listener.accept() {
            Ok((stream, addr)) => {
                let inner = Arc::clone(&inner);
                let stats = Arc::clone(&stats);
                let cfg = cfg.clone();
                let flag = Arc::clone(&shutdown_flag);
                thread::spawn(move || {
                    if let Err(e) = handle_connection(inner, stats, cfg.clone(), flag, stream) {
                        log(&cfg, format!("connection {addr} ended with error: {e}"));
                    }
                });
            }
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(50));
            }
            Err(e) => {
                if !shutdown_flag.load(Ordering::SeqCst) {
                    log(&cfg, format!("accept error: {e}"));
                }
                thread::sleep(Duration::from_millis(50));
            }
        }
    }
}

/// 重发循环：扫描所有在线会话，把超时在途消息以 DUP=1 重发。
fn retry_loop(inner: Arc<Mutex<Inner>>, cfg: BrokerConfig, shutdown_flag: Arc<AtomicBool>) {
    while !shutdown_flag.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(200));

        // 先在锁内收集需要重发的 (会话, 包ID列表)，避免持锁写 socket。
        let due: Vec<(Arc<Mutex<Session>>, Vec<u16>)> = {
            let guard = inner.lock().unwrap();
            guard
                .sessions
                .values()
                .filter_map(|s_arc| {
                    let s = s_arc.lock().unwrap();
                    if !s.connected {
                        return None;
                    }
                    let now = Instant::now();
                    let mut ids = Vec::new();
                    for (id, msg) in s.inflight.iter() {
                        if now.duration_since(msg.last_sent) >= cfg.retry_interval {
                            ids.push(*id);
                        }
                    }
                    if ids.is_empty() {
                        None
                    } else {
                        Some((Arc::clone(s_arc), ids))
                    }
                })
                .collect()
        };

        for (s_arc, ids) in due {
            let mut s = s_arc.lock().unwrap();
            if !s.connected {
                continue;
            }
            for id in ids {
                let Some(msg) = s.inflight.get(&id) else {
                    continue;
                };
                let bytes = encode_publish(
                    &msg.topic,
                    Some(id),
                    &msg.payload,
                    1,
                    true, // 重发一律 DUP=1（§3.3.1.1）
                    msg.retain,
                );
                match write_session(&mut s, &bytes) {
                    Ok(()) => {
                        if let Some(m) = s.inflight.get_mut(&id) {
                            m.last_sent = Instant::now();
                            m.retries += 1;
                        }
                    }
                    Err(_) => {
                        // 写失败：留在 inflight 中；连接线程很快会感知断连并 detach。
                        break;
                    }
                }
            }
        }
    }
}

/// 向会话当前连接写一帧（调用方必须持有会话锁）。
fn write_session(s: &mut Session, bytes: &[u8]) -> io::Result<()> {
    match s.stream.as_mut() {
        Some(stream) => stream.write_all(bytes),
        None => Err(io::Error::new(
            io::ErrorKind::NotConnected,
            "session offline",
        )),
    }
}

fn handle_connection(
    inner: Arc<Mutex<Inner>>,
    stats: Arc<BrokerStats>,
    cfg: BrokerConfig,
    shutdown_flag: Arc<AtomicBool>,
    stream: TcpStream,
) -> io::Result<()> {
    stream.set_nodelay(true)?;
    stream.set_read_timeout(Some(Duration::from_millis(200)))?;

    let mut reader = stream;
    let mut decoder = Decoder::new(cfg.max_packet_size);
    let mut buf = [0u8; 8192];

    // CONNECT 建立后赋值：(会话句柄, 本连接的代号)。
    let mut bound: Option<(Arc<Mutex<Session>>, u64)> = None;

    'conn: loop {
        if shutdown_flag.load(Ordering::SeqCst) {
            break;
        }
        let n = match reader.read(&mut buf) {
            Ok(0) => break 'conn,
            Ok(n) => n,
            Err(ref e)
                if e.kind() == io::ErrorKind::WouldBlock || e.kind() == io::ErrorKind::TimedOut =>
            {
                // 用读超时周期检查 Keep Alive 与停机标志。
                if let Some((s_arc, gen)) = &bound {
                    let s = s_arc.lock().unwrap();
                    if s.generation != *gen {
                        break 'conn; // 被同名新连接接管。
                    }
                    if s.keep_alive_secs > 0 {
                        let limit = Duration::from_secs_f64(
                            s.keep_alive_secs as f64 * cfg.keepalive_multiplier,
                        );
                        if s.last_activity.elapsed() > limit {
                            log(&cfg, format!("client {} keep-alive timeout", s.client_id));
                            break 'conn;
                        }
                    }
                }
                continue;
            }
            Err(_) => break 'conn,
        };

        decoder.feed(&buf[..n]);

        loop {
            if shutdown_flag.load(Ordering::SeqCst) {
                break 'conn;
            }
            let packet = match decoder.try_parse() {
                Ok(None) => break, // 等待更多字节（半包）。
                Ok(Some(p)) => p,
                Err(codec_err) => {
                    // CONNECT 前只对「可表达为 CONNACK」的错误回包；其余静默断开。
                    if bound.is_none() {
                        use crate::error::CodecError::*;
                        let reason = match &codec_err {
                            UnsupportedProtocolLevel(_) => {
                                Some(ConAckReason::UnacceptableProtocolVersion)
                            }
                            _ => None,
                        };
                        if let Some(r) = reason {
                            let _ = reader.write_all(&encode_connack(false, r.as_u8()));
                            stats.connect_rejected.fetch_add(1, Ordering::Relaxed);
                        }
                        log(&cfg, format!("pre-CONNECT parse error: {codec_err}"));
                    } else {
                        log(&cfg, format!("protocol error, closing: {codec_err}"));
                    }
                    break 'conn;
                }
            };

            if let Some(ref bound) = bound {
                let (s_arc, gen) = bound;
                // 被同名新连接接管的旧线程：立即退出（不做清理，新连接是当前持有者）。
                if s_arc.lock().unwrap().generation != *gen {
                    return Ok(());
                }
                match dispatch(&inner, &stats, &cfg, s_arc, *gen, &mut reader, packet) {
                    Ok(true) => return Ok(()), // DISCONNECT：干净结束，无需异常清理。
                    Ok(false) => {}
                    Err(e) => {
                        log(&cfg, format!("dispatch I/O error: {e}"));
                        break 'conn;
                    }
                }
            } else {
                // 连接建立前只允许 CONNECT（§3.1.0）。
                match packet {
                    Packet::Connect(c) => match on_connect(&inner, &stats, &cfg, &mut reader, c) {
                        Ok(b) => bound = Some(b),
                        Err(()) => return Ok(()), // 已发拒绝 CONNACK 或直接断开。
                    },
                    _ => {
                        log(&cfg, "first packet was not CONNECT; closing");
                        return Ok(());
                    }
                }
            }
        }
    }

    // 异常/EOF/超时清理（DISCONNECT 已自行处理并提前返回）。
    if let Some((s_arc, gen)) = bound {
        cleanup_disconnect(&inner, s_arc, gen);
    }
    Ok(())
}

/// 处理 CONNECT，成功返回 (会话, 连接代号)；失败已写 CONNACK 或直接断开。
fn on_connect(
    inner: &Arc<Mutex<Inner>>,
    stats: &Arc<BrokerStats>,
    cfg: &BrokerConfig,
    reader: &mut TcpStream,
    c: crate::packet::Connect,
) -> Result<(Arc<Mutex<Session>>, u64), ()> {
    // 子集不支持遗嘱：明确拒绝（而非静默忽略）。
    if c.will_flag {
        let _ = reader.write_all(&encode_connack(
            false,
            ConAckReason::ServerUnavailable.as_u8(),
        ));
        stats.connect_rejected.fetch_add(1, Ordering::Relaxed);
        log(cfg, "CONNECT rejected: Will flag set (outside subset)");
        return Err(());
    }

    let client_id = if c.client_id.is_empty() {
        if c.clean_session {
            // §3.1.3-6：允许服务端为 CleanSession=1 的空 ClientID 客户端分配唯一ID。
            let mut g = inner.lock().unwrap();
            g.anonymous_seq += 1;
            format!(
                "anonymous-{}-{:x}",
                g.anonymous_seq,
                uptime_seed() & 0xFFFF_FFFF
            )
        } else {
            let _ = reader.write_all(&encode_connack(
                false,
                ConAckReason::IdentifierRejected.as_u8(),
            ));
            stats.connect_rejected.fetch_add(1, Ordering::Relaxed);
            log(
                cfg,
                "CONNECT rejected: empty client id without CleanSession",
            );
            return Err(());
        }
    } else {
        c.client_id.clone()
    };

    // 取出/创建会话（持 broker 锁完成替换，避免重复客户端竞争）。
    let (s_arc, session_present) = {
        let mut g = inner.lock().unwrap();

        // 同名旧会话：先踢旧连接线程（代号递增），再按 CleanSession 决定保留或丢弃。
        if let Some(old_arc) = g.sessions.get(&client_id) {
            let mut old = old_arc.lock().unwrap();
            old.generation = old.generation.wrapping_add(1);
            old.stream = None;
            old.connected = false;
        }

        if c.clean_session {
            // §3.1.2.4：CleanSession=1 必须丢弃旧会话的全部状态。
            g.sessions.remove(&client_id);
            let s_arc = Arc::new(Mutex::new(Session::new(client_id.clone(), true)));
            g.sessions.insert(client_id.clone(), Arc::clone(&s_arc));
            (s_arc, false)
        } else {
            match g.sessions.get(&client_id) {
                Some(s_arc) => {
                    let sp = true;
                    (Arc::clone(s_arc), sp)
                }
                None => {
                    let s_arc = Arc::new(Mutex::new(Session::new(client_id.clone(), false)));
                    g.sessions.insert(client_id.clone(), Arc::clone(&s_arc));
                    (s_arc, false)
                }
            }
        }
    };

    // 接受连接：挂上本连接的可写 socket 副本。
    let writer = reader.try_clone().map_err(|_| ())?;
    let generation = {
        let mut s = s_arc.lock().unwrap();
        s.stream = Some(writer);
        s.connected = true;
        s.keep_alive_secs = c.keep_alive_secs;
        s.touch();
        s.generation
    };

    reader
        .write_all(&encode_connack(
            session_present,
            ConAckReason::Accepted.as_u8(),
        ))
        .map_err(|_| ())?;
    stats.connect_accepted.fetch_add(1, Ordering::Relaxed);
    log(
        cfg,
        format!(
            "client {client_id} connected (session_present={session_present}, clean={})",
            c.clean_session
        ),
    );

    // 持久会话重连：先重发在途消息（DUP=1），再发离线队列。
    if session_present {
        flush_after_reconnect(&s_arc, cfg);
    }

    Ok((s_arc, generation))
}

fn uptime_seed() -> u64 {
    use std::time::SystemTime;
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

/// 重连后恢复投递：在途消息 DUP=1 重发；离线消息分配新包ID 后发送。
fn flush_after_reconnect(s_arc: &Arc<Mutex<Session>>, cfg: &BrokerConfig) {
    let mut s = s_arc.lock().unwrap();
    let client_id = s.client_id.clone();

    // 1) 重发在途消息，保持原有包标识符。
    let inflight_ids: Vec<u16> = s.inflight.keys().copied().collect();
    for id in inflight_ids {
        let Some(msg) = s.inflight.get(&id) else {
            continue;
        };
        let bytes = encode_publish(&msg.topic, Some(id), &msg.payload, 1, true, msg.retain);
        match write_session(&mut s, &bytes) {
            Ok(()) => {
                if let Some(m) = s.inflight.get_mut(&id) {
                    m.last_sent = Instant::now();
                    m.retries += 1;
                }
            }
            Err(e) => {
                log(cfg, format!("resend to {client_id} failed: {e}"));
                return;
            }
        }
    }

    // 2) 离线队列（FIFO），逐条分配包标识符。
    let queued: Vec<QueuedMessage> = s.offline.drain(..).collect();
    for msg in queued {
        let Some(id) = s.allocate_packet_id() else {
            // ID 池满：放回队首等待下次重连/定时器，保持顺序。
            s.offline.push_front(msg);
            break;
        };
        let bytes = encode_publish(&msg.topic, Some(id), &msg.payload, 1, false, false);
        match write_session(&mut s, &bytes) {
            Ok(()) => s.track_inflight(
                id,
                InFlight {
                    topic: msg.topic,
                    payload: msg.payload,
                    retain: false,
                    last_sent: Instant::now(),
                    retries: 0,
                },
            ),
            Err(e) => {
                log(cfg, format!("offline flush to {client_id} failed: {e}"));
                // 未发出的消息放回离线队列（已 drain，重新前插保持顺序）。
                s.offline.push_front(msg);
                break;
            }
        }
    }
}

/// 连接建立后分发单个报文；返回 Ok(true) 表示收到 DISCONNECT，线程应干净退出。
#[allow(clippy::too_many_arguments)]
fn dispatch(
    inner: &Arc<Mutex<Inner>>,
    stats: &Arc<BrokerStats>,
    cfg: &BrokerConfig,
    s_arc: &Arc<Mutex<Session>>,
    generation: u64,
    reader: &mut TcpStream,
    packet: Packet,
) -> io::Result<bool> {
    match packet {
        Packet::Pingreq => {
            s_arc.lock().unwrap().touch();
            reader.write_all(&encode_pingresp())?;
        }
        Packet::Disconnect => {
            graceful_disconnect(inner, s_arc, generation);
            return Ok(true);
        }
        Packet::Connect(_) => {
            // §3.1.0：每条 TCP 连接只能有一个 CONNECT。
            log(cfg, "second CONNECT on same connection; closing");
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "duplicate CONNECT",
            ));
        }
        Packet::Puback(id) => {
            let mut s = s_arc.lock().unwrap();
            s.touch();
            let client_id = s.client_id.clone();
            match s.acknowledge(id) {
                Some(_) => {
                    stats.puback_received.fetch_add(1, Ordering::Relaxed);
                }
                None => {
                    stats.puback_unknown.fetch_add(1, Ordering::Relaxed);
                    log(
                        cfg,
                        format!("client {client_id} sent PUBACK for unknown id {id}"),
                    );
                }
            }
        }
        Packet::Publish(p) => {
            handle_publish(inner, stats, cfg, s_arc, reader, p)?;
        }
        Packet::Subscribe(id, filters) => {
            let filter_refs: Vec<(&str, u8)> =
                filters.iter().map(|(f, q)| (f.as_str(), *q)).collect();
            handle_subscribe(inner, stats, cfg, s_arc, reader, id, &filter_refs)?;
        }
    }
    Ok(false)
}

/// 处理入站 PUBLISH（含入站去重、保留消息更新、路由）。
fn handle_publish(
    inner: &Arc<Mutex<Inner>>,
    stats: &Arc<BrokerStats>,
    cfg: &BrokerConfig,
    s_arc: &Arc<Mutex<Session>>,
    reader: &mut TcpStream,
    p: crate::packet::Publish,
) -> io::Result<()> {
    {
        let mut s = s_arc.lock().unwrap();
        s.touch();
    }

    // 子集外的 QoS2：明确按协议错误断开（而不是悄悄降级）。
    if p.qos == 2 {
        stats.qos2_rejected.fetch_add(1, Ordering::Relaxed);
        log(
            cfg,
            "QoS2 PUBLISH is outside the subset; closing connection",
        );
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "QoS2 not supported",
        ));
    }

    stats.publish_received.fetch_add(1, Ordering::Relaxed);

    // 入站去重（仅 QoS1）：同包ID+同指纹视为重传，只补 PUBACK、不重复转发。
    let mut duplicate = false;
    if let Some(id) = p.packet_id {
        let fp = Session::fingerprint(&p.topic, &p.payload, p.qos, p.retain);
        let mut s = s_arc.lock().unwrap();
        duplicate = s.register_inbound(id, fp);
    }
    if duplicate {
        stats
            .duplicate_publishes_suppressed
            .fetch_add(1, Ordering::Relaxed);
        log(
            cfg,
            format!(
                "duplicate QoS1 PUBLISH id={} topic={} suppressed (will still PUBACK)",
                p.packet_id.unwrap(),
                p.topic
            ),
        );
    } else {
        route_publish(inner, stats, cfg, &p.topic, &p.payload, p.qos, p.retain);
    }

    // 无论是否重复，QoS1 都回 PUBACK（重传的典型原因正是上一个 PUBACK 丢失）。
    if p.qos == 1 {
        if let Some(id) = p.packet_id {
            reader.write_all(&encode_puback(id))?;
        }
    }
    Ok(())
}

/// 保留消息更新 + 向所有匹配订阅者投递（含发布者自己，MQTT 3.1.1 无 NoLocal）。
fn route_publish(
    inner: &Arc<Mutex<Inner>>,
    stats: &Arc<BrokerStats>,
    cfg: &BrokerConfig,
    topic_name: &str,
    payload: &[u8],
    qos: u8,
    retain_flag: bool,
) {
    // 1) 更新保留消息存储（在 broker 锁内）并收集投递目标快照。
    let targets: Vec<(Arc<Mutex<Session>>, u8)> = {
        let mut g = inner.lock().unwrap();

        if retain_flag {
            if payload.is_empty() {
                // §3.3.1.3：RETAIN=1 且载荷为空 → 删除该主题的保留消息。
                g.retained.remove(topic_name);
            } else {
                g.retained.insert(
                    topic_name.to_string(),
                    Retained {
                        payload: payload.to_vec(),
                        qos,
                    },
                );
            }
        }

        g.sessions
            .values()
            .filter_map(|s_arc| {
                let s = s_arc.lock().unwrap();
                // 取最高匹配订阅的 QoS。
                let mut best: Option<u8> = None;
                for (filter, sub_qos) in s.subscriptions.iter() {
                    if topic::matches(filter, topic_name) {
                        best = Some(best.map_or(*sub_qos, |b: u8| b.max(*sub_qos)));
                    }
                }
                best.map(|sub_qos| (Arc::clone(s_arc), sub_qos))
            })
            .collect()
    };

    // 2) 逐会话投递（每会话各自一把锁，写 socket 不持 broker 锁）。
    // 实时投递一律 RETAIN=0（§3.3.1.3：保留位只在「订阅时的保留消息投递」置 1）。
    for (target_arc, sub_qos) in targets {
        let effective = qos.min(sub_qos);
        deliver_one(
            stats,
            cfg,
            &target_arc,
            topic_name,
            payload,
            effective,
            false,
        );
    }
}

/// 向单个会话投递一条消息；按其在线状态与 QoS 走实时发送或离线排队。
fn deliver_one(
    stats: &Arc<BrokerStats>,
    cfg: &BrokerConfig,
    target_arc: &Arc<Mutex<Session>>,
    topic_name: &str,
    payload: &[u8],
    effective_qos: u8,
    retained_delivery: bool,
) {
    let mut s = target_arc.lock().unwrap();

    if !s.connected {
        if !s.clean_session && effective_qos == 1 {
            if s.offline.len() >= MAX_OFFLINE_PER_SESSION {
                s.offline.pop_front();
                s.dropped_offline += 1;
            }
            s.offline.push_back(QueuedMessage {
                topic: topic_name.to_string(),
                payload: payload.to_vec(),
                qos: 1,
                retain: false,
            });
        } else {
            stats.publishes_dropped.fetch_add(1, Ordering::Relaxed);
        }
        return;
    }

    if effective_qos == 0 {
        let bytes = encode_publish(topic_name, None, payload, 0, false, retained_delivery);
        match write_session(&mut s, &bytes) {
            Ok(()) => {
                stats.publish_delivered_qos0.fetch_add(1, Ordering::Relaxed);
            }
            Err(_) => {
                stats.publishes_dropped.fetch_add(1, Ordering::Relaxed);
            }
        }
        return;
    }

    // QoS1：分配包ID → 记入在途 → 发送。
    let Some(id) = s.allocate_packet_id() else {
        stats.publishes_dropped.fetch_add(1, Ordering::Relaxed);
        log(
            cfg,
            format!("packet-id pool exhausted for client {}", s.client_id),
        );
        return;
    };
    let bytes = encode_publish(topic_name, Some(id), payload, 1, false, retained_delivery);
    match write_session(&mut s, &bytes) {
        Ok(()) => {
            s.track_inflight(
                id,
                InFlight {
                    topic: topic_name.to_string(),
                    payload: payload.to_vec(),
                    retain: retained_delivery,
                    last_sent: Instant::now(),
                    retries: 0,
                },
            );
            stats.publish_delivered_qos1.fetch_add(1, Ordering::Relaxed);
        }
        Err(_) => {
            stats.publishes_dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// 处理 SUBSCRIBE：更新订阅、回 SUBACK，再投递匹配的保留消息（RETAIN=1）。
fn handle_subscribe(
    inner: &Arc<Mutex<Inner>>,
    stats: &Arc<BrokerStats>,
    cfg: &BrokerConfig,
    s_arc: &Arc<Mutex<Session>>,
    reader: &mut TcpStream,
    packet_id: u16,
    filters: &[(&str, u8)],
) -> io::Result<()> {
    stats.subscribe_received.fetch_add(1, Ordering::Relaxed);

    let mut codes = Vec::with_capacity(filters.len());
    {
        let mut s = s_arc.lock().unwrap();
        s.touch();
        for (filter, requested_qos) in filters {
            // 子集最高授予 QoS1：请求 2 时降级授予 1（§3.8.3.1 / §3.9.3）。
            let granted = (*requested_qos).min(1);
            s.subscriptions.insert((*filter).to_string(), granted);
            codes.push(granted);
        }
    }
    reader.write_all(&encode_suback(packet_id, &codes))?;
    log(
        cfg,
        format!(
            "client {} subscribed (id={packet_id}): {:?}",
            s_arc.lock().unwrap().client_id,
            filters
        ),
    );

    // 收集匹配的保留消息快照（避免持 broker 锁写 socket）。
    let retained_snapshot: Vec<(String, Vec<u8>, u8, u8)> = {
        let g = inner.lock().unwrap();
        let s = s_arc.lock().unwrap();
        let mut out = Vec::new();
        for (filter, _) in filters {
            let granted = *s.subscriptions.get(*filter).unwrap();
            for (topic_name, r) in g.retained.iter() {
                if topic::matches(filter, topic_name) {
                    out.push((topic_name.clone(), r.payload.clone(), r.qos, granted));
                }
            }
        }
        out
    };

    for (topic_name, payload, retained_qos, granted_qos) in retained_snapshot {
        // §3.8.4-3：保留消息按「发布QoS与订阅QoS的较小值」投递，RETAIN=1。
        let effective = retained_qos.min(granted_qos);
        deliver_one(stats, cfg, s_arc, &topic_name, &payload, effective, true);
    }

    Ok(())
}

/// DISCONNECT：干净断开（§3.14）。
/// CleanSession=1 删除会话；持久会话只摘除连接、保留状态（含在途与入站指纹）。
fn graceful_disconnect(inner: &Arc<Mutex<Inner>>, s_arc: &Arc<Mutex<Session>>, generation: u64) {
    let (client_id, clean) = {
        let s = s_arc.lock().unwrap();
        (s.client_id.clone(), s.clean_session)
    };
    let mut g = inner.lock().unwrap();
    let is_current = g
        .sessions
        .get(&client_id)
        .is_some_and(|arc| Arc::ptr_eq(arc, s_arc));
    if clean && is_current {
        g.sessions.remove(&client_id);
    } else {
        drop(g);
        detach_stream(s_arc, generation);
    }
}

/// 异常断连清理：持久会话保留（等待重连），临时会话删除。
fn cleanup_disconnect(inner: &Arc<Mutex<Inner>>, s_arc: Arc<Mutex<Session>>, generation: u64) {
    let (client_id, clean) = {
        let s = s_arc.lock().unwrap();
        (s.client_id.clone(), s.clean_session)
    };
    let mut g = inner.lock().unwrap();
    let is_current = g
        .sessions
        .get(&client_id)
        .is_some_and(|arc| Arc::ptr_eq(arc, &s_arc));
    if clean && is_current {
        g.sessions.remove(&client_id);
    } else {
        drop(g);
        detach_stream(&s_arc, generation);
    }
}

/// 摘除连接资源（socket/在线标记）。入站指纹表保留：持久会话需要它识别重连后重发。
fn detach_stream(s_arc: &Arc<Mutex<Session>>, generation: u64) {
    let mut s = s_arc.lock().unwrap();
    if s.generation == generation {
        s.stream = None;
        s.connected = false;
    }
}
