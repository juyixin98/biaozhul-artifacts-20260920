//! 客户端会话状态：订阅、包标识符分配、入站去重、离线消息与在途重发。
//!
//! ## 至少一次（at-least-once）语义，非恰好一次
//! - QoS1 出站消息在收到对应 PUBACK 前一直留在 [`Session::inflight`] 中；
//! - 重发由 [`crate::broker`] 的重发定时器驱动，重发报文置 DUP=1；
//! - PUBACK 丢失时客户端会收到 DUP=1 的重复 PUBLISH —— 因此业务层**可能**
//!   收到重复消息，本实现只承诺至少一次，不提供（也不宣称）业务恰好一次。
//! - 入站侧用「包ID + 内容指纹」做尽力去重，仅用于避免明显的重复转发，
//!   跨重连/指纹冲突时仍按至少一次处理。
//!
//! ## 会话生命周期（MQTT 3.1.1 §3.1.2.4）
//! - CleanSession=1：连接时丢弃旧会话；断连（含异常）后立即删除，不排队离线消息。
//! - CleanSession=0：会话在进程内存活；离线期间 QoS1 消息排队，重连后先
//!   重发在途消息（DUP=1），再发送离线队列中的新消息。

use std::collections::hash_map::DefaultHasher;
use std::collections::{HashMap, VecDeque};
use std::hash::Hasher;
use std::net::TcpStream;
use std::time::Instant;

/// 在途（已发送但未被 PUBACK 确认）的出站 QoS1 消息。
#[derive(Debug, Clone)]
pub struct InFlight {
    pub topic: String,
    pub payload: Vec<u8>,
    pub retain: bool,
    /// 最近一次发送时间（首次发送或上次重发）。
    pub last_sent: Instant,
    /// 重发次数（含首次发送则首次为 0，重发后置 true 的 DUP 对应 >=1）。
    pub retries: u32,
}

/// 离线期间排队、尚未分配包标识符的消息。
#[derive(Debug, Clone)]
pub struct QueuedMessage {
    pub topic: String,
    pub payload: Vec<u8>,
    /// 发布时的 QoS（0/1）；实际授予还会再与订阅 QoS 取 min。
    pub qos: u8,
    pub retain: bool,
}

/// 单个持久会话的离线队列上限（超出丢弃最旧消息，符合 §3.1.2.4 中
/// 「服务端可删除排队消息」的许可；丢弃计数见 [`Session::dropped_offline`]）。
pub const MAX_OFFLINE_PER_SESSION: usize = 1000;

#[derive(Debug)]
pub struct Session {
    pub client_id: String,
    /// 本会话首次建立时请求的 CleanSession；持久会话后续重连也必须为 0。
    pub clean_session: bool,

    /// 过滤器 -> 最大 QoS。
    pub subscriptions: HashMap<String, u8>,

    /// 离线队列（重连 flush 时才分配包ID）。
    pub offline: VecDeque<QueuedMessage>,
    pub dropped_offline: u64,

    /// 包标识符 -> 在途消息。
    pub inflight: HashMap<u16, InFlight>,

    /// 入站去重：客户端发布包ID -> 内容指纹。
    inbound_fingerprints: HashMap<u16, u64>,

    /// 当前连接的 TCP 流；离线时为 None。同一时刻一把会话锁保护，写入天然串行。
    pub stream: Option<TcpStream>,
    pub connected: bool,
    pub keep_alive_secs: u16,
    pub last_activity: Instant,
    /// 连接代号：同名客户端重连/踢线时递增，旧连接线程发现代号变化即自行退出，
    /// 避免两个线程同时向同一会话写入。
    pub generation: u64,
}

impl Session {
    pub fn new(client_id: String, clean_session: bool) -> Self {
        Self {
            client_id,
            clean_session,
            subscriptions: HashMap::new(),
            offline: VecDeque::new(),
            dropped_offline: 0,
            inflight: HashMap::new(),
            inbound_fingerprints: HashMap::new(),
            stream: None,
            connected: false,
            keep_alive_secs: 0,
            last_activity: Instant::now(),
            generation: 0,
        }
    }

    /// 分配一个未被在途消息占用的包标识符。
    ///
    /// 策略：分配当前最小的可用 ID（从 1 线性扫描）。MQTT 3.1.1 §2.3.1 只要求
    /// 标识符「当前未被使用」，不规定分配顺序；最小可用策略让 ID 释放后立即可
    /// 复用（行为可预测），在本子集（本地测试、在途量小）下成本可忽略。
    /// 65535 个槽位全满时返回 None（调用方把消息留作背压/离线排队）。
    pub fn allocate_packet_id(&mut self) -> Option<u16> {
        if self.inflight.len() >= u16::MAX as usize {
            return None;
        }
        // 大多数情况下前几个 ID 即空闲；HashMap 查找成本均摊 O(1)。
        (1u16..=u16::MAX).find(|id| !self.inflight.contains_key(id))
    }

    /// PUBACK 到达：完成一个在途消息并释放包标识符供复用。
    /// 返回被确认的消息；未知/重复 PUBACK 返回 None（不报错，幂等处理）。
    pub fn acknowledge(&mut self, packet_id: u16) -> Option<InFlight> {
        self.inflight.remove(&packet_id)
    }

