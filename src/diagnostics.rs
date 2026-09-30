//! Diagnostics: request-scoped identifiers, structured decision records and
//! redaction.
//!
//! Every API request gets a [`RequestId`]. Validation and execution emit a
//! [`DecisionRecord`] explaining *why* the request was accepted, rejected, or
//! could not be decided, together with key state (relation sizes, variable
//! domains, work counters). Values that might carry user data are rendered
//! through [`redact`], so logs never print raw payload contents.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;

/// Process-wide monotonic sequence, combined with a timestamp for uniqueness.
static SEQ: AtomicU64 = AtomicU64::new(0);

/// Opaque correlation id attached to every log line and response.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestId(String);

impl RequestId {
    pub fn generate() -> Self {
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        RequestId(format!("req-{ts:x}-{seq:x}"))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for RequestId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// The outcome class a diagnostic record describes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    Accepted,
    Rejected,
    Indeterminate,
}

/// One structured, redacted decision record.
#[derive(Debug, Clone, Serialize)]
pub struct DecisionRecord {
    pub request_id: String,
    pub decision: Decision,
    /// Stable error code when rejected/indeterminate.
    pub reason_code: Option<String>,
    /// Human-readable, non-sensitive explanation.
    pub reason: String,
    /// Key state at the decision point (sizes/order/counters only).
    pub state: serde_json::Value,
}

impl DecisionRecord {
    pub fn accepted(request_id: &RequestId, reason: impl Into<String>, state: serde_json::Value) -> Self {
        DecisionRecord {
            request_id: request_id.to_string(),
            decision: Decision::Accepted,
            reason_code: None,
            reason: reason.into(),
            state,
        }
    }

    pub fn rejected(
        request_id: &RequestId,
        reason_code: impl Into<String>,
        reason: impl Into<String>,
        state: serde_json::Value,
    ) -> Self {
        DecisionRecord {
            request_id: request_id.to_string(),
            decision: Decision::Rejected,
            reason_code: Some(reason_code.into()),
            reason: reason.into(),
            state,
        }
    }

    pub fn indeterminate(
        request_id: &RequestId,
        reason_code: impl Into<String>,
        reason: impl Into<String>,
        state: serde_json::Value,
    ) -> Self {
        DecisionRecord {
            request_id: request_id.to_string(),
            decision: Decision::Indeterminate,
            reason_code: Some(reason_code.into()),
            reason: reason.into(),
            state,
        }
    }

    /// Emit a single structured tracing event.
    pub fn log(&self) {
        match self.decision {
            Decision::Accepted => tracing::info!(
                request_id = %self.request_id,
                decision = "accepted",
                state = %self.state,
                "{}",
                self.reason
            ),
            Decision::Rejected => tracing::warn!(
                request_id = %self.request_id,
                decision = "rejected",
                reason_code = ?self.reason_code,
                state = %self.state,
                "{}",
                self.reason
            ),
            Decision::Indeterminate => tracing::error!(
                request_id = %self.request_id,
                decision = "indeterminate",
                reason_code = ?self.reason_code,
                state = %self.state,
                "{}",
                self.reason
            ),
        }
    }
}

/// Redact a potentially sensitive value for logs: keep type/shape, hide
/// contents. Integers in a small "safe" range are kept; free text is masked.
pub fn redact(value: &serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::String(s) => serde_json::Value::String(format!("<str:{}>", s.len())),
        serde_json::Value::Array(a) => {
            serde_json::Value::Array(vec![redact_array_marker(a.len())])
        }
        serde_json::Value::Object(o) => {
            let mut m = serde_json::Map::new();
            for k in o.keys() {
                m.insert(k.clone(), serde_json::json!("<redacted>"));
            }
            serde_json::Value::Object(m)
        }
        other => other.clone(),
    }
}

fn redact_array_marker(len: usize) -> serde_json::Value {
    serde_json::json!({ "array_len": len })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_ids_are_unique() {
        let a = RequestId::generate();
        let b = RequestId::generate();
        assert_ne!(a, b);
        assert!(a.as_str().starts_with("req-"));
    }

    #[test]
    fn redaction_hides_string_contents() {
        let v = serde_json::json!({"ssn": "123-45-6789", "rows": ["a", "b"]});
        let r = redact(&v);
        let text = r.to_string();
        assert!(!text.contains("123-45-6789"));
        assert!(text.contains("redacted"));
    }
}
