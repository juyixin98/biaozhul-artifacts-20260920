//! Broker 核心：会话表、包 ID 分配、QoS1 重发状态、保留消息、订阅匹配。
//!
//! 语义说明（重要）：
//! - 本实现承诺 **至少一次** 投递。QoS1 出向消息在收到 PUBACK 前一直
//!   保存在 `inflight_out`，会话恢复（clean_session=false 重连）时以
//!   DUP=1 重发；因此订阅方可能收到重复消息，需要去重由业务层负责。
//! - 入向 QoS1 用 `seen_in` 按包 ID 去重：同一客户端会话内重复的
//!   PUBLISH（典型场景：PUBACK 丢失后客户端重发）只投递一次，但会
//!   重新回 PUBACK。这是对至少一次语义的接收端优化，不等于恰好一次。
//! - 重发时机：仅在会话恢复时重发，不做定时器重传（已记录为限制）。

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::mpsc::Sender;
use std::sync::Mutex;

use crate::packet::{Packet, Publish};
use crate::topic;

/// 连接建立的结果。
pub struct ConnectOutcome {
    pub session_present: bool,
    /// 需要在 CONNACK 之后立即发给客户端的报文：
    /// 未确认的 QoS1 重发（DUP=1）+ 离线期间排队的新消息。
    pub resume: Vec<Packet>,
}

struct Session {
    clean: bool,
    /// 当前在线连接的写端；离线为 None。
    tx: Option<Sender<Packet>>,
    subscriptions: Vec<(String, u8)>,
    /// 服务器→客户端方向：等待 PUBACK 的 QoS1 消息（按包 ID）。
    inflight_out: HashMap<u16, Publish>,
    /// 离线期间排队的新 QoS1 消息（尚未首发，DUP=0）。
    outbox: VecDeque<Publish>,
    /// 出向包 ID 分配器（1..=65535 循环，跳过在飞的）。
    next_id: u16,
    /// 客户端→服务器方向：本会话已接收的 QoS1 包 ID（去重窗口）。
    seen_in: HashSet<u16>,
    will: Option<Publish>,
}

impl Session {
    fn new(clean: bool) -> Self {
        Session {
            clean,
            tx: None,
            subscriptions: Vec::new(),
            inflight_out: HashMap::new(),
            outbox: VecDeque::new(),
            next_id: 1,
            seen_in: HashSet::new(),
            will: None,
        }
    }

    fn alloc_packet_id(&mut self) -> u16 {
        for _ in 0..u16::MAX {
            let id = self.next_id;
            self.next_id = if self.next_id == u16::MAX {
                1
            } else {
                self.next_id + 1
            };
            if !self.inflight_out.contains_key(&id) {
                return id;
            }
        }
        // 65535 个 ID 全部在飞：对测试 broker 而言视为不可达。
        panic!("packet id space exhausted");
    }
}

struct Inner {
    sessions: HashMap<String, Session>,
    /// topic -> (qos, payload)
    retained: HashMap<String, (u8, Vec<u8>)>,
    auto_id: u64,
}

pub struct Broker {
    inner: Mutex<Inner>,
}

impl Default for Broker {
    fn default() -> Self {
        Self::new()
    }
}

impl Broker {
    pub fn new() -> Self {
        Broker {
            inner: Mutex::new(Inner {
                sessions: HashMap::new(),
                retained: HashMap::new(),
                auto_id: 0,
            }),
        }
    }

    /// 为空客户端 ID 分配服务端 ID（仅 clean_session=true 允许）。
    pub fn assign_client_id(&self) -> String {
        let mut g = self.inner.lock().unwrap();
        g.auto_id += 1;
        format!("auto-{:016x}", g.auto_id)
    }

    /// 客户端上线。返回会话是否已存在及需要补发的报文。
    pub fn connect(
        &self,
        client_id: &str,
        clean: bool,
        will: Option<Publish>,
        tx: Sender<Packet>,
    ) -> ConnectOutcome {
        let mut g = self.inner.lock().unwrap();
        let existed = g.sessions.contains_key(client_id);
        if clean || !existed {
            g.sessions.insert(client_id.to_string(), Session::new(clean));
        }
        let session = g.sessions.get_mut(client_id).unwrap();
        session.clean = clean;
        session.tx = Some(tx);
        session.will = will;
        let session_present = existed && !clean;

        let mut resume = Vec::new();
        if session_present {
            // 未确认的 QoS1：置 DUP=1 重发。
            let mut ids: Vec<u16> = session.inflight_out.keys().copied().collect();
            ids.sort_unstable();
            for id in ids {
                let p = session.inflight_out.get_mut(&id).unwrap();
                p.dup = true;
                resume.push(Packet::Publish(p.clone()));
            }
            // 离线排队的新消息：首发，转入 inflight。
            while let Some(mut p) = session.outbox.pop_front() {
                if p.qos == 1 {
                    let id = session.alloc_packet_id();
                    p.packet_id = Some(id);
                    session.inflight_out.insert(id, p.clone());
                }
                resume.push(Packet::Publish(p));
            }
        }
        ConnectOutcome {
            session_present,
            resume,
        }
    }

