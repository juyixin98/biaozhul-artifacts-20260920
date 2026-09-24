use serde::{Deserialize, Serialize};

/// 一条已被接受的运动命令（autonomous / rc）。
#[derive(Debug, Clone, Serialize)]
pub struct Command {
    pub source: String,
    pub source_kind: String,
    pub seq: i64,
    pub nonce: String,
    pub vx: f64,
    pub vy: f64,
    pub omega: f64,
    pub issue_ms: i64,
    pub deadline_ms: i64,
    pub received_ms: i64,
}

/// 一次急停事件（按下或解除）。
#[derive(Debug, Clone, Serialize)]
pub struct EstopEvent {
    pub source: String,
    pub seq: i64,
    pub action: String, // "trigger" | "clear"
    pub received_ms: i64,
}

/// 仲裁时需要的整个世界视图——便于对纯函数做单元测试。
pub struct WorldView {
    pub commands: Vec<Command>,
    pub estop_events: Vec<EstopEvent>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Suppressed {
    pub source: String,
    pub source_kind: String,
    pub seq: i64,
    pub reason: String,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Chosen {
    pub source: String,
    pub source_kind: String,
    pub seq: i64,
    pub vx: f64,
    pub vy: f64,
    pub omega: f64,
    pub reason: String,
    pub clamped: Option<ClampInfo>,
    pub deadline_ms: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ClampInfo {
    pub requested: [f64; 3],
    pub emitted: [f64; 3],
}

#[derive(Debug, Clone, Serialize)]
pub struct DecisionOutcome {
    pub at_ms: i64,
    pub estop_latched: bool,
    pub chosen: Option<Chosen>,
    pub suppressed: Vec<Suppressed>,
    /// 每个来源的当前状态快照，便于观察“为何没选它”。
    pub source_states: Vec<SourceState>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SourceState {
    pub source: String,
    pub source_kind: String,
    pub latest_seq: Option<i64>,
    pub latest_deadline_ms: Option<i64>,
    pub lease_alive: bool,
}

// ---------- HTTP 请求/响应 ----------

#[derive(Debug, Deserialize)]
pub struct CommandReq {
    pub seq: i64,
    pub nonce: String,
    pub vx: f64,
    pub vy: f64,
    pub omega: f64,
    /// 命令签发时刻（毫秒，时钟需与服务器对齐，可先查 /v1/time）。
    pub issue_ms: i64,
    /// 有效期窗口（毫秒）：收到时刻超过 issue_ms + valid_for_ms 即拒绝。
    pub valid_for_ms: i64,
    /// 租约时长（毫秒）：issue_ms + lease_for_ms 之前命令有效。
    pub lease_for_ms: i64,
}

#[derive(Debug, Deserialize)]
pub struct EstopReq {
    pub seq: i64,
    pub nonce: String,
    pub action: String, // "trigger" | "clear"
    pub issue_ms: i64,
    pub valid_for_ms: i64,
}

#[derive(Debug, Deserialize, Default)]
pub struct EvaluateReq {
    /// 不传则使用当前时钟时刻。
    pub at_ms: Option<i64>,
    /// 是否落库为一条决策记录（/v1/evaluate 默认 true；/v1/decision/latest 恒不落库）。
    #[serde(default)]
    pub persist: Option<bool>,
}

#[derive(Debug, Serialize)]
pub struct IngestResponse {
    pub accepted: bool,
    pub received_ms: i64,
    pub decision: DecisionOutcome,
}

#[derive(Debug, Deserialize)]
pub struct ClockAdvanceReq {
    pub delta_ms: i64,
}

#[derive(Debug, Serialize)]
pub struct ClockResponse {
    pub mode: String,
    pub now_ms: i64,
}
