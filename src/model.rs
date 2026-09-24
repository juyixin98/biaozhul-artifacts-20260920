//! API 与领域模型。
//!
//! 时间一律为 Unix 毫秒（i64）。命令的有效期由两个独立时刻决定：
//!
//! - `ttl_expires_at   = issued_at + ttl_ms`   消息自身的保鲜期
//! - `lease_expires_at = issued_at + lease_ms` 租约（授权）有效期
//!
//! 命令仅当当前时刻严格小于二者且未被显式释放时才是 active 的。

use serde::{Deserialize, Serialize};

/// 命令来源。Remote 的优先级高于 Autonomous；急停不在这里，是独立的锁存通道。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    Autonomous,
    Remote,
}

impl Source {
    pub fn as_str(self) -> &'static str {
        match self {
            Source::Autonomous => "autonomous",
            Source::Remote => "remote",
        }
    }

    pub fn parse(s: &str) -> Option<Source> {
        match s {
            "autonomous" => Some(Source::Autonomous),
            "remote" => Some(Source::Remote),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ClockMode {
    Sim,
    Wall,
}

impl ClockMode {
    pub fn as_str(self) -> &'static str {
        match self {
            ClockMode::Sim => "sim",
            ClockMode::Wall => "wall",
        }
    }
}

/// 来自某个来源的速度命令。
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CommandRequest {
    /// 来源内单调递增序号；旧序号（含重复）一律拒绝。
    pub seq: i64,
    /// 租约标识；显式释放时必须匹配。
    pub lease_id: String,
    /// 前向速度 m/s。
    pub vx: f64,
    /// 转向角速度 rad/s。
    pub wz: f64,
    /// 来源生成该命令的时刻（Unix 毫秒）。
    pub issued_at: i64,
    /// 租约时长（毫秒）。
    pub lease_ms: i64,
    /// 消息保鲜期（毫秒）。
    pub ttl_ms: i64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EstopEvent {
        Latch,
        Release,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EstopRequest {
    /// 急停通道自己的单调序号。
    pub seq: i64,
    #[serde(rename = "event")]
    pub event: EstopEvent,
    pub issued_at: i64,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ReleaseRequest {
    /// 来源流上的新序号（必须大于上一条已接受消息）。
    pub seq: i64,
    pub lease_id: String,
    pub issued_at: i64,
}

/// 已被服务器接受并存储的命令。
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StoredCommand {
    pub source: Source,
    pub seq: i64,
    pub lease_id: String,
    pub vx: f64,
    pub wz: f64,
    pub issued_at: i64,
    pub lease_ms: i64,
    pub ttl_ms: i64,
    pub lease_expires_at: i64,
    pub ttl_expires_at: i64,
    pub accepted_at: i64,
    /// 显式释放租约后置位；命令不再 active，但记录仍保留用于审计。
    pub revoked: bool,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct SourceState {
    pub last_seq: i64,
    pub command: Option<StoredCommand>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct EstopState {
    pub latched: bool,
    /// 最近一次急停事件序号（初始 0）。
    pub seq: i64,
    pub issued_at: Option<i64>,
    pub latched_at: Option<i64>,
    pub reason: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Velocity {
    pub vx: f64,
    pub wz: f64,
}

#[derive(Clone, Debug, Serialize)]
pub struct SelectedCommand {
    pub source: String,
    pub seq: i64,
    pub lease_id: String,
    pub vx: f64,
    pub wz: f64,
}

#[derive(Clone, Debug, Serialize)]
pub struct SuppressedCommand {
    pub source: String,
    pub seq: i64,
    pub lease_id: String,
    pub reason: String,
}

/// 一次仲裁的完整决策结果（决策记录的核心内容）。
#[derive(Clone, Debug, Serialize)]
pub struct Decision {
    pub at: i64,
    pub estop_latched: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub selected: Option<SelectedCommand>,
    pub output: Velocity,
    /// selected 为 null 时给出停止原因。
    pub stop_reason: Option<String>,
    pub suppressed: Vec<SuppressedCommand>,
}

/// 结构化 API 错误。
#[derive(Debug, Serialize)]
pub struct ApiError {
    pub error: String,
    pub reason: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl ApiError {
    pub fn new(error: &str, reason: &str) -> Self {
        ApiError {
            error: error.to_string(),
            reason: reason.to_string(),
            detail: None,
        }
    }

    pub fn with_detail(error: &str, reason: &str, detail: String) -> Self {
        ApiError {
            error: error.to_string(),
            reason: reason.to_string(),
            detail: Some(detail),
        }
    }
}

// ---- 原因码（拒绝 / 抑制），集中定义避免拼写漂移 ----

pub mod reason {
    // 硬性拒绝（不进入仲裁）
    pub const VALIDATION: &str = "validation_error";
    pub const STALE_SEQ: &str = "stale_seq";
    pub const TTL_EXPIRED: &str = "ttl_expired";
    pub const LEASE_EXPIRED: &str = "lease_expired";
    pub const FUTURE_SKEW: &str = "future_clock_skew";
    pub const CLOCK_REGRESSION: &str = "clock_regression";
    pub const OUT_OF_RANGE: &str = "velocity_out_of_range";
    pub const NOT_LATCHED: &str = "estop_not_latched";
    pub const LEASE_MISMATCH: &str = "lease_mismatch";

    // 抑制（命令已接受但未被选中）
    pub const ESTOP_LATCHED: &str = "estop_latched";
    pub const PRIORITY_OVERRIDE: &str = "priority_override";
    /// 自主命令早于（或恰好等于）更高优先级占用结束时刻 —— 失联后不许“复活”。
    pub const STALE_AFTER_OVERRIDE: &str = "stale_after_override";
    /// 遥控命令早于（或恰好等于）急停解除时刻。
    pub const STALE_AFTER_ESTOP: &str = "stale_after_estop_release";
    pub const LEASE_RELEASED: &str = "lease_released";
    pub const NO_ELIGIBLE: &str = "no_eligible_command";
}