    /// 连接断开。graceful=false 时投递遗嘱（如有）。
    pub fn disconnect(&self, client_id: &str, graceful: bool) {
        let will = {
            let mut g = self.inner.lock().unwrap();
            let session = match g.sessions.get_mut(client_id) {
                Some(s) => s,
                None => return,
            };
            session.tx = None;
            let will = if graceful { None } else { session.will.take() };
            if graceful {
                session.will = None;
            }
            // clean 会话在断开后整体清除。
            if session.clean {
                g.sessions.remove(client_id);
            }
            will
        };
        if let Some(w) = will {
            self.route(None, w);
        }
    }

    /// 处理 SUBSCRIBE：登记订阅，返回 (授予的 QoS 列表, 匹配的保留消息)。
    pub fn subscribe(
        &self,
        client_id: &str,
        topics: &[(String, u8)],
    ) -> (Vec<u8>, Vec<Packet>) {
        let mut g = self.inner.lock().unwrap();
        let mut granted = Vec::with_capacity(topics.len());
        if let Some(session) = g.sessions.get_mut(client_id) {
            for (filter, qos) in topics {
                // 子集上限：授予 QoS 最高为 1。
                let grant = (*qos).min(1);
                granted.push(grant);
                match session
                    .subscriptions
                    .iter_mut()
                    .find(|(f, _)| f == filter)
                {
                    Some(entry) => entry.1 = grant,
                    None => session.subscriptions.push((filter.clone(), grant)),
                }
            }
        } else {
            granted.resize(topics.len(), 0x80);
        }
        // 保留消息：订阅建立后立即下发，retain 标志保持 1，
        // QoS 取 min(存储值, 本次授予值)；QoS1 纳入在飞跟踪。
        let mut hits: Vec<(String, u8, Vec<u8>)> = Vec::new();
        for ((filter, _), grant) in topics.iter().zip(granted.iter()) {
            for (topic, (qos, payload)) in g.retained.iter() {
                if topic::matches(filter, topic) {
                    hits.push((topic.clone(), (*qos).min(*grant), payload.clone()));
                }
            }
        }
        let mut retained = Vec::new();
        for (topic, qos, payload) in hits {
            let mut msg = Publish {
                dup: false,
                qos,
                retain: true,
                topic,
                packet_id: None,
                payload,
            };
            if msg.qos == 1 {
                if let Some(session) = g.sessions.get_mut(client_id) {
                    let id = session.alloc_packet_id();
                    msg.packet_id = Some(id);
                    session.inflight_out.insert(id, msg.clone());
                }
            }
            retained.push(Packet::Publish(msg));
        }
        (granted, retained)
    }

    /// 处理客户端 PUBLISH：存储保留消息、去重、路由给订阅者。
    /// 返回是否为重复包（重复包不再投递，但调用方仍需回 PUBACK）。
    ///
    /// 去重规则（MQTT 3.1.1 §4.3.2）：只有 DUP=1 的重发才可能命中
    /// 去重窗口；DUP=0 一律视为新消息——包 ID 在 PUBACK 完成后允许
    /// 立即复用，不能仅凭 ID 判重。
    pub fn publish_from_client(&self, client_id: &str, p: Publish) -> bool {
        let mut g = self.inner.lock().unwrap();
        if p.qos == 1 {
            let id = p.packet_id.expect("qos1 publish has packet id");
            let session = g.sessions.get_mut(client_id);
            if let Some(s) = session {
                if p.dup && s.seen_in.contains(&id) {
                    return true; // DUP 重发且已见过：不再投递
                }
                s.seen_in.insert(id);
            }
        }
        drop(g);
        self.route(Some(client_id), p);
        false
    }

    /// 处理 PUBACK：完成一次出向 QoS1 握手，包 ID 随之可复用。
    pub fn puback(&self, client_id: &str, packet_id: u16) {
        let mut g = self.inner.lock().unwrap();
        if let Some(session) = g.sessions.get_mut(client_id) {
            session.inflight_out.remove(&packet_id);
        }
    }