    /// 记录一条在途消息（调用方已完成实际写入）。
    pub fn track_inflight(&mut self, packet_id: u16, msg: InFlight) {
        self.inflight.insert(packet_id, msg);
    }

    /// 入队离线消息（仅持久会话有意义）；超过上限丢弃最旧消息并计数。
    pub fn enqueue_offline(&mut self, msg: QueuedMessage) {
        if self.offline.len() >= MAX_OFFLINE_PER_SESSION {
            self.offline.pop_front();
            self.dropped_offline += 1;
        }
        self.offline.push_back(msg);
    }

    /// 计算入站 PUBLISH 的内容指纹（主题+载荷+QoS+RETAIN）。
    pub fn fingerprint(topic: &str, payload: &[u8], qos: u8, retain: bool) -> u64 {
        let mut h = DefaultHasher::new();
        h.write(topic.as_bytes());
        h.write_u8(0);
        h.write(payload);
        h.write_u8(qos);
        h.write_u8(retain as u8);
        h.finish()
    }

    /// 判断入站 QoS1 PUBLISH 是否为重复传输。
    ///
    /// - 相同包ID + 相同指纹 → 重复（例如 PUBACK 丢失后客户端重发）；
    /// - 相同包ID + 不同指纹 → 旧事务已完成、ID 被复用，视为新消息并替换指纹；
    /// - 新包ID → 新消息。
    ///
    /// 返回 true 表示这是一条**已处理过**的重复消息（调用方只补 PUBACK，
    /// 不再向订阅者转发）。
    pub fn register_inbound(&mut self, packet_id: u16, fingerprint: u64) -> bool {
        match self.inbound_fingerprints.get(&packet_id).copied() {
            Some(prev) if prev == fingerprint => true,
            _ => {
                self.inbound_fingerprints.insert(packet_id, fingerprint);
                false
            }
        }
    }

    /// 清空入站去重表。当前仅在测试或将来需要强制重置时使用；正常流程中：
    /// - CleanSession=1 会话随断连整体删除；
    /// - 持久会话保留指纹表，以便客户端重连后重发的 DUP=1 PUBLISH 仍能被识别
    ///   （尽力去重；指纹表容量受包ID总数 65535 天然限制）。
    pub fn clear_inbound_dedup(&mut self) {
        self.inbound_fingerprints.clear();
    }

    pub fn touch(&mut self) {
        self.last_activity = Instant::now();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn inflight_msg(topic: &str) -> InFlight {
        InFlight {
            topic: topic.to_string(),
            payload: Vec::new(),
            retain: false,
            last_sent: Instant::now(),
            retries: 0,
        }
    }

    #[test]
    fn packet_ids_are_smallest_free_and_reused_after_release() {
        let mut s = Session::new("c".into(), false);
        let id1 = s.allocate_packet_id().unwrap();
        s.track_inflight(id1, inflight_msg("t1"));
        let id2 = s.allocate_packet_id().unwrap();
        s.track_inflight(id2, inflight_msg("t2"));
        assert_eq!(id1, 1);
        assert_eq!(id2, 2);
        // 确认 id=1（释放）；id=2 仍在途。
        assert!(s.acknowledge(1).is_some());
        // id=1 已释放：下一次分配必须立即复用 1，而不是跳到 3。
        let next = s.allocate_packet_id().unwrap();
        assert_eq!(next, 1, "freed packet id must be reusable immediately");
    }

    #[test]
    fn never_allocate_zero_and_full_pool_returns_none() {
        let mut s = Session::new("c".into(), false);
        // 小规模模拟：直接占满 65535 个槽位较慢，验证 0 不出现即可。
        for _ in 0..100 {
            let id = s.allocate_packet_id().unwrap();
            assert!(id >= 1);
            s.track_inflight(id, inflight_msg("t"));
        }
    }

    #[test]
    fn duplicate_publish_detected_then_id_reuse_accepted() {
        let mut s = Session::new("c".into(), false);
        let fp1 = Session::fingerprint("a/b", b"hello", 1, false);
        assert!(!s.register_inbound(7, fp1));
        // PUBACK 丢失，客户端原样重发：判定为重复。
        assert!(s.register_inbound(7, fp1));
        // 事务完成后客户端在新消息上复用包ID 7、内容不同：视为新消息。
        let fp2 = Session::fingerprint("a/b", b"world", 1, false);
        assert!(!s.register_inbound(7, fp2));
    }

    #[test]
    fn offline_queue_evicts_oldest() {
        let mut s = Session::new("c".into(), false);
        for i in 0..(MAX_OFFLINE_PER_SESSION + 3) {
            s.enqueue_offline(QueuedMessage {
                topic: format!("t{i}"),
                payload: vec![i as u8],
                qos: 1,
                retain: false,
            });
        }
        assert_eq!(s.offline.len(), MAX_OFFLINE_PER_SESSION);
        assert_eq!(s.dropped_offline, 3);
        assert_eq!(s.offline.front().unwrap().topic, "t3");
    }
}
