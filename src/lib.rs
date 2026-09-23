//! 中继租约与序号协调 —— 三个角色都在本 crate 里：
//! - [`coordinator`]：PostgreSQL 支撑的协调服务（Axum）
//! - [`target`]：本地模拟目标链桩（内存状态，幂等提交、结果查询、故障注入）
//! - [`worker`]：中继工作循环（领取租约 → 提交 → 超时先查询 → 回执带 fencing token）

pub mod client;
pub mod coordinator;
pub mod db;
pub mod target;
pub mod worker;

/// 租约内消息状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageStatus {
    Pending,
    Leased,
    Confirmed,
    Failed,
}

impl MessageStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            MessageStatus::Pending => "pending",
            MessageStatus::Leased => "leased",
            MessageStatus::Confirmed => "confirmed",
            MessageStatus::Failed => "failed",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "pending" => MessageStatus::Pending,
            "leased" => MessageStatus::Leased,
            "confirmed" => MessageStatus::Confirmed,
            "failed" => MessageStatus::Failed,
            _ => return None,
        })
    }
}

/// 指数退避（确定性，测试可断言）：第 attempt 次失败后等待 base * 2^attempt，封顶 max。
pub fn backoff_delay(base_ms: u64, attempt: u32, max_ms: u64) -> std::time::Duration {
    let factor = 1u64.checked_shl(attempt.min(20)).unwrap_or(1 << 20);
    let ms = base_ms.saturating_mul(factor).min(max_ms);
    std::time::Duration::from_millis(ms)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_is_exponential_then_capped() {
        assert_eq!(backoff_delay(100, 0, 5_000).as_millis(), 100);
        assert_eq!(backoff_delay(100, 1, 5_000).as_millis(), 200);
        assert_eq!(backoff_delay(100, 2, 5_000).as_millis(), 400);
        assert_eq!(backoff_delay(100, 3, 5_000).as_millis(), 800);
        // 达到上限后封顶，不溢出。
        assert_eq!(backoff_delay(100, 100, 5_000).as_millis(), 5_000);
    }

    #[test]
    fn status_roundtrip() {
        for s in [
            MessageStatus::Pending,
            MessageStatus::Leased,
            MessageStatus::Confirmed,
            MessageStatus::Failed,
        ] {
            assert_eq!(MessageStatus::parse(s.as_str()), Some(s));
        }
        assert_eq!(MessageStatus::parse("nope"), None);
    }
}
