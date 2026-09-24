use serde::{Deserialize, Serialize};

pub const STATUS_RUNNING: &str = "running";
pub const STATUS_SAFE_MODE: &str = "safe_mode";

pub const DEFAULT_WINDOW_MS: i64 = 1_000;
pub const DEFAULT_RESET_THRESHOLD: i64 = 3;
pub const DEFAULT_CHALLENGE_TTL_MS: i64 = 60_000;
pub const DEFAULT_OPERATOR_KEY: &str = "dev-operator-key-change-me";

/// Row of the `devices` table (internal representation).
#[derive(Debug, Clone)]
pub struct DeviceRow {
    pub device_id: String,
    pub status: String,
    pub fault_generation: i64,
    pub consecutive_resets: i64,
    pub window_ms: i64,
    pub reset_threshold: i64,
    pub challenge_ttl_ms: i64,
    pub window_started_ms: i64,
    pub last_feed_ms: Option<i64>,
    pub last_reset_reason: Option<String>,
    pub last_reset_source: Option<String>,
    pub last_reset_at_ms: Option<i64>,
    pub last_progress_json: Option<String>,
    pub total_feeds: i64,
    pub total_resets: i64,
    pub operator_key: String,
}

#[derive(Debug, Clone)]
pub struct TaskRow {
    pub task_id: String,
    pub last_counter: Option<i64>,
    pub last_report_ms: Option<i64>,
    pub advanced_this_window: bool,
}

// ---------- requests ----------

#[derive(Debug, Deserialize)]
pub struct CreateDeviceReq {
    pub device_id: String,
    pub window_ms: Option<i64>,
    pub reset_threshold: Option<i64>,
    pub challenge_ttl_ms: Option<i64>,
    pub operator_key: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct RegisterTaskReq {
    pub task_id: String,
    pub initial_counter: Option<u32>,
}

#[derive(Debug, Deserialize)]
pub struct TaskProgress {
    pub task_id: String,
    pub counter: u32,
}

#[derive(Debug, Deserialize)]
pub struct HeartbeatReq {
    pub reports: Vec<TaskProgress>,
}

#[derive(Debug, Deserialize)]
pub struct ReportResetReq {
    pub reason: String,
    pub source: Option<String>,
    pub boot_count: Option<i64>,
}

#[derive(Debug, Deserialize)]
pub struct ClearReq {
    pub generation: i64,
    pub challenge: String,
    /// Lowercase hex HMAC-SHA256(operator_key, "{generation}:{challenge}")
    pub hmac_hex: String,
}

#[derive(Debug, Deserialize)]
pub struct ClockAdvanceReq {
    pub ms: i64,
}

#[derive(Debug, Deserialize)]
pub struct ClockSetReq {
    pub now_ms: i64,
}

// ---------- responses ----------

#[derive(Debug, Serialize)]
pub struct TaskView {
    pub task_id: String,
    pub last_counter: Option<u32>,
    pub advanced_this_window: bool,
    pub last_report_ms: Option<i64>,
}

#[derive(Debug, Serialize)]
pub struct HeartbeatResp {
    pub device_id: String,
    pub fed: bool,
    pub safe_mode: bool,
    pub reason: String,
    pub now_ms: i64,
    pub window_started_ms: i64,
    pub window_deadline_ms: i64,
    pub consecutive_resets: i64,
    pub fault_generation: i64,
    pub tasks: Vec<TaskView>,
}

#[derive(Debug, Serialize)]
pub struct StatusResp {
    pub device_id: String,
    pub status: String,
    pub fault_generation: i64,
    pub consecutive_resets: i64,
    pub window_ms: i64,
    pub reset_threshold: i64,
    pub challenge_ttl_ms: i64,
    pub window_started_ms: i64,
    pub window_deadline_ms: i64,
    pub last_feed_ms: Option<i64>,
    pub last_reset_reason: Option<String>,
    pub last_reset_source: Option<String>,
    pub last_reset_at_ms: Option<i64>,
    pub last_progress: Option<serde_json::Value>,
    pub total_feeds: i64,
    pub total_resets: i64,
    pub boot_count: Option<i64>,
    pub challenge_active: bool,
    pub tasks: Vec<TaskView>,
}

#[derive(Debug, Serialize)]
pub struct TickResp {
    pub device_id: String,
    pub now_ms: i64,
    pub reset_applied: bool,
    pub status: String,
    pub consecutive_resets: i64,
    pub fault_generation: i64,
    pub window_started_ms: i64,
    pub window_deadline_ms: i64,
}

#[derive(Debug, Serialize)]
pub struct ChallengeResp {
    pub device_id: String,
    pub generation: i64,
    pub challenge: String,
    pub issued_at_ms: i64,
    pub expires_at_ms: i64,
    pub sign_hint: String,
}

#[derive(Debug, Serialize)]
pub struct ClearResp {
    pub device_id: String,
    pub cleared: bool,
    pub status: String,
    pub generation: i64,
    pub detail: String,
}

#[derive(Debug, Serialize)]
pub struct ResetRecordView {
    pub seq: i64,
    pub at_ms: i64,
    pub source: String,
    pub reason: String,
    pub boot_count: Option<i64>,
    pub counted: bool,
    pub consecutive_after: i64,
    pub fault_generation_after: i64,
}

#[derive(Debug, Serialize)]
pub struct ResetListResp {
    pub device_id: String,
    pub resets: Vec<ResetRecordView>,
}

#[derive(Debug, Serialize)]
pub struct EventView {
    pub id: i64,
    pub at_ms: i64,
    pub event: String,
    pub detail: String,
}

#[derive(Debug, Serialize)]
pub struct EventListResp {
    pub device_id: String,
    pub events: Vec<EventView>,
}

#[derive(Debug, Serialize)]
pub struct ClockResp {
    pub now_ms: i64,
}