    /// 路由一条消息给所有匹配的会话（内部方法，需不持有锁调用）。
    fn route(&self, from: Option<&str>, p: Publish) {
        let mut g = self.inner.lock().unwrap();

        // 保留消息存储：空载荷清除，非空覆盖。
        if p.retain {
            if p.payload.is_empty() {
                g.retained.remove(&p.topic);
            } else {
                g.retained.insert(p.topic.clone(), (p.qos, p.payload.clone()));
            }
        }

        for (cid, session) in g.sessions.iter_mut() {
            if Some(cid.as_str()) == from {
                continue; // 不回投给发布者本人
            }
            let grant = session
                .subscriptions
                .iter()
                .filter(|(f, _)| topic::matches(f, &p.topic))
                .map(|(_, q)| *q)
                .max();
            let Some(grant) = grant else { continue };
            let mut out = p.clone();
            out.retain = false; // 普通投递 retain=0（保留消息仅在订阅时下发）
            out.qos = out.qos.min(grant);
            let tx = session.tx.clone();
            match tx {
                Some(tx) => {
                    if out.qos == 1 {
                        let id = session.alloc_packet_id();
                        out.packet_id = Some(id);
                        session.inflight_out.insert(id, out.clone());
                    } else {
                        out.packet_id = None;
                    }
                    // 接收方连接可能刚断开；发送失败则按离线处理。
                    if tx.send(Packet::Publish(out.clone())).is_err() && out.qos == 1 {
                        session.outbox.push_back(out);
                    }
                }
                None => {
                    // 持久会话离线：QoS1 排队，QoS0 丢弃（符合 MQTT）。
                    if !session.clean && out.qos == 1 {
                        session.outbox.push_back(out);
                    }
                }
            }
        }
    }

    // ---- 测试/诊断用只读接口 ----

    pub fn inflight_count(&self, client_id: &str) -> usize {
        self.inner
            .lock()
            .unwrap()
            .sessions
            .get(client_id)
            .map(|s| s.inflight_out.len())
            .unwrap_or(0)
    }

    pub fn retained_topics(&self) -> Vec<String> {
        let mut v: Vec<String> = self.inner.lock().unwrap().retained.keys().cloned().collect();
        v.sort();
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packet_id_wraps_and_skips_inflight() {
        let mut s = Session::new(false);
        s.next_id = u16::MAX - 1; // 65534
        s.inflight_out
            .insert(u16::MAX, Publish::new("t", 1, Vec::new()));
        assert_eq!(s.alloc_packet_id(), 65534);
        // 65535 在飞被跳过，回绕到 1
        assert_eq!(s.alloc_packet_id(), 1);
        assert_eq!(s.alloc_packet_id(), 2);
    }

    #[test]
    fn clean_session_state_is_dropped() {
        let broker = Broker::new();
        let (tx, _rx) = std::sync::mpsc::channel();
        broker.connect("c", true, None, tx.clone());
        broker.subscribe("c", &[("a/+".into(), 1)]);
        broker.disconnect("c", true);
        // clean 会话断开后重新连接：session_present 必须为 false
        let outcome = broker.connect("c", true, None, tx);
        assert!(!outcome.session_present);
    }

    #[test]
    fn persistent_session_keeps_subscription_and_offline_queue() {
        let broker = Broker::new();
        let (tx_a, rx_a) = std::sync::mpsc::channel();
        broker.connect("a", false, None, tx_a);
        broker.subscribe("a", &[("t/x".into(), 1)]);
        broker.disconnect("a", true); // 离线，会话保留

        let (tx_b, _rx_b) = std::sync::mpsc::channel();
        broker.connect("b", true, None, tx_b);
        let mut p = Publish::new("t/x", 1, b"m1".to_vec());
        p.packet_id = Some(1);
        broker.publish_from_client("b", p);

        // 离线期间消息应排队，重连后补发
        assert!(rx_a.try_recv().is_err());
        let (tx_a2, rx_a2) = std::sync::mpsc::channel();
        let outcome = broker.connect("a", false, None, tx_a2);
        assert!(outcome.session_present);
        assert_eq!(outcome.resume.len(), 1);
        match &outcome.resume[0] {
            Packet::Publish(p) => {
                assert_eq!(p.payload, b"m1");
                assert!(!p.dup); // 首发不是重发
                assert!(p.packet_id.is_some());
            }
            other => panic!("expected publish, got {other:?}"),
        }
        drop(rx_a2);
    }
}
