//! 有限历史缓冲：支持 Last-Event-ID 续传。
//!
//! 服务端保留最近 `capacity` 个已编码事件。客户端用上次收到的 id 续传时：
//! - 游标仍在历史范围内 → 返回遗漏的事件（严格 `id > last_id`）；
//! - 游标太旧（已被挤出历史）→ 返回 [`Replay::Expired`]，服务端应发送
//!   明确的 `reset` 事件，客户端收到后清空本地去重状态重新同步；
//! - 游标已是最新 → 返回空列表。

use std::collections::VecDeque;
use std::sync::Arc;

/// 已编码待重放的事件。
#[derive(Debug, Clone)]
pub struct StoredEvent {
    pub id: u64,
    /// 编码后的完整 SSE 字节块（含结尾空行）。
    pub raw: Arc<Vec<u8>>,
}

/// 续传判定结果。
#[derive(Debug)]
pub enum Replay {
    /// 重放这些事件（可能为空：游标已最新）。
    Events(Vec<StoredEvent>),
    /// 游标已过期（last_id 之前的事件已被挤出历史），需要全量重置。
    Expired,
}

/// 定长环形历史：满了之后挤出最旧事件。
#[derive(Debug)]
pub struct History {
    capacity: usize,
    events: VecDeque<StoredEvent>,
    /// 下一个将要分配的 id（历史为空时用于判断游标是否过期）。
    next_id: u64,
}

impl History {
    /// `capacity` 为保留的最大事件数；`first_id` 为首个事件 id（通常为 1）。
    pub fn new(capacity: usize, first_id: u64) -> Self {
        History {
            capacity: capacity.max(1),
            events: VecDeque::new(),
            next_id: first_id,
        }
    }

    /// 追加一个事件；超出容量时挤出最旧的。
    pub fn push(&mut self, ev: StoredEvent) {
        self.next_id = ev.id + 1;
        if self.events.len() == self.capacity {
            self.events.pop_front();
        }
        self.events.push_back(ev);
    }

    /// 当前保留中最旧可见的 id（历史为空时为下一个将分配的 id）。
    pub fn oldest_available_id(&self) -> u64 {
        self.events.front().map(|e| e.id).unwrap_or(self.next_id)
    }

    /// 最新已分配的 id；还没有事件时为 `None`。
    pub fn newest_id(&self) -> Option<u64> {
        if self.next_id == 0 {
            None
        } else {
            Some(self.next_id - 1)
        }
    }

    pub fn len(&self) -> usize {
        self.events.len()
    }

    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    /// 以 `last_id` 续传：
    /// - `None`（新客户端）：不重放，只接实时流；
    /// - `Some(id)` 且 `id + 1 < oldest_available_id()`：游标过期 → [`Replay::Expired`]；
    /// - 否则返回所有 `id > last_id` 的保留事件（严格大于，服务端不主动制造重复）。
    pub fn replay_after(&self, last_id: Option<u64>) -> Replay {
        let last = match last_id {
            None => return Replay::Events(Vec::new()),
            Some(v) => v,
        };
        if last.saturating_add(1) < self.oldest_available_id() {
            return Replay::Expired;
        }
        Replay::Events(
            self.events
                .iter()
                .filter(|e| e.id > last)
                .cloned()
                .collect(),
        )
    }

    /// 取 `id > after` 的全部保留事件（实时跟随用）。
    pub fn events_after(&self, after: u64) -> Vec<StoredEvent> {
        self.events.iter().filter(|e| e.id > after).cloned().collect()
    }
}
